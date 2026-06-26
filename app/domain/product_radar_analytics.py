"""Product radar analytics — ranked insights, release contour, load & closure charts."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Literal

from planning_poker_common.scope.domain import classify_scope_report_bucket

ProductAnalyticsPeriod = Literal["all", "quarter", "month"]

ProductInsightKind = Literal[
    "subtask_bottleneck",
    "release_tail",
    "cross_team_block",
    "load_spike",
    "stalled_parent",
    "dependency_stall",
    "subtask_gap",
    "idle_capacity",
]

_PERIOD_LABELS: dict[str, str] = {
    "all": "За всё время",
    "quarter": "Квартал",
    "month": "Месяц",
}

_PERIOD_DAYS: dict[str, int | None] = {
    "all": None,
    "quarter": 90,
    "month": 30,
}

_MONTH_SHORT_RU = (
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)

_RELEASE_READY_KEYWORDS = (
    "к релиз",
    "ready for release",
    "ready to release",
    "to release",
    "release ready",
)

_DONE_SUBTASK_STATUSES = frozenset({"готово", "done", "closed", "resolved", "cancelled", "canceled"})

_STAGE_COLORS = {
    "backlog": "#94a3b8",
    "pause": "#f59e0b",
    "dev": "#3b82f6",
    "test": "#8b5cf6",
    "release": "#06b6d4",
}

_LONG_AGE_CUTOFF = 30

_SHORT_AGE_BUCKETS: tuple[tuple[str, str, int, int], ...] = (
    ("0_3", "0–3 дн.", 0, 3),
    ("4_7", "4–7 дн.", 4, 7),
    ("8_14", "8–14 дн.", 8, 14),
    ("15_30", "15–30 дн.", 15, 30),
)


def _segment_start(segment: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(
        segment.get("entered_at") or segment.get("started_at") or segment.get("from")
    )


def _segment_end(segment: dict[str, Any], *, fallback: datetime) -> datetime:
    return _parse_timestamp(segment.get("left_at") or segment.get("ended_at") or segment.get("to")) or fallback


def _segment_duration_days(segment: dict[str, Any], start: datetime | None, end: datetime) -> float:
    days = segment.get("duration_days")
    if days is None:
        days = segment.get("days")
    if isinstance(days, (int, float)):
        return float(days)
    seg_start = _segment_start(segment)
    seg_end = _segment_end(segment, fallback=end)
    if seg_start is None:
        return 0.0
    window_start = start or seg_start
    overlap_start = max(seg_start, window_start)
    overlap_end = min(seg_end, end)
    if overlap_end <= overlap_start:
        return 0.0
    return max(0.0, (overlap_end - overlap_start).total_seconds() / 86400.0)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _period_start(period: ProductAnalyticsPeriod, now: datetime) -> datetime | None:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    return now - timedelta(days=days)


def _issue_touched_in_period(issue: dict[str, Any], start: datetime | None, end: datetime) -> bool:
    if start is None:
        return True
    for field in ("updated", "created", "status_changed_at", "status_entered_at", "resolution_date"):
        ts = _parse_timestamp(issue.get(field))
        if ts is not None and start <= ts <= end:
            return True
    segments = issue.get("status_segments") if isinstance(issue.get("status_segments"), list) else []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_start = _segment_start(segment)
        seg_end = _segment_end(segment, fallback=end)
        if seg_start is not None and seg_start <= end and seg_end >= start:
            return True
    return False


def filter_issues_for_period(
    issues: list[dict[str, Any]],
    period: ProductAnalyticsPeriod,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    reference = now or datetime.now(timezone.utc)
    start = _period_start(period, reference)
    if start is None:
        return list(issues)
    return [issue for issue in issues if _issue_touched_in_period(issue, start, reference)]


def _sort_bar_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (-int(item.get("value") or 0), str(item.get("label") or "")))


def _team_label(issue: dict[str, Any]) -> str:
    team = str(issue.get("team") or "").strip()
    if team:
        return team
    labels = issue.get("team_labels") or []
    if isinstance(labels, list) and labels:
        return str(labels[0] or "").strip()
    domain = str(issue.get("domain") or "").strip()
    return domain or "Без команды"


def _is_done_issue(issue: dict[str, Any]) -> bool:
    if _parse_timestamp(issue.get("resolution_date")):
        return True
    category = str(issue.get("status_category") or "").lower()
    if category in {"done", "complete", "completed"}:
        return True
    status = str(issue.get("status") or "").strip().lower()
    if status in _DONE_SUBTASK_STATUSES:
        return True
    resolution = str(issue.get("resolution") or "").strip().lower()
    return bool(resolution and resolution not in {"unresolved", "none"})


def _is_subtask_done(subtask: dict[str, Any]) -> bool:
    category = str(subtask.get("status_category") or "").lower()
    status = str(subtask.get("status") or "").strip().lower()
    if category == "done":
        return True
    return status in _DONE_SUBTASK_STATUSES


def _is_ready_for_release(issue: dict[str, Any]) -> bool:
    status = str(issue.get("status") or "").strip().casefold()
    if not status:
        return False
    if status in {"к релизу", "ready for release", "to release", "ready to release", "release ready"}:
        return True
    return any(keyword in status for keyword in _RELEASE_READY_KEYWORDS)


def _release_contour_stage(issue: dict[str, Any]) -> str:
    if _is_ready_for_release(issue):
        return "release"
    bucket = classify_scope_report_bucket(issue)
    if bucket == "not_started":
        return "backlog"
    if bucket == "open_questions":
        return "pause"
    if bucket == "in_test":
        return "test"
    if bucket == "in_work":
        return "dev"
    return "backlog"


def _issue_story_points(issue: dict[str, Any]) -> float:
    value = issue.get("story_points")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return 0.0


def _issue_status_days(issue: dict[str, Any], *, now: datetime | None = None) -> float:
    days = issue.get("current_status_days")
    if isinstance(days, (int, float)):
        return float(days)
    reference = now or datetime.now(timezone.utc)
    entered = _parse_timestamp(issue.get("status_entered_at") or issue.get("status_changed_at"))
    if entered is None:
        return 0.0
    return max(0.0, (reference - entered).total_seconds() / 86400.0)


def _subtask_team(subtask: dict[str, Any], parent: dict[str, Any]) -> str:
    assignee = str(subtask.get("assignee") or "").strip()
    if assignee:
        return assignee
    return _team_label(parent)


def _collect_subtask_bottleneck_insights(issues: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    for issue in issues:
        if _is_done_issue(issue):
            continue
        bucket = classify_scope_report_bucket(issue)
        if bucket not in {"in_work", "in_test"}:
            continue
        subtasks = [item for item in (issue.get("subtasks") or []) if isinstance(item, dict)]
        if len(subtasks) < 2:
            continue
        open_subtasks = [item for item in subtasks if not _is_subtask_done(item)]
        done_count = len(subtasks) - len(open_subtasks)
        if len(open_subtasks) != 1 or done_count < len(subtasks) - 1:
            continue
        blocker = open_subtasks[0]
        blocker_key = str(blocker.get("key") or "")
        blocker_status = str(blocker.get("status") or "—")
        blocker_team = _subtask_team(blocker, issue)
        parent_team = _team_label(issue)
        parent_days = _issue_status_days(issue, now=now)
        key = str(issue.get("key") or "")
        score = 88 + min(12, int(parent_days // 3))
        insights.append(
            {
                "kind": "subtask_bottleneck",
                "severity": "high" if parent_days >= 7 else "medium",
                "score": score,
                "issue_key": key,
                "summary": str(issue.get("summary") or key),
                "issue_url": issue.get("url"),
                "title": "Узкое горлышко в подзадачах",
                "detail": (
                    f"{done_count} из {len(subtasks)} подзадач готовы, но {blocker_key} "
                    f"({blocker_team}) в «{blocker_status}» держит всю {key}."
                ),
                "parent_team": parent_team,
                "blocker_key": blocker_key,
                "blocker_team": blocker_team,
                "blocker_status": blocker_status,
                "days": round(parent_days, 1),
                "metric_label": "Готово подзадач",
                "metric_value": f"{done_count}/{len(subtasks)}",
            }
        )
    return insights


def _collect_release_tail_insights(issues: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    for issue in issues:
        if _is_done_issue(issue):
            continue
        if not _is_ready_for_release(issue):
            continue
        days = _issue_status_days(issue, now=now)
        if days < 3:
            continue
        key = str(issue.get("key") or "")
        team = _team_label(issue)
        insights.append(
            {
                "kind": "release_tail",
                "severity": "high" if days >= 7 else "medium",
                "score": 75 + min(20, int(days)),
                "issue_key": key,
                "summary": str(issue.get("summary") or key),
                "issue_url": issue.get("url"),
                "title": "Застряла на релизном контуре",
                "detail": f"{key} ({team}) в «{issue.get('status')}» уже {days:.0f} дн. — релиз не выехал.",
                "parent_team": team,
                "days": round(days, 1),
                "metric_label": "Дней на релизе",
                "metric_value": f"{days:.0f}",
            }
        )
    return insights


def _signal_to_insight(signal: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(signal.get("kind") or "")
    severity = str(signal.get("severity") or "low")
    base_score = {"high": 80, "medium": 55, "low": 30}.get(severity, 20)
    days = signal.get("days")
    score = base_score + (min(15, int(float(days) // 2)) if isinstance(days, (int, float)) else 0)
    if kind == "cross_team_block":
        return {**signal, "score": score + 5, "metric_label": "Блокирует", "metric_value": str(signal.get("blocking_team") or "—")}
    if kind in {"dependency_stall", "subtask_gap", "stalled", "stuck_in_test", "stuck_in_dev", "handoff_stuck"}:
        metric_value = f"{float(days):.0f} дн." if isinstance(days, (int, float)) else ""
        return {**signal, "score": score, "metric_label": "В статусе", "metric_value": metric_value}
    return None


def collect_product_insights(
    issues: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Ranked actionable insights — not a flat signal dump."""
    reference = now or datetime.now(timezone.utc)
    issue_keys = {str(issue.get("key") or "") for issue in issues}
    merged: dict[str, dict[str, Any]] = {}
    for insight in _collect_subtask_bottleneck_insights(issues, now=reference):
        merged[f"{insight['kind']}:{insight['issue_key']}"] = insight
    for insight in _collect_release_tail_insights(issues, now=reference):
        merged[f"{insight['kind']}:{insight['issue_key']}"] = insight
    for signal in signals:
        issue_key = str(signal.get("issue_key") or "")
        if issue_keys and issue_key and issue_key not in issue_keys:
            continue
        converted = _signal_to_insight(signal)
        if not converted:
            continue
        key = f"{converted.get('kind')}:{converted.get('issue_key')}"
        existing = merged.get(key)
        if existing is None or int(converted.get("score") or 0) > int(existing.get("score") or 0):
            merged[key] = converted

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            -int(item.get("score") or 0),
            {"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity")), 9),
            str(item.get("issue_key") or ""),
        ),
    )
    return ranked[:limit]


