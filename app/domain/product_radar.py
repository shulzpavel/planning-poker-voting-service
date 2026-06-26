"""Product radar — portfolio workload, people bands, signals and triggers."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from planning_poker_common.scope.domain import classify_scope_report_bucket, normalize_scope_issue

from app.domain.product_radar_analytics import compute_product_analytics
from app.domain.scope_flow_pace import collect_product_flow_alerts

ProductRadarHealth = Literal["ok", "attention", "critical"]
PersonLoadBand = Literal["loaded", "normal", "idle", "light"]

_LOADED_ACTIVE_MIN = 3
_LOADED_SP_MIN = 13.0

_TRIGGER_LABELS: dict[str, str] = {
    "stalled": "Зависание в статусе",
    "stuck_in_test": "Застряла в тестировании",
    "stuck_in_dev": "Застряла в разработке",
    "stuck_before_release": "Ждёт релиза",
    "waiting_for_test": "Очередь QA",
    "stuck_in_review": "Застряла на ревью",
    "paused": "На паузе",
    "unassigned": "Нет исполнителя",
    "missing_start_date": "Нет даты начала",
    "missing_story_points": "Нет story points",
    "due_date_at_risk": "Риск по сроку",
    "returned_from_qa": "Возврат из QA",
    "qa_phase_heavy": "Долго в QA",
    "excessive_pause_history": "Много пауз в истории",
    "status_churn": "Скачки по статусам",
    "assignee_overloaded": "Перегруз исполнителя",
    "handoff_stuck": "Застряла на передаче",
    "cross_team_block": "Блокировка другой командой",
    "dependency_stall": "Зависимость не двигается",
    "subtask_gap": "Подзадачи без активности",
    "no_subtasks": "Нет подзадач в работе",
    "subtask_bottleneck": "Узкое горлышко подзадач",
    "release_tail": "Хвост релиза",
}


def _is_done_issue(issue: dict[str, Any]) -> bool:
    from app.domain.product_radar_analytics import _is_done_issue as analytics_is_done

    return analytics_is_done(issue)


def _issue_story_points(issue: dict[str, Any]) -> float:
    value = issue.get("story_points")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return 0.0


def _person_name(issue: dict[str, Any]) -> str:
    for key in ("assignee", "developer", "current_status_assignee"):
        name = str(issue.get(key) or "").strip()
        if name:
            return name
    return ""


def _is_subtask_done(subtask: dict[str, Any]) -> bool:
    category = str(subtask.get("status_category") or "").lower()
    status = str(subtask.get("status") or "").strip().lower()
    if category == "done":
        return True
    return status in {"готово", "done", "closed", "resolved", "cancelled", "canceled"}


def _team_label(issue: dict[str, Any]) -> str:
    team = str(issue.get("team") or "").strip()
    if team:
        return team
    labels = issue.get("team_labels") or []
    if isinstance(labels, list) and labels:
        return str(labels[0] or "").strip()
    domain = str(issue.get("domain") or "").strip()
    return domain or "Без команды"


def _issues_by_key(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(issue.get("key") or ""): issue for issue in issues if str(issue.get("key") or "").strip()}


def _issue_status_days(issue: dict[str, Any], *, now: datetime | None = None) -> float:
    days = issue.get("current_status_days")
    if isinstance(days, (int, float)):
        return float(days)
    reference = now or datetime.now(timezone.utc)
    for field in ("status_entered_at", "status_changed_at", "updated"):
        raw = issue.get(field)
        if not raw:
            continue
        try:
            entered = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        return max(0.0, (reference - entered).total_seconds() / 86400.0)
    return 0.0


def build_issue_timeline(issue: dict[str, Any]) -> list[dict[str, Any]]:
    segments = issue.get("status_segments") if isinstance(issue.get("status_segments"), list) else []
    timeline: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        days = segment.get("duration_days")
        if days is None:
            days = segment.get("days")
        timeline.append(
            {
                "status": str(segment.get("status") or ""),
                "assignee": str(segment.get("assignee") or ""),
                "bucket": str(segment.get("bucket") or segment.get("flow_bucket") or ""),
                "days": float(days) if isinstance(days, (int, float)) else None,
                "started_at": segment.get("entered_at") or segment.get("started_at"),
                "ended_at": segment.get("left_at") or segment.get("ended_at"),
            }
        )
    return timeline


def normalize_radar_issue(raw: dict[str, Any]) -> dict[str, Any]:
    raw_subtasks = [item for item in (raw.get("subtasks") or []) if isinstance(item, dict)]
    raw_links = [item for item in (raw.get("issue_links") or []) if isinstance(item, dict)]
    raw_workload = raw.get("role_workload_items") if isinstance(raw.get("role_workload_items"), list) else []
    issue = normalize_scope_issue(raw)
    subtasks = raw_subtasks or [item for item in (issue.get("subtasks") or []) if isinstance(item, dict)]
    issue_links = raw_links or [item for item in (issue.get("issue_links") or []) if isinstance(item, dict)]
    if raw_workload:
        issue["role_workload_items"] = raw_workload
    timeline = build_issue_timeline(issue)
    issue["timeline"] = timeline
    issue["subtasks"] = subtasks
    issue["issue_links"] = issue_links
    issue["drilldown"] = {
        "team": _team_label(issue),
        "subtasks": subtasks,
        "issue_links": issue_links,
        "timeline": timeline,
        "role_workload_items": issue.get("role_workload_items") or [],
        "status_bucket_durations": issue.get("status_bucket_durations") or {},
    }
    return issue


def _link_team(link: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> str:
    team = str(link.get("team") or "").strip()
    if team:
        return team
    key = str(link.get("key") or "").strip()
    linked = lookup.get(key)
    if isinstance(linked, dict):
        return _team_label(linked)
    return ""


def collect_deep_radar_signals(
    issues: list[dict[str, Any]],
    *,
    issues_by_key: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    lookup = issues_by_key or _issues_by_key(issues)
    reference = now or datetime.now(timezone.utc)
    signals: list[dict[str, Any]] = []

    for issue in issues:
        if _is_done_issue(issue):
            continue
        key = str(issue.get("key") or "")
        blocked_team = _team_label(issue)
        status_days = _issue_status_days(issue, now=reference)

        for link in issue.get("issue_links") or []:
            if not isinstance(link, dict):
                continue
            relation = str(link.get("relation") or "").lower()
            if relation not in {"blocked_by", "blocks"} and "block" not in relation:
                continue
            blocker_key = str(link.get("key") or "")
            blocker = lookup.get(blocker_key, link)
            if isinstance(blocker, dict) and _is_done_issue(blocker):
                continue
            blocking_team = _link_team(link, lookup) or _link_team(blocker if isinstance(blocker, dict) else {}, lookup)
            if not blocking_team:
                blocking_team = "Неизвестная команда"
            if blocking_team and blocked_team and blocking_team != blocked_team:
                signals.append(
                    {
                        "kind": "cross_team_block",
                        "severity": "high",
                        "issue_key": key,
                        "summary": str(issue.get("summary") or key),
                        "issue_url": issue.get("url"),
                        "title": "Блокировка другой командой",
                        "detail": (
                            f"{key} ({blocked_team}) заблокирована {blocker_key} ({blocking_team}) "
                            f"в статусе «{link.get('status') or (blocker.get('status') if isinstance(blocker, dict) else '')}»."
                        ),
                        "blocking_team": blocking_team,
                        "blocked_team": blocked_team,
                        "blocker_key": blocker_key,
                        "blocker_team": blocking_team,
                        "blocker_status": str(link.get("status") or ""),
                        "days": round(status_days, 1),
                    }
                )

        subtasks = [item for item in (issue.get("subtasks") or []) if isinstance(item, dict)]
        if len(subtasks) >= 2:
            open_subtasks = [item for item in subtasks if not _is_subtask_done(item)]
            active_keys = {
                str(item.get("subtask_key") or "")
                for item in (issue.get("role_workload_items") or [])
                if isinstance(item, dict)
            }
            idle_open = [item for item in open_subtasks if str(item.get("key") or "") not in active_keys]
            if len(idle_open) >= max(1, len(subtasks) // 2):
                signals.append(
                    {
                        "kind": "subtask_gap",
                        "severity": "medium",
                        "issue_key": key,
                        "summary": str(issue.get("summary") or key),
                        "issue_url": issue.get("url"),
                        "title": "Подзадачи без активности",
                        "detail": f"{len(idle_open)} из {len(subtasks)} подзадач без движения у {key}.",
                        "days": round(status_days, 1),
                    }
                )

    return signals


def _classify_person_band(*, active_count: int, active_sp: float, backlog_count: int) -> PersonLoadBand:
    if active_count >= _LOADED_ACTIVE_MIN or active_sp >= _LOADED_SP_MIN:
        return "loaded"
    if active_count >= 1:
        return "normal"
    if backlog_count >= 1:
        return "idle"
    return "light"


def _health_from_signals(alerts: list[dict[str, Any]]) -> ProductRadarHealth:
    high = sum(1 for item in alerts if item.get("severity") == "high")
    medium = sum(1 for item in alerts if item.get("severity") == "medium")
    if high >= 3:
        return "critical"
    if high >= 1 or medium >= 5:
        return "attention"
    return "ok"


def _build_people_rows(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "name": "",
            "active_count": 0,
            "backlog_count": 0,
            "active_sp": 0.0,
            "backlog_sp": 0.0,
            "max_status_days": 0.0,
            "issue_keys": [],
        }
    )

    for issue in issues:
        if _is_done_issue(issue):
            continue
        name = _person_name(issue) or "Без исполнителя"
        bucket = classify_scope_report_bucket(issue)
        row = stats[name]
        row["name"] = name
        sp = _issue_story_points(issue)
        status_days = issue.get("current_status_days")
        if isinstance(status_days, (int, float)):
            row["max_status_days"] = max(row["max_status_days"], float(status_days))
        key = str(issue.get("key") or "")
        if key:
            row["issue_keys"].append(key)
        if bucket in {"in_work", "in_test"}:
            row["active_count"] += 1
            row["active_sp"] += sp
        elif bucket in {"not_started", "open_questions"}:
            row["backlog_count"] += 1
            row["backlog_sp"] += sp

    people: list[dict[str, Any]] = []
    for row in stats.values():
        band = _classify_person_band(
            active_count=int(row["active_count"]),
            active_sp=float(row["active_sp"]),
            backlog_count=int(row["backlog_count"]),
        )
        people.append(
            {
                **row,
                "load_band": band,
                "total_count": int(row["active_count"]) + int(row["backlog_count"]),
            }
        )

    band_rank = {"loaded": 0, "idle": 1, "normal": 2, "light": 3}
    people.sort(
        key=lambda item: (
            band_rank.get(str(item.get("load_band")), 9),
            -int(item.get("active_count") or 0),
            -float(item.get("active_sp") or 0),
            str(item.get("name") or ""),
        )
    )
    return people


def _build_triggers(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        kind = str(alert.get("kind") or "unknown")
        severity = str(alert.get("severity") or "low")
        entry = grouped.setdefault(
            kind,
            {
                "id": kind,
                "label": _TRIGGER_LABELS.get(kind, kind),
                "severity": severity,
                "count": 0,
                "issue_keys": [],
            },
        )
        entry["count"] += 1
        if severity == "high" or (severity == "medium" and entry["severity"] == "low"):
            entry["severity"] = severity
        issue_key = str(alert.get("issue_key") or "").strip()
        if issue_key and issue_key not in entry["issue_keys"]:
            entry["issue_keys"].append(issue_key)

    triggers = list(grouped.values())
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    triggers.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 9), -int(item.get("count") or 0)))
    return triggers


def compute_product_radar_snapshot(
    issues: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build radar snapshot: people workload, pipeline, signals, triggers."""
    reference = now or datetime.now(timezone.utc)
    active_issues = [issue for issue in issues if not _is_done_issue(issue)]
    issues_by_key = _issues_by_key(issues)
    people = _build_people_rows(issues)
    alerts = collect_product_flow_alerts(issues, now=reference)
    deep_alerts = collect_deep_radar_signals(issues, issues_by_key=issues_by_key, now=reference)
    all_signals = alerts + deep_alerts

    band_counts = {"loaded": 0, "normal": 0, "idle": 0, "light": 0}
    for person in people:
        band = str(person.get("load_band") or "light")
        if band in band_counts:
            band_counts[band] += 1

    high_count = sum(1 for item in all_signals if item.get("severity") == "high")

    analytics = compute_product_analytics(issues, all_signals, now=reference)

    attention_issues = sorted(
        [
            normalize_radar_issue(issue)
            for issue in issues
            if str(issue.get("key") or "") in {str(item.get("issue_key") or "") for item in all_signals if item.get("severity") == "high"}
        ],
        key=lambda item: str(item.get("key") or ""),
    )[:24]

    return {
        "refreshed_at": reference.isoformat(),
        "issue_count": len(issues),
        "active_count": len(active_issues),
        "health_status": _health_from_signals(all_signals),
        "people": people,
        "signals": all_signals,
        "triggers": _build_triggers(all_signals),
        "analytics": analytics,
        "issues": [normalize_radar_issue(issue) for issue in issues[:120]],
        "attention_issues": attention_issues,
        "summary": {
            "loaded_people": band_counts["loaded"],
            "idle_people": band_counts["idle"],
            "high_signals": high_count,
            "unassigned_active": sum(
                1
                for issue in active_issues
                if not _person_name(issue)
                and classify_scope_report_bucket(issue) in {"in_work", "in_test"}
            ),
        },
    }
