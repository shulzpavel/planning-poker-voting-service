"""Retrospective analytics for the discovery/preparation Jira stage."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

DISCOVERY = {"Исследование", "Описание от PO", "Ревью заказчика", "Согласовано маркетингом", "Дизайн"}
BACKLOG_SA = "backlog SA"
BOARD_STATUSES = ["Исследование", "Описание от PO", "Ревью заказчика", "Согласовано маркетингом", "Дизайн", BACKLOG_SA]
OLD_DAYS = 14


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _workdays(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0
    cursor = start
    total = 0.0
    while cursor.date() < end.date():
        if cursor.weekday() < 5:
            total += (cursor.replace(hour=23, minute=59, second=59) - cursor).total_seconds() / 86400
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if cursor.weekday() < 5:
        total += (end - cursor).total_seconds() / 86400
    if total == 0:
        has_weekday = any((start.date() + timedelta(days=offset)).weekday() < 5 for offset in range((end.date() - start.date()).days + 1))
        return 1.0 if has_weekday else 0.0
    return round(max(total, 1), 1)


def _stage_sla(issues: list[dict[str, Any]]) -> dict[str, Any]:
    durations: list[tuple[datetime, float]] = []
    for issue in issues:
        segments = issue.get("segments", [])
        start = next((segment for segment in segments if segment.get("status") == "Исследование"), None)
        finish = next((segment for segment in segments if segment.get("status") == BACKLOG_SA), None)
        if not start or not finish:
            continue
        entered = _at(start["entered_at"])
        durations.append((entered, _workdays(entered, _at(finish["entered_at"]))))
    values = sorted(value for _, value in durations)
    if not values:
        return {"completed_count": 0, "average_workdays": None, "p85_workdays": None, "trend": "flat", "recent_p85": None, "previous_p85": None}
    midpoint = max(1, len(durations) // 2)
    recent = sorted(value for entered, value in durations if entered >= sorted(entered for entered, _ in durations)[midpoint - 1])
    previous = sorted(value for entered, value in durations if entered < sorted(entered for entered, _ in durations)[midpoint - 1])
    recent_average = round(sum(recent) / len(recent), 1) if recent else round(sum(values) / len(values), 1)
    previous_average = round(sum(previous) / len(previous), 1) if previous else recent_average
    average = round(sum(values) / len(values), 1)
    recent_p85 = round(recent[max(0, int(len(recent) * .85) - 1)]) if recent else round(values[max(0, int(len(values) * .85) - 1)])
    previous_p85 = round(previous[max(0, int(len(previous) * .85) - 1)]) if previous else recent_p85
    p85 = round(values[max(0, int(len(values) * .85) - 1)])
    trend = "up" if recent_p85 > previous_p85 else "down" if recent_p85 < previous_p85 else "flat"
    return {"completed_count": len(values), "average_workdays": average, "p85_workdays": p85, "trend": trend, "recent_p85": recent_p85, "previous_p85": previous_p85}


def _current_age(issue: dict[str, Any], now: datetime) -> float | None:
    segments = issue.get("segments", [])
    if not segments or not issue.get("history_complete"):
        return None
    current = segments[-1]
    return round(max(0, (now - _at(current["entered_at"])).total_seconds() / 86400), 1)


def _owner_signal(stage_count: int, old_count: int) -> str:
    if stage_count >= 5 and old_count >= 2:
        return "high"
    if stage_count >= 3 or old_count >= 1:
        return "attention"
    return "watch"


def _owner_rows(issues: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        status = issue.get("status")
        if status in DISCOVERY or status == BACKLOG_SA:
            grouped[issue.get("assignee") or "Без исполнителя"].append(issue)
    rows = []
    for name, owned in grouped.items():
        active = [x for x in owned if x.get("status") in DISCOVERY]
        ages = [age for x in active if (age := _current_age(x, now)) is not None]
        status_counts: dict[str, int] = defaultdict(int)
        for item in active:
            status_counts[item["status"]] += 1
        old_count = sum(age >= OLD_DAYS for age in ages)
        rows.append({"name": name, "stage_count": len(active), "backlog_sa_count": sum(x.get("status") == BACKLOG_SA for x in owned),
                     "status_counts": dict(sorted(status_counts.items())), "ages_days": sorted(ages, reverse=True),
                     "old_count": old_count, "load_signal": _owner_signal(len(active), old_count)})
    return sorted(rows, key=lambda row: (-row["stage_count"], -row["old_count"], row["name"]))


def _attention_rows(issues: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows = []
    for issue in issues:
        if issue.get("status") not in DISCOVERY:
            continue
        age = _current_age(issue, now)
        if age is None or age < OLD_DAYS:
            continue
        rows.append({"key": issue["key"], "summary": issue["summary"], "assignee": issue.get("assignee") or "Без исполнителя",
                     "status": issue["status"], "age_days": age, "signal": "old_current_status"})
    return sorted(rows, key=lambda row: (-row["age_days"], row["key"]))


def _status_stats(issues: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    result = []
    for status in BOARD_STATUSES:
        durations = []
        current = 0
        for issue in issues:
            for segment in issue.get("segments", []):
                if segment.get("status") != status:
                    continue
                if segment.get("is_current"):
                    current += 1
                if segment.get("left_at"):
                    durations.append(max(0, (_at(segment["left_at"]) - _at(segment["entered_at"])).total_seconds() / 86400))
        ordered = sorted(durations)
        result.append({"status": status, "current": current, "visits": len(durations) + current,
                       "median_days": round(ordered[len(ordered) // 2], 2) if ordered else None,
                       "p85_days": round(ordered[max(0, int(len(ordered) * .85) - 1)], 2) if ordered else None,
                       "under_hour": sum(value < 1 / 24 for value in durations)})
    return result


def _transition_stats(issues: list[dict[str, Any]], now: datetime, days: int) -> dict[str, Any]:
    start = now - timedelta(days=days)
    order = {status: index for index, status in enumerate(BOARD_STATUSES)}
    transitions: Counter[tuple[str, str]] = Counter()
    returns = 0
    for issue in issues:
        for previous, current in zip(issue.get("segments", []), issue.get("segments", [])[1:]):
            if previous.get("status") not in order or current.get("status") not in order or not current.get("entered_at"):
                continue
            if _at(current["entered_at"]) < start:
                continue
            pair = (previous["status"], current["status"])
            transitions[pair] += 1
            returns += order[current["status"]] < order[previous["status"]]
    rows = [{"from": source, "to": target, "count": count, "return": order[target] < order[source]}
            for (source, target), count in transitions.most_common()]
    transition_count = sum(transitions.values())
    return {"rows": rows, "return_count": returns, "transition_count": transition_count,
            "return_rate": round(returns / transition_count * 100, 1) if transition_count else 0}


def build_discovery_snapshot(issues: list[dict[str, Any]], *, now: datetime, days: int) -> dict[str, Any]:
    if days not in {7, 30, 90}:
        raise ValueError("Unsupported dashboard window")
    now = now.astimezone(now.tzinfo)
    stage = [x for x in issues if x.get("status") in DISCOVERY]
    owners = _owner_rows(issues, now)
    attention = _attention_rows(issues, now)
    transitions = _transition_stats(issues, now, days)
    result = {"days": days, "generated_at": now.isoformat(), "owners": owners, "stage_sla": _stage_sla(issues),
              "attention": attention, "status_stats": _status_stats(issues, now), "transitions": transitions,
              "metrics": {"stage_count": len(stage),
              "backlog_sa_count": sum(x.get("status") == BACKLOG_SA for x in issues),
              "attention_count": len(attention), "owner_count": len(owners), "return_count": transitions["return_count"]},
              "coverage": {"complete": sum(bool(x.get("history_complete")) for x in issues), "total": len(issues)}}
    return result