def build_release_contour(active_issues: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    labels = {
        "backlog": "Очередь",
        "pause": "Пауза",
        "dev": "Разработка",
        "test": "Тестирование",
        "release": "К релизу",
    }
    stats: dict[str, dict[str, Any]] = {
        key: {"key": key, "label": labels[key], "count": 0, "sp": 0.0, "days_sum": 0.0, "color": _STAGE_COLORS[key]}
        for key in labels
    }
    for issue in active_issues:
        stage = _release_contour_stage(issue)
        if stage not in stats:
            continue
        row = stats[stage]
        row["count"] += 1
        row["sp"] += _issue_story_points(issue)
        row["days_sum"] += _issue_status_days(issue, now=now)

    stages = []
    for key in ("backlog", "pause", "dev", "test", "release"):
        row = stats[key]
        count = int(row["count"])
        stages.append(
            {
                **row,
                "sp": round(float(row["sp"]), 1),
                "avg_days": round(float(row["days_sum"]) / count, 1) if count else 0.0,
            }
        )
    stages = _sort_bar_items(stages)
    total = sum(stage["count"] for stage in stages)
    return {"stages": stages, "total_active": total}


def _nice_number(value: float) -> float:
    if value <= 1:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    residual = value / magnitude
    if residual <= 1:
        nice = 1
    elif residual <= 2:
        nice = 2
    elif residual <= 5:
        nice = 5
    else:
        nice = 10
    return nice * magnitude


def _format_age_label(low: float, high: float, *, open_end: bool) -> str:
    low_i = int(math.floor(low))
    high_i = int(math.ceil(high))
    if open_end:
        return f"{low_i}+ дн."
    if low_i >= high_i:
        return f"{low_i} дн."
    return f"{low_i}–{high_i} дн."


def _subdivide_long_age_range(
    min_days: float,
    max_days: float,
    *,
    max_buckets: int = 6,
) -> list[tuple[str, str, float, float]]:
    """Split ages above 30 days into readable buckets instead of one catch-all bin."""
    start = max(_LONG_AGE_CUTOFF + 1, int(math.floor(min_days)))
    end = int(math.ceil(max_days))
    if end <= start:
        return [("long_0", _format_age_label(start, end, open_end=True), float(start), 10_000.0)]

    span_days = end - start + 1
    bucket_count = min(max_buckets, max(3, int(math.ceil(span_days / 20))))
    step = max(1, int(_nice_number(span_days / bucket_count)))

    defs: list[tuple[str, str, float, float]] = []
    current = start
    index = 0
    while current <= end and index < bucket_count:
        bucket_end = min(end, current + step - 1)
        is_last = index == bucket_count - 1 or bucket_end >= end
        if is_last:
            defs.append((f"long_{index}", _format_age_label(current, end, open_end=True), float(current), 10_000.0))
            break
        defs.append(
            (
                f"long_{index}",
                _format_age_label(current, bucket_end, open_end=False),
                float(current),
                float(bucket_end),
            )
        )
        current = bucket_end + 1
        index += 1
    return defs


def _adaptive_age_bucket_defs(sorted_ages: list[float]) -> list[tuple[str, str, float, float]]:
    """Short fixed buckets up to 30d, then auto-expanded long-tail buckets from actual data."""
    if not sorted_ages:
        return []

    defs: list[tuple[str, str, float, float]] = []
    for key, label, low, high in _SHORT_AGE_BUCKETS:
        if any(low <= age <= high for age in sorted_ages):
            defs.append((key, label, float(low), float(high)))

    long_ages = [age for age in sorted_ages if age > _LONG_AGE_CUTOFF]
    if long_ages:
        long_min = min(long_ages)
        long_max = max(long_ages)
        if long_min == long_max:
            defs.append(
                ("long_only", _format_age_label(long_min, long_max, open_end=True), long_min, 10_000.0)
            )
        else:
            spread = long_max - long_min
            if spread > 120:
                max_long_buckets = 7
            elif spread > 60:
                max_long_buckets = 5
            else:
                max_long_buckets = 4
            defs.extend(_subdivide_long_age_range(long_min, long_max, max_buckets=max_long_buckets))

    if defs:
        return defs

    only_age = sorted_ages[0]
    return [("only", _format_age_label(only_age, only_age, open_end=False), only_age, only_age)]


def _age_bucket_color(index: int, total: int, *, max_age: float, high: float) -> str:
    if high >= max_age and max_age > _LONG_AGE_CUTOFF:
        return "#ef4444"
    if index >= total - 2:
        return "#f59e0b"
    if index >= total - 3:
        return "#3b82f6"
    return "#94a3b8"


def build_load_by_team(active_issues: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "team": "",
            "active_count": 0,
            "active_sp": 0.0,
            "test_count": 0,
            "release_count": 0,
            "issue_keys": [],
        }
    )
    for issue in active_issues:
        team = _team_label(issue)
        bucket = classify_scope_report_bucket(issue)
        if bucket not in {"in_work", "in_test"} and not _is_ready_for_release(issue):
            continue
        row = stats[team]
        row["team"] = team
        row["active_count"] += 1
        row["active_sp"] += _issue_story_points(issue)
        issue_key = str(issue.get("key") or "")
        if issue_key:
            row["issue_keys"].append(issue_key)
        if bucket == "in_test":
            row["test_count"] += 1
        if _is_ready_for_release(issue):
            row["release_count"] += 1

    rows = sorted(
        stats.values(),
        key=lambda item: (-float(item["active_sp"]), -int(item["active_count"]), str(item["team"])),
    )
    items = [
        {
            **row,
            "active_sp": round(float(row["active_sp"]), 1),
            "key": str(row["team"]),
            "label": str(row["team"]),
            "value": int(row["active_count"]),
        }
        for row in rows[:limit]
    ]
    return items


