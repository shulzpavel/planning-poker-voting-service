"""Product intake analytics from normalized visit records."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Protocol


class ReportVisit(Protocol):
    key: str
    entered: datetime
    left: datetime | None
    outcome: str | None
    returned: bool

    def duration(self, now: datetime) -> float: ...


def _buckets(values: list[tuple[str, float | None]], *, current: bool) -> list[dict[str, Any]]:
    bounds = [(3, "recent", "До 3 дней"), (7, "week", "От 3 до 7 дней"),
              (float("inf"), "old", "Больше недели")] if current else [
                  (1, "day", "До одного дня"), (3, "three", "От 1 до 3 дней"),
                  (7, "week", "От 3 до 7 дней"), (float("inf"), "old", "Больше недели")]
    rows = [{"id": code, "label": label, "count": 0, "keys": []} for _, code, label in bounds]
    unknown: list[str] = []
    for key, duration in values:
        if duration is None:
            unknown.append(key)
            continue
        index = next(i for i, (limit, _, _) in enumerate(bounds) if duration <= limit)
        rows[index]["keys"].append(key)
        rows[index]["count"] += 1
    if unknown:
        rows.append({"id": "unknown", "label": "Нет полной истории", "count": len(unknown), "keys": unknown})
    return rows


def _outcomes(keys: list[str], by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    labels = [("forward", "Передали дальше"), ("deferred", "Отложили"),
              ("rejected", "Отклонили"), ("waiting", "Ещё ждут решения")]
    rows = {code: {"id": code, "label": label, "count": 0, "keys": []} for code, label in labels}
    for key in keys:
        status = by_key[key]["status"]
        code = {"Отложено": "deferred", "Отклонено": "rejected", "Backlog": "waiting",
                "Новый запрос": "waiting", "Нужна информация": "waiting"}.get(status, "forward")
        rows[code]["count"] += 1
        rows[code]["keys"].append(key)
    return list(rows.values())


def _sources(keys: list[str], by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for key in keys:
        label = str(by_key[key].get("source") or "").strip() or "Не указан"
        row = rows.setdefault(label, {"id": f"source:{label}", "label": label, "count": 0, "keys": []})
        row["count"] += 1
        row["keys"].append(key)
    return sorted(rows.values(), key=lambda row: (row["label"] == "Не указан", -row["count"], row["label"].casefold()))


def _weekly(first: list[ReportVisit], start: datetime, now: datetime) -> list[dict[str, Any]]:
    cursor = start - timedelta(days=start.weekday())
    rows = []
    while cursor < now:
        left, right = max(start, cursor), min(now, cursor + timedelta(days=7))
        received = [v.key for v in first if left <= v.entered < right]
        decided = [v.key for v in first if v.left is not None and left <= v.left < right]
        rows.append({"start": left.date().isoformat(), "end": (right - timedelta(microseconds=1)).date().isoformat(),
                     "received": len(received), "decided": len(decided),
                     "received_keys": received, "decided_keys": decided})
        cursor += timedelta(days=7)
    return rows


def build_intake_report(issues: list[dict[str, Any]], visits: list[ReportVisit], *,
                        start: datetime, now: datetime, aging: list[dict[str, Any]],
                        request_rows: list[dict[str, Any]]) -> dict[str, Any]:
    first_by_key: dict[str, ReportVisit] = {}
    for visit in sorted(visits, key=lambda v: v.entered):
        first_by_key.setdefault(visit.key, visit)
    first = list(first_by_key.values())
    received = [v.key for v in first if start <= v.entered < now]
    decided = [v for v in first if v.left is not None and start <= v.left < now]
    durations = [v.duration(now) for v in decided]
    returned = {v.key for v in visits if v.returned and start <= v.entered < now}
    by_key = {x["key"]: x for x in issues}
    return {
        "received": len(received), "received_keys": received,
        "decided": len(decided), "decided_keys": [v.key for v in decided],
        "typical_days": round(median(durations), 3) if durations else None,
        "returned": len(returned), "returned_keys": sorted(returned),
        "outcomes": _outcomes(received, by_key),
        "sources": _sources(received, by_key),
        "resolution_bands": _buckets([(v.key, v.duration(now)) for v in decided], current=False),
        "current_age": _buckets([(x["key"], x["age_days"]) for x in aging], current=True),
        "queue_start": sum(v.entered < start and (v.left is None or v.left >= start) for v in visits),
        "queue_end": len(aging),
        "needs_information": sum(x["status"] == "Нужна информация" for x in aging),
        "unassigned": sum(not x.get("assignee") for x in aging),
        "older_week": sum(x["age_days"] is not None and x["age_days"] > 7 for x in aging),
        "weekly": _weekly(first, start, now),
        "requests": request_rows,
    }

