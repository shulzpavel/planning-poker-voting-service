"""Intake analytics from normalized Jira timeline segments; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo
from app.domain.intake_report import build_intake_report

ZONE = ZoneInfo("Europe/Belgrade")
INTAKE = {"Backlog", "Новый запрос", "Нужна информация"}


@dataclass
class IntakeVisit:
    key: str
    entered: datetime
    left: datetime | None
    outcome: str | None
    returned: bool

    def duration(self, now: datetime) -> float:
        return ((self.left or now) - self.entered).total_seconds() / 86400


def _at(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return round(sorted(values)[ceil(len(values) * fraction) - 1], 2)


def _visits(issue: dict[str, Any]) -> list[IntakeVisit]:
    if not issue.get("history_complete"):
        return []
    visits: list[IntakeVisit] = []
    entered: datetime | None = None
    segments = issue.get("segments", [])
    for part in segments:
        status = part["status"]
        at = _at(part["entered_at"])
        if status in INTAKE and entered is None:
            entered = at
        elif status not in INTAKE and entered is not None:
            visits.append(IntakeVisit(issue["key"], entered, at, status, bool(visits)))
            entered = None
    if entered is not None:
        visits.append(IntakeVisit(issue["key"], entered, None, None, bool(visits)))
    return visits


def _visit_row(visit: IntakeVisit, issue: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {"key": visit.key, "summary": issue["summary"], "status": issue["status"],
            "assignee": issue.get("assignee"), "source": issue.get("source") or "Не указан",
            "entered_at": visit.entered.isoformat(), "left_at": visit.left.isoformat() if visit.left else None,
            "age_days": round(visit.duration(now), 2), "outcome": visit.outcome, "returned": visit.returned}


def _daily(visits: list[IntakeVisit], start: datetime, now: datetime, days: int) -> list[dict[str, Any]]:
    result = []
    for index in range(days):
        day = start + timedelta(days=index)
        end = min(day + timedelta(days=1), now)
        result.append({"date": day.date().isoformat(),
                       "arrivals": sum(day <= v.entered < end for v in visits),
                       "decisions": sum(v.left is not None and day <= v.left < end for v in visits),
                       "queue": sum(v.entered < end and (v.left is None or v.left >= end) for v in visits)})
    return result


def _aging(issues: list[dict[str, Any]], visits: list[IntakeVisit], now: datetime) -> list[dict[str, Any]]:
    current = {v.key: v for v in visits if v.left is None}
    rows = []
    for issue in issues:
        if issue["status"] not in INTAKE:
            continue
        if issue["key"] in current:
            rows.append(_visit_row(current[issue["key"]], issue, now))
        else:
            rows.append({"key": issue["key"], "summary": issue["summary"], "status": issue["status"],
                         "assignee": issue.get("assignee"), "age_days": None, "source": issue.get("source") or "Не указан"})
    return sorted(rows, key=lambda x: x["age_days"] if x["age_days"] is not None else -1, reverse=True)


def _insights(metrics: dict[str, Any], days: int, complete: int, total: int) -> dict[str, str]:
    arrivals, decisions = metrics["arrivals"], metrics["decisions"]
    balance = arrivals - decisions
    movement = f"увеличилась на {balance}" if balance > 0 else f"сократилась на {abs(balance)}" if balance < 0 else "не изменилась"
    coverage = " История загружена не полностью; вывод предварительный." if complete < total else ""
    flow = f"За {days} дней — {arrivals} входов и {decisions} решений. По восстановленной истории очередь {movement}.{coverage}"
    duration = (f"85% завершённых прохождений заняли не больше {metrics['p85_days']:g} дн. "
                "Открытые запросы в этот расчёт не включены.") if metrics["p85_days"] is not None else "В выбранном периоде нет завершённых прохождений с полной историей."
    baseline = (f"Исторический ориентир — {metrics['sle_days']:g} дн. Он рассчитан до выбранного периода и не является SLA."
                if metrics["sle_days"] is not None else f"Для ориентира нужно 20 завершений до начала периода; сейчас {metrics['baseline_n']}. SLA пока не задан.")
    aging = f"Сейчас решения ждут {metrics['pending']} запросов. Возраст считается с входа в стадию; комментарии его не обнуляют."
    queue = f"Сейчас в очереди {metrics['pending']} запросов. Ожидание информации включено в общий срок обработки."
    return {"flow": flow, "queue": queue, "duration": duration, "baseline": baseline, "aging": aging}


def build_intake_snapshot(issues: list[dict[str, Any]], *, now: datetime, days: int) -> dict[str, Any]:
    if days not in {7, 30, 90}:
        raise ValueError("Unsupported dashboard window")
    now = now.astimezone(ZONE)
    start = datetime.combine(now.date() - timedelta(days=days - 1), time.min, ZONE)
    visits = [v for issue in issues for v in _visits(issue)]
    by_key = {x["key"]: x for x in issues}
    completed = [v for v in visits if v.left is not None and start <= v.left < now]
    baseline = [v.duration(now) for v in visits if v.left is not None and start - timedelta(days=90) <= v.left < start]
    durations = [v.duration(now) for v in completed]
    sle = _quantile(baseline, .85) if len(baseline) >= 20 else None
    aging = _aging(issues, visits, now)
    metrics = {"arrivals": sum(start <= v.entered < now for v in visits), "decisions": len(completed),
               "pending": len(aging), "p50_days": _quantile(durations, .5), "p85_days": _quantile(durations, .85),
               "sle_days": sle, "within_sle_pct": round(100 * sum(d <= sle for d in durations) / len(durations), 1) if sle is not None and durations else None,
               "over_sle": sum(x["age_days"] is not None and x["age_days"] > sle for x in aging) if sle is not None else None,
               "baseline_n": len(baseline)}
    complete = sum(bool(x.get("history_complete")) for x in issues)
    first = {v.key: v for v in reversed(visits)}
    request_rows = [_visit_row(v, by_key[v.key], now) for v in first.values()]
    report = build_intake_report(issues, visits, start=start, now=now, aging=aging, request_rows=request_rows)
    return {"days": days, "generated_at": now.isoformat(), "window_start": start.isoformat(), "metrics": metrics,
            "coverage": {"complete": complete, "total": len(issues)}, "daily": _daily(visits, start, now, days),
            "aging": aging, "completed": [_visit_row(v, by_key[v.key], now) for v in completed],
            "returns": sum(start <= v.entered < now and v.returned for v in visits),
            "insights": _insights(metrics, days, complete, len(issues)), "report": report}