def build_status_age_histogram(active_issues: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    reference = now or datetime.now(timezone.utc)
    ages = []
    for issue in active_issues:
        if _is_done_issue(issue) or classify_scope_report_bucket(issue) == "done":
            continue
        days = _issue_status_days(issue, now=reference)
        if days >= 0:
            ages.append(days)
    if not ages:
        return []

    sorted_ages = sorted(ages)
    bucket_defs = _adaptive_age_bucket_defs(sorted_ages)
    counts = {bucket[0]: 0 for bucket in bucket_defs}
    for days in ages:
        for key, _label, low, high in bucket_defs:
            if low <= days <= high:
                counts[key] += 1
                break

    max_age = sorted_ages[-1]
    items: list[dict[str, Any]] = []
    visible_count = sum(1 for key, _, _, _ in bucket_defs if counts[key] > 0)
    visible_index = 0
    for key, label, _low, high in bucket_defs:
        if counts[key] <= 0:
            continue
        items.append(
            {
                "key": key,
                "label": label,
                "value": counts[key],
                "color": _age_bucket_color(visible_index, visible_count, max_age=max_age, high=high),
            }
        )
        visible_index += 1
    return items


def _segment_overlap_days(segment: dict[str, Any], start: datetime | None, end: datetime) -> float:
    return _segment_duration_days(segment, start, end)


def _normalize_flow_bucket(segment: dict[str, Any]) -> str:
    bucket = str(segment.get("bucket") or segment.get("flow_bucket") or "other").lower()
    if bucket in {"dev", "test", "pause", "todo", "other"}:
        return bucket
    mapping = {
        "in_work": "dev",
        "in_test": "test",
        "not_started": "todo",
        "open_questions": "pause",
        "done": "other",
    }
    return mapping.get(bucket, "other")


def build_phase_totals(
    issues: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    period: ProductAnalyticsPeriod = "all",
) -> dict[str, float]:
    reference = now or datetime.now(timezone.utc)
    start = _period_start(period, reference)
    totals = {"dev": 0.0, "test": 0.0, "pause": 0.0, "todo": 0.0, "other": 0.0}
    for issue in issues:
        buckets = issue.get("status_bucket_durations") if isinstance(issue.get("status_bucket_durations"), dict) else {}
        if buckets:
            for key in totals:
                value = buckets.get(key)
                if isinstance(value, (int, float)):
                    totals[key] += float(value)
            continue
        segments = issue.get("status_segments") if isinstance(issue.get("status_segments"), list) else []
        if not segments:
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            bucket = _normalize_flow_bucket(segment)
            totals[bucket] += _segment_overlap_days(segment, start, reference)
    return {key: round(value, 1) for key, value in totals.items()}


def build_people_load(
    issues: list[dict[str, Any]],
    *,
    period: ProductAnalyticsPeriod = "all",
    now: datetime | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    reference = now or datetime.now(timezone.utc)
    scoped = filter_issues_for_period(issues, period, now=reference)
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "active_count": 0, "backlog_count": 0, "active_sp": 0.0, "issue_keys": []}
    )
    for issue in scoped:
        if _is_done_issue(issue):
            continue
        name = str(issue.get("assignee") or issue.get("developer") or "").strip() or "Без исполнителя"
        bucket = classify_scope_report_bucket(issue)
        row = stats[name]
        row["name"] = name
        key = str(issue.get("key") or "")
        if key:
            row["issue_keys"].append(key)
        sp = _issue_story_points(issue)
        if bucket in {"in_work", "in_test"}:
            row["active_count"] += 1
            row["active_sp"] += sp
        elif bucket in {"not_started", "open_questions"}:
            row["backlog_count"] += 1

    items = [
        {
            "key": row["name"],
            "label": row["name"],
            "value": int(row["active_count"]),
            "sp": round(float(row["active_sp"]), 1),
            "backlog_count": int(row["backlog_count"]),
            "issue_keys": row["issue_keys"],
        }
        for row in stats.values()
        if row["name"] != "Без исполнителя" or row["active_count"] or row["backlog_count"]
    ]
    items.sort(key=lambda item: (-int(item["value"]), -float(item["sp"]), str(item["label"])))
    return items[:limit]


