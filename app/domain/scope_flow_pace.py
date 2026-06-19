"""Flow pace metrics and stall alerts for epic scope boards (Plan/Unplan only)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, NamedTuple

from app.domain.scope_board import classify_scope_report_bucket, normalize_scope_team_slug

FlowPaceStatus = Literal["ok", "attention", "critical"]
FlowAlertSeverity = Literal["high", "medium", "low"]
FlowAlertKind = Literal[
    "stalled",
    "stuck_in_test",
    "stuck_in_dev",
    "stuck_before_release",
    "waiting_for_test",
    "stuck_in_review",
    "paused",
    "slow_throughput",
    "missing_start_date",
    "missing_story_points",
    "due_date_at_risk",
    "returned_from_qa",
    "qa_phase_heavy",
    "excessive_pause_history",
    "status_churn",
    "long_cycle",
    "unassigned",
    "test_heavy",
    "assignee_overloaded",
    "epic_stalled",
    "handoff_stuck",
]

FLOW_PACE_TEAM_SLUGS = frozenset({"igaming-rip"})
_PARENT_EPIC_JQL_RE = re.compile(r"\bparent\s*=\s*([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)

FLOW_PACE_TEAM_PROFILE = {
    "label": "iGaming Rip",
    "dev_count": 6,
    "qa_count": 4,
    "min_done_per_week": 4,
    "target_done_per_week": 8,
}

_THROUGHPUT_WINDOW_DAYS = 7
_DONE_STATUS_NAMES = frozenset({"готово", "done", "closed", "resolved", "cancelled", "canceled"})
_PAUSE_STATUS_KEYWORDS = ("пауз", "pause", "on hold", "blocked", "блок")
_DEV_CODING_STATUSES = frozenset({"in progress", "в работе", "in development"})
_STALL_ALERT_KINDS = frozenset(
    {
        "stalled",
        "stuck_in_test",
        "stuck_in_dev",
        "stuck_before_release",
        "waiting_for_test",
        "stuck_in_review",
        "paused",
    }
)
_HYGIENE_ALERT_KINDS = frozenset(
    {
        "missing_start_date",
        "missing_story_points",
        "due_date_at_risk",
    }
)


class _StatusStallProfile(NamedTuple):
    kind: FlowAlertKind
    title: str
    action: str
    highlight_role: str
    high_at: float
    medium_at: float
    low_at: float
    criteria: str


def _profile(
    kind: FlowAlertKind,
    title: str,
    action: str,
    highlight_role: str,
    *,
    high_at: float,
    medium_at: float,
    low_at: float,
) -> _StatusStallProfile:
    return _StatusStallProfile(
        kind,
        title,
        action,
        highlight_role,
        high_at,
        medium_at,
        low_at,
        f"High ≥{high_at:g}д · Medium ≥{medium_at:g}д · Low ≥{low_at:g}д",
    )


_STATUS_STALL_BY_NAME: dict[str, _StatusStallProfile] = {
    "тестирование": _profile(
        "stuck_in_test",
        "Зависла в тестировании",
        "Проверьте блокер QA или возврат в dev.",
        "qa",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "testing": _profile(
        "stuck_in_test",
        "Зависла в тестировании",
        "Проверьте блокер QA или возврат в dev.",
        "qa",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "in testing": _profile(
        "stuck_in_test",
        "Зависла в тестировании",
        "Проверьте блокер QA или возврат в dev.",
        "qa",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "к тестированию": _profile(
        "waiting_for_test",
        "Долго ждёт тестирования",
        "Задача в очереди QA — возьмите в работу или перераспределите.",
        "qa",
        high_at=5,
        medium_at=3,
        low_at=2,
    ),
    "ready for qa": _profile(
        "waiting_for_test",
        "Долго ждёт тестирования",
        "Задача в очереди QA — возьмите в работу или перераспределите.",
        "qa",
        high_at=5,
        medium_at=3,
        low_at=2,
    ),
    "к релизу": _profile(
        "stuck_before_release",
        "Долго ждёт релиза",
        "Нужен релиз или решение по блокеру перед выкладкой.",
        "release",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "to release": _profile(
        "stuck_before_release",
        "Долго ждёт релиза",
        "Нужен релиз или решение по блокеру перед выкладкой.",
        "release",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "ready for release": _profile(
        "stuck_before_release",
        "Долго ждёт релиза",
        "Нужен релиз или решение по блокеру перед выкладкой.",
        "release",
        high_at=7,
        medium_at=5,
        low_at=3,
    ),
    "in progress": _profile(
        "stuck_in_dev",
        "Долго в разработке",
        "Нужен push к QA или снятие блокера.",
        "dev",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
    "в работе": _profile(
        "stuck_in_dev",
        "Долго в разработке",
        "Нужен push к QA или снятие блокера.",
        "dev",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
    "ревью": _profile(
        "stuck_in_review",
        "Зависла на ревью",
        "Нужен approve или комментарии по MR.",
        "dev",
        high_at=5,
        medium_at=3,
        low_at=2,
    ),
    "code review": _profile(
        "stuck_in_review",
        "Зависла на ревью",
        "Нужен approve или комментарии по MR.",
        "dev",
        high_at=5,
        medium_at=3,
        low_at=2,
    ),
    "review": _profile(
        "stuck_in_review",
        "Зависла на ревью",
        "Нужен approve или комментарии по MR.",
        "dev",
        high_at=5,
        medium_at=3,
        low_at=2,
    ),
    "к выполнению": _profile(
        "stalled",
        "Не взята в работу",
        "Задача в бэклоге без движения — нужен owner или приоритет.",
        "lead",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
    "backlog": _profile(
        "stalled",
        "Не взята в работу",
        "Задача в бэклоге без движения — нужен owner или приоритет.",
        "lead",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
    "to do": _profile(
        "stalled",
        "Не взята в работу",
        "Задача в бэклоге без движения — нужен owner или приоритет.",
        "lead",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
    "todo": _profile(
        "stalled",
        "Не взята в работу",
        "Задача в бэклоге без движения — нужен owner или приоритет.",
        "lead",
        high_at=10,
        medium_at=7,
        low_at=5,
    ),
}


def _resolve_status_stall_profile(status: str, bucket: str) -> _StatusStallProfile | None:
    normalized = status.strip().lower()
    if normalized in _STATUS_STALL_BY_NAME:
        return _STATUS_STALL_BY_NAME[normalized]
    if bucket == "in_test":
        return _profile(
            "stuck_in_test",
            f"Долго в «{status.strip()}»",
            "Проверьте блокер в QA-фазе.",
            "qa",
            high_at=7,
            medium_at=5,
            low_at=3,
        )
    if bucket == "in_work":
        return _profile(
            "stuck_in_dev",
            f"Долго в «{status.strip()}»",
            "Нужен push к следующему этапу.",
            "dev",
            high_at=10,
            medium_at=7,
            low_at=5,
        )
    if bucket == "not_started":
        return _profile(
            "stalled",
            "Не взята в работу",
            f"«{status.strip()}» без движения — нужен owner или приоритет.",
            "lead",
            high_at=10,
            medium_at=7,
            low_at=5,
        )
    return None


def _stall_detail(*, status: str, age_days: float, highlight_name: str, action: str) -> str:
    status_label = status.strip() or "—"
    parts = [f"В статусе «{status_label}» уже {age_days:.0f} дн."]
    if highlight_name:
        parts.append(f"Ответственный: {highlight_name}.")
    parts.append(action)
    return " ".join(parts)


def _is_dev_coding_status(status: str) -> bool:
    return status.strip().lower() in _DEV_CODING_STATUSES


def _issue_story_points(issue: dict[str, Any]) -> float | None:
    sp = issue.get("story_points")
    if isinstance(sp, (int, float)) and sp > 0:
        return float(sp)
    return None


def _due_date_risk(issue: dict[str, Any], *, now: datetime) -> tuple[FlowAlertSeverity, float, str] | None:
    due = _parse_timestamp(issue.get("due_date"))
    if due is None:
        return None
    days_left = (due - now).total_seconds() / 86400.0
    if days_left < 0:
        overdue = abs(days_left)
        severity: FlowAlertSeverity = "high" if overdue >= 3 else "medium"
        return severity, overdue, f"Просрочена на {overdue:.0f} дн."
    if days_left <= 3:
        return "medium", days_left, f"До дедлайна {days_left:.0f} дн."
    if days_left <= 7:
        return "low", days_left, f"До дедлайна {days_left:.0f} дн."
    return None


def _status_transition_count(issue: dict[str, Any]) -> int:
    segments = issue.get("status_segments")
    if isinstance(segments, list) and segments:
        return len(segments)
    durations = _status_durations(issue)
    return len(durations)


def _dedupe_issue_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not alerts:
        return alerts
    kinds = {str(item.get("kind") or "") for item in alerts}
    has_stall = bool(kinds & _STALL_ALERT_KINDS)
    filtered: list[dict[str, Any]] = []
    for item in alerts:
        kind = str(item.get("kind") or "")
        if has_stall and kind in _HYGIENE_ALERT_KINDS:
            continue
        if kind == "qa_phase_heavy" and kinds & {"stuck_in_test", "stuck_before_release", "waiting_for_test"}:
            continue
        if has_stall and kind == "status_churn" and item.get("severity") == "low":
            continue
        filtered.append(item)
    filtered.sort(
        key=lambda item: (
            _severity_rank(str(item.get("severity"))),
            -(float(item.get("days") or 0)),
        )
    )
    return filtered[:2]


def _append_flow_insight_alerts(
    alerts: list[dict[str, Any]],
    *,
    issue: dict[str, Any],
    key: str,
    summary: str,
    status: str,
    age_days: float,
    bucket: str,
    epic_key: str,
    section_kind: str,
    section_name: str,
    highlight_name: str,
    highlight_role: str,
    durations: dict[str, float],
    issue_url: str,
    now: datetime,
) -> None:
    buckets = _bucket_durations(issue)

    if _is_dev_coding_status(status) and not _issue_started_at(issue) and age_days >= 3:
        severity = _pick_severity(age_days, high_at=14, medium_at=7, low_at=3)
        if severity:
            alerts.append(
                _alert(
                    kind="missing_start_date",
                    severity=severity,
                    issue_key=key,
                    summary=summary,
                    title="Нет даты начала",
                    detail=(
                        f"Поле «Дата начала» пустое уже {age_days:.0f} дн. в «{status}». "
                        "Без неё некорректен цикл и темп — проверьте автоматизацию Jira."
                    ),
                    criteria="High ≥14д · Medium ≥7д · Low ≥3д без «Даты начала»",
                    days=age_days,
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=highlight_role or "dev",
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    issue_url=issue_url,
                )
            )

    if bucket in {"in_work", "in_test"} and _issue_story_points(issue) is None and age_days >= 3:
        alerts.append(
            _alert(
                kind="missing_story_points",
                severity="low",
                issue_key=key,
                summary=summary,
                title="Нет story points",
                detail=(
                    f"Активная задача в «{status}» {age_days:.0f} дн. без SP — "
                    "оценка не зафиксирована, метрики capacity будут занижены."
                ),
                criteria="Low: активная задача ≥3д без SP",
                days=age_days,
                status=status,
                highlight_name=highlight_name,
                highlight_role=highlight_role or "lead",
                epic_key=epic_key,
                section_kind=section_kind,
                section_name=section_name,
                issue_url=issue_url,
            )
        )

    due_risk = _due_date_risk(issue, now=now)
    if due_risk:
        severity, days_value, label = due_risk
        alerts.append(
            _alert(
                kind="due_date_at_risk",
                severity=severity,
                issue_key=key,
                summary=summary,
                title="Риск по сроку",
                detail=f"{label} Текущий статус: «{status}».",
                criteria="High: просрочка ≥3д · Medium: ≤3д до дедлайна · Low: ≤7д",
                days=days_value,
                status=status,
                highlight_name=highlight_name,
                highlight_role=highlight_role or "lead",
                epic_key=epic_key,
                section_kind=section_kind,
                section_name=section_name,
                issue_url=issue_url,
            )
        )

    test_days = buckets.get("test", 0.0)
    dev_days = buckets.get("dev", 0.0)
    if bucket == "in_work" and test_days >= 2 and dev_days >= 0:
        severity = _pick_severity(test_days, high_at=10, medium_at=5, low_at=2)
        if severity:
            alerts.append(
                _alert(
                    kind="returned_from_qa",
                    severity=severity,
                    issue_key=key,
                    summary=summary,
                    title="Возврат из QA",
                    detail=(
                        f"Сейчас «{status}», но в QA-фазах уже {test_days:.0f} дн. "
                        f"(dev {dev_days:.1f} дн.) — вероятен возврат после тестирования."
                    ),
                    criteria="High ≥10д в QA · Medium ≥5д · Low ≥2д при текущем dev-статусе",
                    days=test_days,
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=highlight_role or "dev",
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    status_durations=durations,
                    issue_url=issue_url,
                )
            )

    if bucket == "in_test" and test_days >= 5 and dev_days > 0 and test_days > dev_days * 1.3:
        severity = _pick_severity(test_days, high_at=14, medium_at=8, low_at=5)
        if severity:
            alerts.append(
                _alert(
                    kind="qa_phase_heavy",
                    severity=severity,
                    issue_key=key,
                    summary=summary,
                    title="Долго в QA-фазе",
                    detail=(
                        f"Накоплено {test_days:.0f} дн. в тестировании/релизе vs {dev_days:.1f} дн. в dev. "
                        f"Сейчас «{status}» — возможны итерации QA."
                    ),
                    criteria="test ≥5д и test > 1.3× dev",
                    days=test_days,
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=highlight_role or "qa",
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    status_durations=durations,
                    issue_url=issue_url,
                )
            )

    pause_days = buckets.get("pause", 0.0)
    if pause_days >= 7 and not _is_paused_status(status.lower()):
        severity = _pick_severity(pause_days, high_at=21, medium_at=14, low_at=7)
        if severity:
            alerts.append(
                _alert(
                    kind="excessive_pause_history",
                    severity=severity,
                    issue_key=key,
                    summary=summary,
                    title="Много времени на паузах",
                    detail=(
                        f"Суммарно {pause_days:.0f} дн. в паузах по changelog. "
                        f"Сейчас «{status}» — проверьте повторяющиеся блокеры."
                    ),
                    criteria="High ≥21д · Medium ≥14д · Low ≥7д суммарно в pause",
                    days=pause_days,
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=highlight_role or "lead",
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    status_durations=durations,
                    issue_url=issue_url,
                )
            )

    transitions = _status_transition_count(issue)
    unique_statuses = len(_status_durations(issue))
    if transitions >= 8 or unique_statuses >= 6:
        churn_days = max(age_days, float(transitions))
        severity = _pick_severity(churn_days, high_at=20, medium_at=12, low_at=8)
        if severity:
            alerts.append(
                _alert(
                    kind="status_churn",
                    severity=severity if transitions >= 10 else "low",
                    issue_key=key,
                    summary=summary,
                    title="Частая смена статусов",
                    detail=(
                        f"{transitions} переходов, {unique_statuses} разных статусов. "
                        f"Сейчас «{status}» — нестабильный поток, нужен разбор причин."
                    ),
                    criteria="Low ≥8 переходов · Medium ≥12 · High ≥20",
                    days=float(transitions),
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=highlight_role or "lead",
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    status_durations=durations,
                    issue_url=issue_url,
                )
            )


def is_flow_pace_enabled(team_slug: str | None) -> bool:
    return normalize_scope_team_slug(team_slug) in FLOW_PACE_TEAM_SLUGS


def extract_parent_epic_from_jql(jql: str) -> str:
    match = _PARENT_EPIC_JQL_RE.search(jql or "")
    return match.group(1).upper() if match else ""


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


def _days_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 86400.0)


def _status_name(issue: dict[str, Any]) -> str:
    return str(issue.get("status") or "").strip().lower()


def _status_category(issue: dict[str, Any]) -> str:
    return str(issue.get("status_category") or "").strip().lower()


def _is_done_issue(issue: dict[str, Any]) -> bool:
    category = _status_category(issue)
    status = _status_name(issue)
    return category == "done" or status in _DONE_STATUS_NAMES


def _is_paused_status(status: str) -> bool:
    return any(token in status for token in _PAUSE_STATUS_KEYWORDS)


def _issue_resolved_at(issue: dict[str, Any]) -> datetime | None:
    if not _is_done_issue(issue):
        return None
    for field in ("resolution_date", "status_entered_at", "status_changed_at", "updated"):
        parsed = _parse_timestamp(issue.get(field))
        if parsed is not None:
            return parsed
    return None


def _issue_completed_at(issue: dict[str, Any]) -> datetime | None:
    return _issue_resolved_at(issue)


def _issue_started_at(issue: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(issue.get("start_date"))


def _issue_work_started_at(issue: dict[str, Any]) -> datetime | None:
    started = _issue_started_at(issue)
    if started is not None:
        return started
    segments = issue.get("status_segments")
    earliest: datetime | None = None
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            entered = _parse_timestamp(segment.get("entered_at"))
            if entered is not None and (earliest is None or entered < earliest):
                earliest = entered
    if earliest is not None:
        return earliest
    return _parse_timestamp(issue.get("created"))


def _issue_active_work_days(issue: dict[str, Any]) -> float | None:
    buckets = _bucket_durations(issue)
    if not buckets:
        return None
    total = sum(float(buckets.get(key) or 0.0) for key in ("dev", "test", "pause", "todo", "other"))
    return total if total > 0 else None


def _issue_cycle_days(issue: dict[str, Any]) -> float | None:
    started = _issue_work_started_at(issue)
    resolved = _issue_resolved_at(issue)
    calendar = _days_between(started, resolved)
    if calendar is not None:
        return calendar
    active = _issue_active_work_days(issue)
    if active is not None:
        return active
    return None


def _issue_status_age_days(issue: dict[str, Any], *, now: datetime) -> float | None:
    current_days = issue.get("current_status_days")
    if isinstance(current_days, (int, float)) and current_days >= 0:
        return float(current_days)
    entered = _parse_timestamp(issue.get("status_entered_at") or issue.get("status_changed_at") or issue.get("updated"))
    return _days_between(entered, now)


def _bucket_durations(issue: dict[str, Any]) -> dict[str, float]:
    raw = issue.get("status_bucket_durations")
    if isinstance(raw, dict):
        return {str(key): float(value) for key, value in raw.items() if isinstance(value, (int, float))}
    return {}


def _status_durations(issue: dict[str, Any]) -> dict[str, float]:
    raw = issue.get("status_durations")
    if isinstance(raw, dict):
        return {str(key): float(value) for key, value in raw.items() if isinstance(value, (int, float))}
    return {}


def _highlight_person(issue: dict[str, Any], *, bucket: str, status: str = "") -> tuple[str, str]:
    status_lower = status.strip().lower()
    role_contributors = issue.get("role_contributors") if isinstance(issue.get("role_contributors"), dict) else {}
    if status_lower in {"к релизу", "to release", "ready for release"} or bucket == "in_test":
        qa = role_contributors.get("qa") if isinstance(role_contributors.get("qa"), dict) else {}
        name = str(qa.get("name") or issue.get("current_status_assignee") or issue.get("assignee") or "").strip()
        role = "release" if status_lower in {"к релизу", "to release", "ready for release"} else "qa"
        return name, role
    if bucket == "in_work":
        for role in ("back", "front"):
            payload = role_contributors.get(role)
            if isinstance(payload, dict) and str(payload.get("name") or "").strip():
                return str(payload["name"]).strip(), role
        dev = str(issue.get("developer") or issue.get("current_status_assignee") or issue.get("assignee") or "").strip()
        return dev, "dev"
    assignee = str(issue.get("current_status_assignee") or issue.get("assignee") or "").strip()
    return assignee, "owner"


def _jira_browse_base(issues: list[dict[str, Any]]) -> str:
    for issue in issues:
        url = str(issue.get("url") or "").strip()
        if not url:
            continue
        marker = "/browse/"
        index = url.find(marker)
        if index > 0:
            return url[: index + len(marker.rstrip("/"))]
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}/browse"
        except ValueError:
            continue
    return ""


def _issue_browse_url(issue: dict[str, Any], *, browse_base: str = "") -> str:
    direct = str(issue.get("url") or "").strip()
    if direct:
        return direct
    key = str(issue.get("key") or "").strip()
    if key and browse_base:
        return f"{browse_base.rstrip('/')}/{key}"
    return ""


def _alert(
    *,
    kind: FlowAlertKind,
    severity: FlowAlertSeverity,
    issue_key: str,
    summary: str,
    title: str,
    detail: str,
    criteria: str,
    days: float | None = None,
    status: str = "",
    highlight_name: str = "",
    highlight_role: str = "",
    epic_key: str = "",
    section_kind: str = "",
    section_name: str = "",
    status_durations: dict[str, float] | None = None,
    issue_url: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "severity": severity,
        "issue_key": issue_key,
        "summary": summary,
        "title": title,
        "detail": detail,
        "criteria": criteria,
        "status": status,
        "highlight_name": highlight_name,
        "highlight_role": highlight_role,
        "epic_key": epic_key,
        "section_kind": section_kind,
        "section_name": section_name,
    }
    if issue_url:
        payload["issue_url"] = issue_url
    if days is not None:
        payload["days"] = round(days, 1)
    if status_durations:
        payload["status_durations"] = status_durations
    return payload


def _pick_severity(days: float, *, high_at: float, medium_at: float, low_at: float) -> FlowAlertSeverity | None:
    if days >= high_at:
        return "high"
    if days >= medium_at:
        return "medium"
    if days >= low_at:
        return "low"
    return None


def collect_flow_pace_scope(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect issues only from Plan/Unplan sections tied to parent epics."""
    scoped_issues: list[dict[str, Any]] = []
    epic_scope: list[dict[str, Any]] = []
    for section in snapshot.get("sections") or []:
        kind = str(section.get("kind") or "").strip().lower()
        if kind not in {"planned", "unplanned"}:
            continue
        section_id = str(section.get("id") or "")
        section_name = str(section.get("name") or section_id)
        jql = str(section.get("jql") or "")
        epic_key = extract_parent_epic_from_jql(jql) or ""
        epic_scope.append(
            {
                "epic_key": epic_key,
                "section_kind": kind,
                "section_name": section_name,
                "jql": jql,
            }
        )
        for issue in section.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            parent_key = str(issue.get("parent_key") or "").strip().upper()
            scoped_issues.append(
                {
                    **issue,
                    "flow_epic_key": epic_key or parent_key,
                    "flow_section_kind": kind,
                    "flow_section_name": section_name,
                }
            )
    return scoped_issues, epic_scope