def build_team_blocking(
    signals: list[dict[str, Any]],
    issues_by_key: dict[str, dict[str, Any]] | None = None,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    cross = [signal for signal in signals if signal.get("kind") == "cross_team_block"]
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"team": "", "blocks": 0, "blocked_teams": set(), "items": []}
    )
    for signal in cross:
        team = str(signal.get("blocking_team") or signal.get("blocker_team") or "—")
        row = stats[team]
        row["team"] = team
        row["blocks"] += 1
        blocked_team = str(signal.get("blocked_team") or "")
        if blocked_team:
            row["blocked_teams"].add(blocked_team)
        issue_key = str(signal.get("issue_key") or "")
        issue = (issues_by_key or {}).get(issue_key, {})
        row["items"].append(
            {
                "issue_key": issue_key,
                "summary": str(signal.get("summary") or issue.get("summary") or issue_key),
                "issue_url": signal.get("issue_url") or issue.get("url"),
                "blocked_team": blocked_team,
                "blocker_key": str(signal.get("blocker_key") or ""),
                "detail": str(signal.get("detail") or ""),
                "blocker_status": str(signal.get("blocker_status") or ""),
            }
        )

    teams = sorted(
        [
            {
                "key": row["team"],
                "label": row["team"],
                "value": int(row["blocks"]),
                "color": "#ef4444",
                "blocked_teams": sorted(row["blocked_teams"]),
                "items": row["items"],
            }
            for row in stats.values()
        ],
        key=lambda item: (-int(item["value"]), str(item["label"])),
    )[:limit]
    return {"teams": teams, "total_blocks": len(cross)}


def build_chart_details(
    issues: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    period: ProductAnalyticsPeriod,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    scoped = filter_issues_for_period(issues, period, now=reference)
    lookup = {str(issue.get("key") or ""): issue for issue in issues if issue.get("key")}
    closure_items: list[dict[str, Any]] = []
    if period == "all":
        range_start = None
        range_end = _day_start(reference)
    else:
        range_start, range_end = _closure_daily_range(period, reference)
        range_start = _day_start(range_start)
        range_end = _day_start(range_end)
    for issue in issues:
        if not _is_done_issue(issue):
            continue
        resolved = _parse_timestamp(issue.get("resolution_date") or issue.get("updated"))
        if resolved is None:
            continue
        resolved_day = _day_start(resolved)
        if range_start is not None and resolved_day < range_start:
            continue
        if resolved_day > range_end:
            continue
        key = str(issue.get("key") or "")
        closure_items.append(
            {
                "issue_key": key,
                "summary": str(issue.get("summary") or key),
                "issue_url": issue.get("url"),
                "resolved_at": resolved.isoformat(),
                "story_points": _issue_story_points(issue),
                "team": _team_label(issue),
            }
        )
    closure_items.sort(key=lambda item: str(item.get("resolved_at") or ""), reverse=True)

    phase_items: list[dict[str, Any]] = []
    for issue in scoped:
        buckets = issue.get("status_bucket_durations") if isinstance(issue.get("status_bucket_durations"), dict) else {}
        if not buckets:
            continue
        dominant = max(buckets.items(), key=lambda pair: float(pair[1] or 0), default=("other", 0))
        phase_items.append(
            {
                "issue_key": str(issue.get("key") or ""),
                "summary": str(issue.get("summary") or ""),
                "issue_url": issue.get("url"),
                "dominant_phase": dominant[0],
                "phase_days": round(float(dominant[1] or 0), 1),
                "buckets": buckets,
            }
        )
    phase_items.sort(key=lambda item: -float(item.get("phase_days") or 0))

    return {
        "people_load": build_people_load(issues, period=period, now=reference),
        "team_blocking": build_team_blocking(signals, lookup),
        "closures": closure_items,
        "phase_time": phase_items,
        "epic_interior": build_epic_interior_stats(scoped)["groups"],
    }


def _issue_epic_key(issue: dict[str, Any]) -> str:
    for field in ("linked_epic_key", "epic_key", "flow_epic_key", "parent_key"):
        value = str(issue.get(field) or "").strip()
        if value:
            return value
    return ""


def _is_epic_issue(issue: dict[str, Any]) -> bool:
    issue_type = str(issue.get("issue_type") or issue.get("type") or "").strip().lower()
    if isinstance(issue.get("issue_type"), dict):
        issue_type = str(issue["issue_type"].get("name") or "").strip().lower()
    return issue_type == "epic"


def _interior_child_done(child: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> bool:
    key = str(child.get("issue_key") or child.get("key") or "")
    full = lookup.get(key)
    if isinstance(full, dict):
        return _is_done_issue(full)
    if child.get("done") is not None:
        return bool(child.get("done"))
    return _is_subtask_done(child)


def _collect_interior_children(
    issue: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Subtasks + all Jira issue links («Связанные задачи»)."""
    parent_key = str(issue.get("key") or "")
    seen: set[str] = set()
    children: list[dict[str, Any]] = []

    for subtask in issue.get("subtasks") or []:
        if not isinstance(subtask, dict):
            continue
        key = str(subtask.get("key") or "")
        if not key or key == parent_key or key in seen:
            continue
        seen.add(key)
        full = lookup.get(key)
        children.append(
            {
                "issue_key": key,
                "summary": str((full or subtask).get("summary") or subtask.get("summary") or key),
                "status": str((full or subtask).get("status") or subtask.get("status") or ""),
                "assignee": str((full or subtask).get("assignee") or subtask.get("assignee") or ""),
                "done": _is_done_issue(full) if isinstance(full, dict) else _is_subtask_done(subtask),
                "link_kind": "subtask",
                "relation": "subtask",
                "relation_label": "Подзадача",
            }
        )

    for link in issue.get("issue_links") or []:
        if not isinstance(link, dict):
            continue
        key = str(link.get("key") or "")
        if not key or key == parent_key or key in seen:
            continue
        seen.add(key)
        full = lookup.get(key)
        relation = str(link.get("relation") or "relates")
        relation_label = str(link.get("relation_label") or relation)
        if isinstance(full, dict):
            children.append(
                {
                    "issue_key": key,
                    "summary": str(full.get("summary") or key),
                    "status": str(full.get("status") or link.get("status") or ""),
                    "assignee": str(full.get("assignee") or link.get("assignee") or ""),
                    "done": _is_done_issue(full),
                    "link_kind": relation,
                    "relation": relation,
                    "relation_label": relation_label,
                }
            )
        else:
            children.append(
                {
                    "issue_key": key,
                    "summary": str(link.get("summary") or key),
                    "status": str(link.get("status") or ""),
                    "assignee": str(link.get("assignee") or ""),
                    "done": _is_subtask_done(link),
                    "link_kind": relation,
                    "relation": relation,
                    "relation_label": relation_label,
                }
            )

    return children


def _child_issue_rows(children: list[dict[str, Any]], *, limit: int = 24) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for child in children:
        rows.append(
            {
                "issue_key": str(child.get("issue_key") or child.get("key") or ""),
                "summary": str(child.get("summary") or child.get("issue_key") or child.get("key") or ""),
                "status": str(child.get("status") or ""),
                "assignee": str(child.get("assignee") or ""),
                "done": bool(child.get("done")),
                "relation": child.get("relation"),
                "relation_label": child.get("relation_label"),
                "link_kind": child.get("link_kind"),
            }
        )
    rows.sort(key=lambda row: (bool(row.get("done")), str(row.get("issue_key") or "")))
    return rows[:limit]


def _interior_progress(children: list[dict[str, Any]], lookup: dict[str, dict[str, Any]]) -> tuple[int, int, int]:
    done_count = sum(1 for child in children if _interior_child_done(child, lookup))
    total = len(children)
    return done_count, total - done_count, total


def _append_epic_child(bucket: dict[str, dict[str, Any]], issue: dict[str, Any]) -> None:
    key = str(issue.get("key") or "")
    if key and key not in bucket:
        bucket[key] = issue


def build_epic_interior_stats(
    issues: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> dict[str, Any]:
    """Progress inside epics/stories: subtasks + all linked Jira issues."""
    lookup = {str(issue.get("key") or ""): issue for issue in issues if str(issue.get("key") or "").strip()}
    epic_buckets: dict[str, dict[str, dict[str, Any]]] = {}
    epic_summaries: dict[str, str] = {}
    parents: list[dict[str, Any]] = []
    totals = {
        "epics": 0,
        "parents": 0,
        "tasks": 0,
        "subtasks": 0,
        "linked_tasks": 0,
        "done_tasks": 0,
        "open_tasks": 0,
        "done_subtasks": 0,
        "open_subtasks": 0,
    }

    for issue in issues:
        key = str(issue.get("key") or "")
        if _is_epic_issue(issue) and key:
            epic_summaries[key] = str(issue.get("summary") or key)
        epic_key = _issue_epic_key(issue)
        if epic_key and epic_key != key:
            epic_buckets.setdefault(epic_key, {})
            _append_epic_child(epic_buckets[epic_key], issue)

    for issue in issues:
        source_key = str(issue.get("key") or "")
        for link in issue.get("issue_links") or []:
            if not isinstance(link, dict):
                continue
            target_key = str(link.get("key") or "")
            if not target_key or target_key == source_key:
                continue
            target = lookup.get(target_key)
            if isinstance(target, dict) and _is_epic_issue(target):
                epic_buckets.setdefault(target_key, {})
                _append_epic_child(epic_buckets[target_key], issue)
            elif _is_epic_issue(issue):
                epic_buckets.setdefault(source_key, {})
                if isinstance(target, dict):
                    _append_epic_child(epic_buckets[source_key], target)

    epics: list[dict[str, Any]] = []
    for epic_key, child_map in epic_buckets.items():
        children_issues = list(child_map.values())
        if not children_issues:
            continue
        flat_children: list[dict[str, Any]] = []
        for child_issue in children_issues:
            key = str(child_issue.get("key") or "")
            if not key:
                continue
            flat_children.append(
                {
                    "issue_key": key,
                    "summary": str(child_issue.get("summary") or key),
                    "status": str(child_issue.get("status") or ""),
                    "assignee": str(child_issue.get("assignee") or ""),
                    "done": _is_done_issue(child_issue),
                    "relation": "in_epic",
                    "relation_label": "В эпике",
                    "link_kind": "in_epic",
                }
            )
        done_count, open_count, total = _interior_progress(flat_children, lookup)
        totals["epics"] += 1
        totals["tasks"] += total
        totals["done_tasks"] += done_count
        totals["open_tasks"] += open_count
        epics.append(
            {
                "key": epic_key,
                "label": epic_key,
                "group_type": "epic",
                "summary": epic_summaries.get(epic_key) or f"Эпик {epic_key}",
                "issue_url": lookup.get(epic_key, {}).get("url") if epic_key in lookup else None,
                "value": open_count,
                "total_subtasks": total,
                "done_subtasks": done_count,
                "open_subtasks": open_count,
                "progress_pct": round((done_count / total) * 100) if total else 0,
                "subtasks": _child_issue_rows(flat_children),
            }
        )

    for issue in issues:
        key = str(issue.get("key") or "")
        if not key or _is_epic_issue(issue):
            continue
        children = _collect_interior_children(issue, lookup)
        if not children:
            continue
        done_count, open_count, total = _interior_progress(children, lookup)
        linked_count = sum(1 for child in children if str(child.get("link_kind") or "") != "subtask")
        subtask_count = total - linked_count
        totals["parents"] += 1
        totals["subtasks"] += subtask_count
        totals["linked_tasks"] += linked_count
        totals["done_subtasks"] += done_count
        totals["open_subtasks"] += open_count
        epic_key = _issue_epic_key(issue)
        parents.append(
            {
                "key": key,
                "label": key,
                "group_type": "linked" if linked_count else "parent",
                "summary": str(issue.get("summary") or key),
                "issue_url": issue.get("url"),
                "epic_key": epic_key,
                "team": _team_label(issue),
                "value": open_count,
                "total_subtasks": total,
                "done_subtasks": done_count,
                "open_subtasks": open_count,
                "progress_pct": round((done_count / total) * 100) if total else 0,
                "subtasks": _child_issue_rows(children),
            }
        )

    epics.sort(
        key=lambda row: (
            -int(row.get("open_subtasks") or 0),
            -int(row.get("total_subtasks") or 0),
            str(row.get("key") or ""),
        )
    )
    parents.sort(
        key=lambda row: (
            -int(row.get("open_subtasks") or 0),
            -int(row.get("total_subtasks") or 0),
            str(row.get("key") or ""),
        )
    )
    combined = epics + parents
    return {
        "totals": totals,
        "epics": epics[:limit],
        "parents": parents[:limit],
        "groups": combined[: limit * 2],
    }


def _day_start(value: datetime) -> datetime:
    normalized = value.astimezone(timezone.utc)
    return normalized.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(value: datetime) -> datetime:
    day = _day_start(value)
    return day.replace(day=1)


def _iter_months(start: datetime, end: datetime) -> list[datetime]:
    months: list[datetime] = []
    current = _month_start(start)
    end_month = _month_start(end)
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def _month_label(value: datetime) -> str:
    return f"{_MONTH_SHORT_RU[value.month - 1]} {value.strftime('%y')}"


def _month_end(value: datetime) -> datetime:
    start = _month_start(value)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1, day=1)
    else:
        next_month = start.replace(month=start.month + 1, day=1)
    return _day_start(next_month - timedelta(days=1))


def _months_ago_month_start(reference: datetime, months: int) -> datetime:
    """Start of the month `months` calendar months ago, including the current month."""
    current = _month_start(reference)
    if months < 1:
        return current
    year = current.year
    month = current.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return current.replace(year=year, month=month, day=1)


def _collect_closed_issues(
    issues: list[dict[str, Any]],
    period_start: datetime | None,
    reference: datetime,
) -> list[tuple[datetime, dict[str, Any]]]:
    closed: list[tuple[datetime, dict[str, Any]]] = []
    for issue in issues:
        if not _is_done_issue(issue):
            continue
        resolved = _parse_timestamp(issue.get("resolution_date") or issue.get("updated"))
        if resolved is None:
            continue
        if period_start is not None and _day_start(resolved) < _day_start(period_start):
            continue
        if _day_start(resolved) > _day_start(reference):
            continue
        closed.append((resolved, issue))
    return closed


def _collect_all_closed_issues(
    issues: list[dict[str, Any]],
    reference: datetime,
) -> list[tuple[datetime, dict[str, Any]]]:
    return _collect_closed_issues(issues, None, reference)


def _closure_bucket_item(
    key: str,
    label: str,
    count: int,
    sp: float,
    *,
    issue_keys: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": count,
        "sp": round(sp, 1),
        "color": "#10b981",
        "issue_keys": list(issue_keys or []),
    }


def _build_closure_daily(
    closed: list[tuple[datetime, dict[str, Any]]],
    range_start: datetime,
    range_end: datetime,
    *,
    day_label: bool = False,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    sp_map: dict[str, float] = defaultdict(float)
    keys_map: dict[str, list[str]] = defaultdict(list)
    for resolved, issue in closed:
        day = _day_start(resolved)
        key = day.strftime("%Y-%m-%d")
        issue_key = str(issue.get("key") or "").strip()
        counts[key] += 1
        sp_map[key] += _issue_story_points(issue)
        if issue_key:
            keys_map[key].append(issue_key)

    buckets: list[dict[str, Any]] = []
    current = _day_start(range_start)
    range_end_day = _day_start(range_end)
    while current <= range_end_day:
        key = current.strftime("%Y-%m-%d")
        month = _month_start(current)
        label = str(current.day) if day_label else current.strftime("%d.%m")
        item = _closure_bucket_item(
            key,
            label,
            counts.get(key, 0),
            sp_map.get(key, 0.0),
            issue_keys=keys_map.get(key, []),
        )
        item["month_key"] = month.strftime("%Y-%m")
        item["month_label"] = _month_label(month)
        buckets.append(item)
        current += timedelta(days=1)
    return buckets


def _build_closure_monthly_range(
    closed: list[tuple[datetime, dict[str, Any]]],
    range_start: datetime,
    range_end: datetime,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    sp_map: dict[str, float] = defaultdict(float)
    keys_map: dict[str, list[str]] = defaultdict(list)
    for resolved, issue in closed:
        month = _month_start(resolved)
        key = month.strftime("%Y-%m")
        issue_key = str(issue.get("key") or "").strip()
        counts[key] += 1
        sp_map[key] += _issue_story_points(issue)
        if issue_key:
            keys_map[key].append(issue_key)

    buckets: list[dict[str, Any]] = []
    for month in _iter_months(range_start, range_end):
        key = month.strftime("%Y-%m")
        buckets.append(
            _closure_bucket_item(
                key,
                _month_label(month),
                counts.get(key, 0),
                sp_map.get(key, 0.0),
                issue_keys=keys_map.get(key, []),
            )
        )
    return buckets


def _closure_daily_range(period: ProductAnalyticsPeriod, reference: datetime) -> tuple[datetime, datetime]:
    if period == "month":
        return _month_start(reference), _month_end(reference)
    if period == "quarter":
        return _months_ago_month_start(reference, 3), _month_end(reference)
    range_end = _day_start(reference)
    period_days = _PERIOD_DAYS.get(period) or 30
    return range_end - timedelta(days=period_days - 1), range_end


def build_closure_trend(
    issues: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    period: ProductAnalyticsPeriod = "all",
) -> list[dict[str, Any]]:
    """Actual (non-cumulative) closures per calendar day or month."""
    reference = now or datetime.now(timezone.utc)

    if period == "all":
        closed = _collect_all_closed_issues(issues, reference)
        if closed:
            range_start = min(_month_start(resolved) for resolved, _ in closed)
        else:
            range_start = _month_start(reference)
        range_end = _month_start(reference)
        return _build_closure_monthly_range(closed, range_start, range_end)

    range_start, range_end = _closure_daily_range(period, reference)
    closed = _collect_closed_issues(issues, range_start, reference)
    return _build_closure_daily(closed, range_start, range_end, day_label=True)


def build_throughput_summary(
    issues: list[dict[str, Any]],
    scoped_issues: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    period: ProductAnalyticsPeriod = "all",
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    start = _period_start(period, reference)
    active = [issue for issue in scoped_issues if not _is_done_issue(issue)]
    wip = sum(
        1
        for issue in active
        if classify_scope_report_bucket(issue) in {"in_work", "in_test"} or _is_ready_for_release(issue)
    )
    done_in_period = 0
    done_sp_in_period = 0.0
    for issue in issues:
        if not _is_done_issue(issue):
            continue
        resolved = _parse_timestamp(issue.get("resolution_date") or issue.get("updated"))
        if resolved is None:
            continue
        if start is not None and resolved < start:
            continue
        done_in_period += 1
        done_sp_in_period += _issue_story_points(issue)

    period_days = _PERIOD_DAYS.get(period) or max(
        30,
        int(
            (
                reference
                - min(
                    (_parse_timestamp(issue.get("created")) or reference)
                    for issue in issues
                )
            ).total_seconds()
            / 86400.0
        )
        if issues
        else 30,
    )
    weeks_in_period = max(1.0, period_days / 7.0)
    target = max(3, round(len(active) * 0.15 * weeks_in_period)) if active else max(3, round(weeks_in_period * 2))
    ratio = round(done_in_period / target, 2) if target else 0.0
    return {
        "wip": wip,
        "active": len(active),
        "done_in_period": done_in_period,
        "done_sp_in_period": round(done_sp_in_period, 1),
        "done_7d": done_in_period if period == "month" else 0,
        "done_14d": done_in_period if period == "quarter" else 0,
        "target_in_period": target,
        "target_per_week": max(1, round(target / weeks_in_period)),
        "ratio": ratio,
        "period_days": period_days,
    }


def compute_period_analytics(
    issues: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    period: ProductAnalyticsPeriod,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    scoped_issues = filter_issues_for_period(issues, period, now=reference)
    scoped_keys = {str(issue.get("key") or "") for issue in scoped_issues}
    scoped_signals = [
        signal
        for signal in signals
        if not scoped_keys or str(signal.get("issue_key") or "") in scoped_keys
    ]
    active_issues = [issue for issue in scoped_issues if not _is_done_issue(issue)]
    return {
        "period": period,
        "label": _PERIOD_LABELS[period],
        "issue_count": len(scoped_issues),
        "active_count": len(active_issues),
        "insights": collect_product_insights(scoped_issues, scoped_signals, now=reference),
        "release_contour": build_release_contour(active_issues, now=reference),
        "load_by_team": build_load_by_team(active_issues),
        "status_age": build_status_age_histogram(active_issues, now=reference),
        "phase_totals": build_phase_totals(scoped_issues, now=reference, period=period),
        "people_load": build_people_load(issues, period=period, now=reference),
        "team_blocking": build_team_blocking(scoped_signals, {str(i.get("key") or ""): i for i in scoped_issues}),
        "chart_details": build_chart_details(issues, scoped_signals, period, now=reference),
        "closure_trend": build_closure_trend(issues, now=reference, period=period),
        "epic_interior": build_epic_interior_stats(scoped_issues),
        "throughput": build_throughput_summary(issues, scoped_issues, now=reference, period=period),
    }


def compute_product_analytics(
    issues: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    periods = {
        period: compute_period_analytics(issues, signals, period, now=reference)
        for period in ("month", "quarter", "all")
    }
    default_period: ProductAnalyticsPeriod = "month"
    default_bundle = periods[default_period]
    return {
        "default_period": default_period,
        "periods": periods,
        **default_bundle,
    }