def collect_flow_pace_issues(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    issues, _ = collect_flow_pace_scope(snapshot)
    return issues


def _issue_alerts(issue: dict[str, Any], *, now: datetime, browse_base: str = "") -> list[dict[str, Any]]:
    key = str(issue.get("key") or "")
    summary = str(issue.get("summary") or key)
    issue_url = _issue_browse_url(issue, browse_base=browse_base)
    status = str(issue.get("status") or "")
    status_lower = status.lower()
    epic_key = str(issue.get("flow_epic_key") or issue.get("parent_key") or "")
    section_kind = str(issue.get("flow_section_kind") or "")
    section_name = str(issue.get("flow_section_name") or "")
    durations = _status_durations(issue)
    alerts: list[dict[str, Any]] = []

    if _is_done_issue(issue):
        return alerts

    age_days = _issue_status_age_days(issue, now=now)
    if age_days is None:
        return alerts

    bucket = classify_scope_report_bucket(issue)
    highlight_name, highlight_role = _highlight_person(issue, bucket=bucket, status=status)

    if _is_paused_status(status_lower) or bucket == "open_questions":
        severity = _pick_severity(age_days, high_at=5, medium_at=3, low_at=2) or "low"
        pause_title = "На паузе" if _is_paused_status(status_lower) else "Открытые вопросы"
        alerts.append(
            _alert(
                kind="paused",
                severity=severity if _is_paused_status(status_lower) else "medium",
                issue_key=key,
                summary=summary,
                title=pause_title,
                detail=_stall_detail(
                    status=status,
                    age_days=age_days,
                    highlight_name=highlight_name,
                    action="Нужно снять блокер или вернуть задачу в работу.",
                ),
                criteria="High ≥5д · Medium ≥3д",
                days=age_days,
                status=status,
                highlight_name=highlight_name,
                highlight_role=highlight_role or "lead",
                epic_key=epic_key,
                section_kind=section_kind,
                section_name=section_name,
                status_durations=durations,
                issue_url=issue_url,
            )
        )
        return _dedupe_issue_alerts(alerts)

    if not str(issue.get("assignee") or issue.get("current_status_assignee") or "").strip():
        if bucket in {"in_work", "in_test"}:
            severity = _pick_severity(age_days, high_at=5, medium_at=3, low_at=1)
            if severity:
                alerts.append(
                    _alert(
                        kind="unassigned",
                        severity=severity,
                        issue_key=key,
                        summary=summary,
                        title="Нет исполнителя",
                        detail=_stall_detail(
                            status=status,
                            age_days=age_days,
                            highlight_name="",
                            action="Назначьте owner для текущего этапа.",
                        ),
                        criteria="High ≥5д · Medium ≥3д · Low ≥1д",
                        days=age_days,
                        status=status,
                        highlight_role="lead",
                        epic_key=epic_key,
                        section_kind=section_kind,
                        section_name=section_name,
                        status_durations=durations,
                        issue_url=issue_url,
                    )
                )

    stall_profile = _resolve_status_stall_profile(status, bucket)
    if stall_profile:
        severity = _pick_severity(
            age_days,
            high_at=stall_profile.high_at,
            medium_at=stall_profile.medium_at,
            low_at=stall_profile.low_at,
        )
        if severity:
            role = stall_profile.highlight_role or highlight_role
            alerts.append(
                _alert(
                    kind=stall_profile.kind,
                    severity=severity,
                    issue_key=key,
                    summary=summary,
                    title=stall_profile.title,
                    detail=_stall_detail(
                        status=status,
                        age_days=age_days,
                        highlight_name=highlight_name,
                        action=stall_profile.action,
                    ),
                    criteria=stall_profile.criteria,
                    days=age_days,
                    status=status,
                    highlight_name=highlight_name,
                    highlight_role=role,
                    epic_key=epic_key,
                    section_kind=section_kind,
                    section_name=section_name,
                    status_durations=durations,
                    issue_url=issue_url,
                )
            )

    _append_flow_insight_alerts(
        alerts,
        issue=issue,
        key=key,
        summary=summary,
        status=status,
        age_days=age_days,
        bucket=bucket,
        epic_key=epic_key,
        section_kind=section_kind,
        section_name=section_name,
        highlight_name=highlight_name,
        highlight_role=highlight_role,
        durations=durations,
        issue_url=issue_url,
        now=now,
    )
    return _dedupe_issue_alerts(alerts)


def _epic_browse_url(epic_key: str, *, browse_base: str) -> str:
    key = epic_key.strip()
    if not key or not browse_base:
        return ""
    return f"{browse_base.rstrip('/')}/{key}"


def _severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def _assignee_overload_alerts(issues: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for alert in alerts:
        if alert.get("severity") != "high":
            continue
        name = str(alert.get("highlight_name") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    extra: list[dict[str, Any]] = []
    for name, count in counts.items():
        if count < 3:
            continue
        severity: FlowAlertSeverity = "high" if count >= 4 else "medium"
        extra.append(
            _alert(
                kind="assignee_overloaded",
                severity=severity,
                issue_key="",
                summary="",
                title="Перегруз исполнителя",
                detail=f"У {name} {count} High-сигналов по задачам эпика — перераспределите WIP.",
                criteria="Medium ≥3 High · High ≥4 High на человека",
                highlight_name=name,
                highlight_role="lead",
            )
        )
    return extra


def _epic_stalled_alerts(
    epic_scope: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    done_last_7d_by_epic: dict[str, int],
    browse_base: str = "",
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for epic in epic_scope:
        epic_key = str(epic.get("epic_key") or "")
        if not epic_key:
            continue
        epic_issues = [issue for issue in issues if str(issue.get("flow_epic_key") or "") == epic_key]
        active = [issue for issue in epic_issues if not _is_done_issue(issue)]
        if not active:
            continue
        done_recent = done_last_7d_by_epic.get(epic_key, 0)
        if done_recent > 0:
            continue
        high_active = sum(
            1
            for issue in active
            if classify_scope_report_bucket(issue) in {"in_work", "in_test"}
        )
        if high_active < 2:
            continue
        severity: FlowAlertSeverity = "high" if high_active >= 4 else "medium"
        kind_label = "Plan" if epic.get("section_kind") == "planned" else "Unplan"
        alerts.append(
            _alert(
                kind="epic_stalled",
                severity=severity,
                issue_key=epic_key,
                summary=kind_label,
                title=f"Эпик {epic_key} не закрывает задачи",
                detail=(
                    f"{kind_label}: 0 закрытий за 7 дней при {len(active)} активных задачах. "
                    f"Проверьте приоритеты и блокеры."
                ),
                criteria="0 done/7д и ≥2 активных in dev/test",
                epic_key=epic_key,
                section_kind=str(epic.get("section_kind") or ""),
                section_name=str(epic.get("section_name") or ""),
                highlight_role="team",
                issue_url=_epic_browse_url(epic_key, browse_base=browse_base),
            )
        )
    return alerts


def _format_flow_date(value: Any) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%d.%m.%Y")


def _chart_detail_item(
    *,
    segment_key: str,
    issue_key: str = "",
    summary: str = "",
    metric_label: str = "",
    metric_value: str = "",
    detail: str = "",
    issue_url: str | None = None,
    alert: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "segment_key": segment_key,
        "issue_key": issue_key,
        "summary": summary,
        "metric_label": metric_label,
        "metric_value": metric_value,
        "detail": detail,
        "issue_url": issue_url,
    }
    if alert is not None:
        item["alert"] = alert
    return item


def _group_detail_segments(
    items: list[dict[str, Any]],
    segment_defs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in segment_defs}
    for item in items:
        key = str(item.get("segment_key") or "")
        if key in grouped:
            grouped[key].append(item)
    return [{"key": key, "label": label, "items": grouped.get(key, [])} for key, label in segment_defs]


def _cycle_source_label(issue: dict[str, Any]) -> str:
    if _issue_started_at(issue) is not None:
        return "дата начала"
    if isinstance(issue.get("status_segments"), list) and issue.get("status_segments"):
        return "первый статус changelog"
    if _parse_timestamp(issue.get("created")) is not None:
        return "created"
    return "сумма фаз changelog"


def _cycle_detail_text(issue: dict[str, Any]) -> str:
    started = _issue_work_started_at(issue)
    resolved = _issue_resolved_at(issue)
    source = _cycle_source_label(issue)
    if started is not None and resolved is not None:
        return f"{source}: {_format_flow_date(started)} → resolution {_format_flow_date(resolved)}"
    active = _issue_active_work_days(issue)
    if active is not None:
        return f"сумма фаз changelog: dev+test+pause+todo = {active:.1f} дн."
    return source


def _qa_iteration_bucket(dev_days: float, test_days: float) -> str | None:
    if dev_days <= 0 and test_days <= 0:
        return None
    if test_days >= 3 and test_days > dev_days * 1.3:
        return "qa_heavy"
    if dev_days > test_days * 1.3:
        return "dev_heavy"
    return "balanced"


def _donut_chart(
    chart_id: str,
    *,
    title: str,
    subtitle: str,
    center_value: str,
    center_label: str,
    segments: list[dict[str, Any]],
    methodology: str = "",
    detail_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    visible = [segment for segment in segments if float(segment.get("value") or 0) > 0]
    if not visible:
        visible = [{"key": "empty", "label": "Нет данных", "value": 1.0, "color": "#94a3b8"}]
    chart: dict[str, Any] = {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "center_value": center_value,
        "center_label": center_label,
        "segments": [
            {
                "key": str(segment.get("key") or ""),
                "label": str(segment.get("label") or ""),
                "value": round(float(segment.get("value") or 0), 1),
                "color": str(segment.get("color") or "#94a3b8"),
            }
            for segment in visible
        ],
    }
    if methodology:
        chart["methodology"] = methodology
    if detail_segments is not None:
        chart["detail_segments"] = detail_segments
    return chart


def _compute_flow_pace_charts(
    scoped_issues: list[dict[str, Any]],
    done_issues: list[dict[str, Any]],
    *,
    reference: datetime,
    done_last_7d: int,
    done_last_14d: int,
    target_done_per_week: int,
    browse_base: str = "",
    alerts: list[dict[str, Any]] | None = None,
    high_count: int = 0,
    medium_count: int = 0,
    low_count: int = 0,
) -> dict[str, Any]:
    plan_done = sum(1 for issue in done_issues if str(issue.get("flow_section_kind") or "") == "planned")
    unplan_done = sum(1 for issue in done_issues if str(issue.get("flow_section_kind") or "") == "unplanned")

    done_last_8_14d = 0
    done_older_14d = 0
    cycle_fast = 0
    cycle_normal = 0
    cycle_slow = 0
    cycle_values: list[float] = []
    qa_heavy_done = 0
    balanced_done = 0
    dev_heavy_done = 0
    phase_totals = {"dev": 0.0, "test": 0.0, "pause": 0.0, "other": 0.0}

    done_mix_items: list[dict[str, Any]] = []
    throughput_items: list[dict[str, Any]] = []
    cycle_items: list[dict[str, Any]] = []
    phase_items: list[dict[str, Any]] = []
    qa_items: list[dict[str, Any]] = []

    for issue in done_issues:
        key = str(issue.get("key") or "")
        summary = str(issue.get("summary") or "")
        issue_url = _issue_browse_url(issue, browse_base=browse_base) or None
        section_kind = str(issue.get("flow_section_kind") or "")

        if section_kind == "planned":
            done_mix_items.append(
                _chart_detail_item(
                    segment_key="plan",
                    issue_key=key,
                    summary=summary,
                    metric_label="Plan",
                    metric_value=str(issue.get("story_points") or "—"),
                    detail=str(issue.get("flow_section_name") or "Plan"),
                    issue_url=issue_url,
                )
            )
        elif section_kind == "unplanned":
            done_mix_items.append(
                _chart_detail_item(
                    segment_key="unplan",
                    issue_key=key,
                    summary=summary,
                    metric_label="Unplan",
                    metric_value=str(issue.get("story_points") or "—"),
                    detail=str(issue.get("flow_section_name") or "Unplan"),
                    issue_url=issue_url,
                )
            )

        completed = _issue_resolved_at(issue)
        if completed is not None:
            days_since_done = _days_between(completed, reference)
            if days_since_done is not None:
                if days_since_done <= 7:
                    throughput_bucket = "last_7d"
                elif days_since_done <= 14:
                    throughput_bucket = "last_14d"
                    done_last_8_14d += 1
                else:
                    throughput_bucket = "older"
                    done_older_14d += 1
                throughput_items.append(
                    _chart_detail_item(
                        segment_key=throughput_bucket,
                        issue_key=key,
                        summary=summary,
                        metric_label="Закрыта",
                        metric_value=f"{days_since_done:.0f} дн. назад",
                        detail=f"resolution {_format_flow_date(completed)}",
                        issue_url=issue_url,
                    )
                )

        cycle = _issue_cycle_days(issue)
        if cycle is not None:
            cycle_values.append(cycle)
            if cycle <= 7:
                cycle_bucket = "fast"
                cycle_fast += 1
            elif cycle <= 14:
                cycle_bucket = "normal"
                cycle_normal += 1
            else:
                cycle_bucket = "slow"
                cycle_slow += 1
            cycle_items.append(
                _chart_detail_item(
                    segment_key=cycle_bucket,
                    issue_key=key,
                    summary=summary,
                    metric_label="Цикл",
                    metric_value=f"{cycle:.1f} дн.",
                    detail=_cycle_detail_text(issue),
                    issue_url=issue_url,
                )
            )

        buckets = _bucket_durations(issue)
        dev_days = float(buckets.get("dev") or 0.0)
        test_days = float(buckets.get("test") or 0.0)
        pause_days = float(buckets.get("pause") or 0.0)
        other_days = (
            float(buckets.get("todo") or 0.0)
            + float(buckets.get("done") or 0.0)
            + float(buckets.get("other") or 0.0)
        )
        phase_detail = (
            f"Dev {dev_days:.1f} · Test {test_days:.1f} · Pause {pause_days:.1f} · Прочее {other_days:.1f} дн."
        )
        if dev_days > 0:
            phase_items.append(
                _chart_detail_item(
                    segment_key="dev",
                    issue_key=key,
                    summary=summary,
                    metric_label="Dev",
                    metric_value=f"{dev_days:.1f} дн.",
                    detail=phase_detail,
                    issue_url=issue_url,
                )
            )
        if test_days > 0:
            phase_items.append(
                _chart_detail_item(
                    segment_key="test",
                    issue_key=key,
                    summary=summary,
                    metric_label="Test/Release",
                    metric_value=f"{test_days:.1f} дн.",
                    detail=phase_detail,
                    issue_url=issue_url,
                )
            )
        if pause_days > 0:
            phase_items.append(
                _chart_detail_item(
                    segment_key="pause",
                    issue_key=key,
                    summary=summary,
                    metric_label="Пауза",
                    metric_value=f"{pause_days:.1f} дн.",
                    detail=phase_detail,
                    issue_url=issue_url,
                )
            )
        if other_days > 0:
            phase_items.append(
                _chart_detail_item(
                    segment_key="other",
                    issue_key=key,
                    summary=summary,
                    metric_label="Прочее",
                    metric_value=f"{other_days:.1f} дн.",
                    detail=phase_detail,
                    issue_url=issue_url,
                )
            )

        qa_bucket = _qa_iteration_bucket(dev_days, test_days)
        if qa_bucket == "qa_heavy":
            qa_heavy_done += 1
        elif qa_bucket == "dev_heavy":
            dev_heavy_done += 1
        elif qa_bucket == "balanced":
            balanced_done += 1
        if qa_bucket is not None:
            qa_items.append(
                _chart_detail_item(
                    segment_key=qa_bucket,
                    issue_key=key,
                    summary=summary,
                    metric_label="Dev / Test",
                    metric_value=f"{dev_days:.1f} / {test_days:.1f} дн.",
                    detail=(
                        "QA-heavy: test ≥3 дн. и test > dev×1.3"
                        if qa_bucket == "qa_heavy"
                        else "Dev-heavy: dev > test×1.3"
                        if qa_bucket == "dev_heavy"
                        else "Баланс dev/test"
                    ),
                    issue_url=issue_url,
                )
            )

        phase_totals["dev"] += dev_days
        phase_totals["test"] += test_days
        phase_totals["pause"] += pause_days
        phase_totals["other"] += other_days

    median_cycle = None
    if cycle_values:
        sorted_cycles = sorted(cycle_values)
        mid = len(sorted_cycles) // 2
        if len(sorted_cycles) % 2:
            median_cycle = round(sorted_cycles[mid], 1)
        else:
            median_cycle = round((sorted_cycles[mid - 1] + sorted_cycles[mid]) / 2, 1)

    qa_share = 0
    dev_test_total = phase_totals["dev"] + phase_totals["test"]
    if dev_test_total > 0:
        qa_share = round(phase_totals["test"] / dev_test_total * 100)

    cycle_subtitle = "Lead time закрытых"
    cycle_methodology = (
        "Lead time закрытых задач: от даты начала (или created/changelog) до resolution. "
        "Buckets: ≤7 / 8–14 / ≥15 дней."
    )
    if cycle_values:
        with_start = sum(1 for issue in done_issues if _issue_started_at(issue) is not None)
        if with_start >= len(done_issues) // 2:
            cycle_subtitle = "Дата начала → resolution"
        elif any(isinstance(issue.get("status_segments"), list) and issue.get("status_segments") for issue in done_issues):
            cycle_subtitle = "Changelog / создание → готово"
            cycle_methodology = (
                "Lead time: created или первый статус changelog → resolution. "
                "Если дат нет — сумма фаз dev+test+pause из changelog."
            )

    alert_items: list[dict[str, Any]] = []
    for alert in alerts or []:
        severity = str(alert.get("severity") or "low")
        if severity not in {"high", "medium", "low"}:
            continue
        days = alert.get("days")
        metric_value = f"{float(days):.1f} дн." if isinstance(days, (int, float)) else ""
        alert_items.append(
            _chart_detail_item(
                segment_key=severity,
                issue_key=str(alert.get("issue_key") or ""),
                summary=str(alert.get("summary") or ""),
                metric_label=str(alert.get("title") or "Сигнал"),
                metric_value=metric_value,
                detail=str(alert.get("detail") or ""),
                issue_url=str(alert.get("issue_url") or "") or None,
                alert=alert,
            )
        )

    total_signals = high_count + medium_count + low_count

    donuts = [
        _donut_chart(
            "done_mix",
            title="Закрыто Plan / Unplan",
            subtitle=f"{len(done_issues)} из {len(scoped_issues)} задач в scope",
            center_value=str(len(done_issues)),
            center_label="готово",
            segments=[
                {"key": "plan", "label": "Plan", "value": plan_done, "color": "#3b82f6"},
                {"key": "unplan", "label": "Unplan", "value": unplan_done, "color": "#f59e0b"},
            ],
            methodology="Закрытые (Готово) задачи Plan/Unplan эпиков из JQL parent=FLEX-2861/2862.",
            detail_segments=_group_detail_segments(
                done_mix_items,
                [("plan", "Plan"), ("unplan", "Unplan")],
            ),
        ),
        _donut_chart(
            "throughput",
            title="Темп закрытия",
            subtitle=f"Цель {target_done_per_week}/7д · факт {done_last_7d}",
            center_value=str(done_last_7d),
            center_label="за 7д",
            segments=[
                {"key": "last_7d", "label": "≤7 дней", "value": done_last_7d, "color": "#10b981"},
                {"key": "last_14d", "label": "8–14 дней", "value": done_last_8_14d, "color": "#06b6d4"},
                {"key": "older", "label": ">14 дней", "value": done_older_14d, "color": "#94a3b8"},
            ],
            methodology=(
                "Когда задача перешла в Готово (resolution_date), считаем сколько дней назад относительно сейчас. "
                f"Цель команды — {target_done_per_week} закрытий за 7 дней."
            ),
            detail_segments=_group_detail_segments(
                throughput_items,
                [("last_7d", "≤7 дней"), ("last_14d", "8–14 дней"), ("older", ">14 дней")],
            ),
        ),
        _donut_chart(
            "cycle_time",
            title="Цикл закрытых",
            subtitle=cycle_subtitle,
            center_value=str(median_cycle if median_cycle is not None else "—"),
            center_label="мед. дн.",
            segments=[
                {"key": "fast", "label": "≤7 дн.", "value": cycle_fast, "color": "#10b981"},
                {"key": "normal", "label": "8–14 дн.", "value": cycle_normal, "color": "#f59e0b"},
                {"key": "slow", "label": "≥15 дн.", "value": cycle_slow, "color": "#ef4444"},
            ],
            methodology=cycle_methodology,
            detail_segments=_group_detail_segments(
                cycle_items,
                [("fast", "≤7 дн."), ("normal", "8–14 дн."), ("slow", "≥15 дн.")],
            ),
        ),
        _donut_chart(
            "phase_time",
            title="Время в фазах",
            subtitle=f"QA-доля {qa_share}% · только закрытые",
            center_value=f"{qa_share}%",
            center_label="QA",
            segments=[
                {"key": "dev", "label": "Dev", "value": round(phase_totals["dev"], 1), "color": "#6366f1"},
                {"key": "test", "label": "Test/Release", "value": round(phase_totals["test"], 1), "color": "#8b5cf6"},
                {"key": "pause", "label": "Пауза", "value": round(phase_totals["pause"], 1), "color": "#94a3b8"},
                {"key": "other", "label": "Прочее", "value": round(phase_totals["other"], 1), "color": "#cbd5e1"},
            ],
            methodology=(
                "Сумма дней в статусах по bucket из Jira changelog (dev / test+release / pause / прочее). "
                "Задача может быть в нескольких сегментах, если провела время в нескольких фазах."
            ),
            detail_segments=_group_detail_segments(
                phase_items,
                [
                    ("dev", "Dev"),
                    ("test", "Test/Release"),
                    ("pause", "Пауза"),
                    ("other", "Прочее"),
                ],
            ),
        ),
        _donut_chart(
            "qa_iterations",
            title="QA-итерации",
            subtitle="Закрытые: test ≫ dev · баланс · dev ≫ test",
            center_value=str(qa_heavy_done),
            center_label="QA-heavy",
            segments=[
                {"key": "qa_heavy", "label": "QA-heavy", "value": qa_heavy_done, "color": "#8b5cf6"},
                {"key": "balanced", "label": "Баланс", "value": balanced_done, "color": "#10b981"},
                {"key": "dev_heavy", "label": "Dev-heavy", "value": dev_heavy_done, "color": "#6366f1"},
            ],
            methodology=(
                "QA-heavy: test ≥3 дн. и test > dev×1.3. Dev-heavy: dev > test×1.3. Иначе — баланс. "
                "Дни из status_bucket_durations changelog."
            ),
            detail_segments=_group_detail_segments(
                qa_items,
                [
                    ("qa_heavy", "QA-heavy"),
                    ("balanced", "Баланс"),
                    ("dev_heavy", "Dev-heavy"),
                ],
            ),
        ),
        _donut_chart(
            "active_signals",
            title="Сигналы",
            subtitle=f"High {high_count} · Medium {medium_count} · Low {low_count}",
            center_value=str(total_signals),
            center_label="сигналов",
            segments=[
                {"key": "high", "label": "High", "value": high_count, "color": "#ef4444"},
                {"key": "medium", "label": "Medium", "value": medium_count, "color": "#f59e0b"},
                {"key": "low", "label": "Low", "value": low_count, "color": "#3b82f6"},
            ],
            methodology=(
                "Сигналы по активным (не Готово) задачам Plan/Unplan: зависания в статусах, риски сроков, "
                "перегруз исполнителей, пауза эпика без закрытий. Закрытые задачи не попадают в список."
            ),
            detail_segments=_group_detail_segments(
                alert_items,
                [("high", "High"), ("medium", "Medium"), ("low", "Low")],
            ),
        ),
    ]

    return {"donuts": donuts}


def compute_scope_flow_pace(
    snapshot: dict[str, Any],
    *,
    team_slug: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build flow-pace summary and alerts for enabled teams (Plan/Unplan epics only)."""
    if not is_flow_pace_enabled(team_slug):
        return None

    reference = now or datetime.now(timezone.utc)
    scoped_issues, epic_scope = collect_flow_pace_scope(snapshot)
    browse_base = _jira_browse_base(scoped_issues)

    done_issues: list[dict[str, Any]] = []
    active_issues: list[dict[str, Any]] = []
    for issue in scoped_issues:
        if _is_done_issue(issue):
            done_issues.append(issue)
        elif _status_category(issue) != "done":
            active_issues.append(issue)

    done_last_7d = 0
    done_sp_last_7d = 0.0
    done_last_14d = 0
    done_last_7d_by_epic: dict[str, int] = {}
    cycle_days: list[float] = []

    for issue in done_issues:
        completed = _issue_completed_at(issue)
        if completed is None:
            continue
        epic_key = str(issue.get("flow_epic_key") or "")
        days_since_done = _days_between(completed, reference)
        if days_since_done is not None and days_since_done <= _THROUGHPUT_WINDOW_DAYS:
            done_last_7d += 1
            if epic_key:
                done_last_7d_by_epic[epic_key] = done_last_7d_by_epic.get(epic_key, 0) + 1
            sp = issue.get("story_points")
            if isinstance(sp, (int, float)) and sp > 0:
                done_sp_last_7d += float(sp)
        if days_since_done is not None and days_since_done <= 14:
            done_last_14d += 1
        cycle = _issue_cycle_days(issue)
        if cycle is not None:
            cycle_days.append(cycle)

    avg_cycle_days = round(sum(cycle_days) / len(cycle_days), 1) if cycle_days else None
    median_cycle_days = None
    if cycle_days:
        sorted_cycles = sorted(cycle_days)
        mid = len(sorted_cycles) // 2
        if len(sorted_cycles) % 2:
            median_cycle_days = round(sorted_cycles[mid], 1)
        else:
            median_cycle_days = round((sorted_cycles[mid - 1] + sorted_cycles[mid]) / 2, 1)

    in_work = sum(1 for issue in active_issues if classify_scope_report_bucket(issue) == "in_work")
    in_test = sum(1 for issue in active_issues if classify_scope_report_bucket(issue) == "in_test")
    not_started = sum(1 for issue in active_issues if classify_scope_report_bucket(issue) == "not_started")

    alerts: list[dict[str, Any]] = []
    for issue in scoped_issues:
        alerts.extend(_issue_alerts(issue, now=reference, browse_base=browse_base))
    alerts.extend(_assignee_overload_alerts(scoped_issues, alerts))
    alerts.extend(
        _epic_stalled_alerts(epic_scope, scoped_issues, done_last_7d_by_epic=done_last_7d_by_epic, browse_base=browse_base)
    )

    profile = FLOW_PACE_TEAM_PROFILE
    min_done = profile["min_done_per_week"]
    target_done = profile["target_done_per_week"]
    if done_last_7d < min_done and len(active_issues) >= 3:
        alerts.append(
            _alert(
                kind="slow_throughput",
                severity="high" if done_last_7d == 0 else "medium",
                issue_key="",
                summary="",
                title="Медленный темп закрытия",
                detail=(
                    f"Plan+Unplan: за 7 дней закрыто {done_last_7d} задач "
                    f"(норма ≥{min_done}, цель {target_done})."
                ),
                criteria=f"High: 0 done/7д · Medium: <{min_done} done/7д",
                highlight_role="team",
            )
        )

    alerts.sort(
        key=lambda item: (
            _severity_rank(str(item.get("severity"))),
            -(float(item.get("days") or 0)),
            str(item.get("issue_key")),
        )
    )

    high_count = sum(1 for item in alerts if item.get("severity") == "high")
    medium_count = sum(1 for item in alerts if item.get("severity") == "medium")
    low_count = sum(1 for item in alerts if item.get("severity") == "low")

    pace_status: FlowPaceStatus = "ok"
    if high_count >= 3 or (high_count >= 1 and done_last_7d == 0 and len(active_issues) >= 5):
        pace_status = "critical"
    elif high_count >= 1 or medium_count >= 3 or done_last_7d < min_done:
        pace_status = "attention"

    return {
        "enabled": True,
        "team_slug": normalize_scope_team_slug(team_slug),
        "pace_status": pace_status,
        "jira_browse_base": browse_base,
        "team_profile": profile,
        "epic_scope": epic_scope,
        "charts": _compute_flow_pace_charts(
            scoped_issues,
            done_issues,
            reference=reference,
            done_last_7d=done_last_7d,
            done_last_14d=done_last_14d,
            target_done_per_week=target_done,
            browse_base=browse_base,
            alerts=alerts,
            high_count=high_count,
            medium_count=medium_count,
            low_count=low_count,
        ),
        "summary": {
            "total": len(scoped_issues),
            "done": len(done_issues),
            "active": len(active_issues),
            "in_work": in_work,
            "in_test": in_test,
            "not_started": not_started,
            "done_last_7d": done_last_7d,
            "done_sp_last_7d": round(done_sp_last_7d, 1),
            "done_last_14d": done_last_14d,
            "avg_cycle_days": avg_cycle_days,
            "median_cycle_days": median_cycle_days,
            "target_done_per_week": target_done,
            "min_done_per_week": min_done,
            "high_count": high_count,
            "medium_count": medium_count,
            "low_count": low_count,
        },
        "alerts": alerts[:40],
        "hints": _flow_pace_hints(pace_status, done_last_7d, in_test, high_count, medium_count, epic_scope),
        "computed_at": reference.isoformat(),
    }


def _flow_pace_hints(
    pace_status: FlowPaceStatus,
    done_last_7d: int,
    in_test: int,
    high_count: int,
    medium_count: int,
    epic_scope: list[dict[str, Any]],
) -> list[str]:
    hints: list[str] = []
    profile = FLOW_PACE_TEAM_PROFILE
    epic_labels = ", ".join(
        f"{item.get('section_name') or item.get('section_kind')} ({item.get('epic_key') or '—'})"
        for item in epic_scope
    )
    hints.append(
        f"Оцениваем только Plan/Unplan эпики отчёта: {epic_labels or 'не заданы parent=FLEX-… в JQL'}."
    )
    hints.append(
        f"Команда {profile['label']}: {profile['dev_count']} dev + {profile['qa_count']} QA. "
        f"Сигналы High/Medium/Low по времени в статусах из Jira changelog."
    )
    hints.append("Подсветка исполнителя: dev/back/front в разработке, QA в тестировании, lead — пауза и unassigned.")
    hints.append("Сигналы только по активным задачам — закрытые (Готово) не попадают в список.")
    if in_test >= profile["qa_count"]:
        hints.append(f"В тестировании {in_test} задач при {profile['qa_count']} QA — проверьте WIP-лимит.")
    if pace_status == "critical":
        hints.append(f"Сначала разберите {high_count} High-сигнал(ов), затем Medium ({medium_count}).")
    elif pace_status == "attention":
        hints.append(f"Темп {done_last_7d}/7д vs цель {profile['target_done_per_week']}.")
    return hints


FLOW_PACE_CHART_IDS: tuple[str, ...] = (
    "done_mix",
    "throughput",
    "cycle_time",
    "phase_time",
    "qa_iterations",
    "active_signals",
)
FLOW_PACE_CHART_ID_SET = frozenset(FLOW_PACE_CHART_IDS)


def normalize_flow_pace_chart_order(chart_order: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in chart_order or []:
        key = str(item).strip()
        if not key or key not in FLOW_PACE_CHART_ID_SET or key in seen:
            continue
        cleaned.append(key)
        seen.add(key)
    for key in FLOW_PACE_CHART_IDS:
        if key not in seen:
            cleaned.append(key)
    return cleaned


def reorder_flow_pace_charts(charts: dict[str, Any] | None, chart_order: list[str] | None) -> dict[str, Any]:
    if not isinstance(charts, dict):
        return {"donuts": []}
    donuts = charts.get("donuts")
    if not isinstance(donuts, list):
        return charts
    normalized = normalize_flow_pace_chart_order(chart_order)
    by_id = {str(item.get("id") or ""): item for item in donuts if isinstance(item, dict) and item.get("id")}
    reordered = [by_id[key] for key in normalized if key in by_id]
    return {**charts, "donuts": reordered}


def apply_flow_pace_chart_order(flow_pace: dict[str, Any] | None, chart_order: list[str] | None) -> dict[str, Any] | None:
    if not isinstance(flow_pace, dict):
        return flow_pace
    charts = flow_pace.get("charts")
    if not isinstance(charts, dict):
        return flow_pace
    normalized = normalize_flow_pace_chart_order(chart_order)
    return {
        **flow_pace,
        "chart_order": normalized,
        "charts": reorder_flow_pace_charts(charts, normalized),
    }
