"""Server-side historical first-review speed, with equal-duration baseline."""
from datetime import datetime, timedelta
from statistics import mean, median
from zoneinfo import ZoneInfo

DAY = 86400

def _stats(rows, arrivals: int, seconds: float) -> dict:
    values = [duration for _, _, duration in rows]
    return {"count": len(values), "median": median(values) if values else None, "mean": mean(values) if values else None,
            "withinDay": 100 * sum(x <= 1 for x in values) / len(values) if values else None,
            "arrivalsPerDay": arrivals / (seconds / DAY) if seconds > 0 else 0}

def build_intake_speed(report: dict, *, start: datetime, now: datetime, timezone: str) -> dict:
    span = now.timestamp() - start.timestamp()
    previous_start = datetime.fromtimestamp(start.timestamp() - span, start.tzinfo)
    completed, arrivals = [], []
    for row in report["requests"]:
        entered = datetime.fromisoformat(row["entered_at"])
        arrivals.append(entered)
        if row.get("left_at"):
            left = datetime.fromisoformat(row["left_at"])
            completed.append((row["key"], left, (left - entered).total_seconds() / DAY))
    current = [x for x in completed if start <= x[1] < now]
    previous = [x for x in completed if previous_start <= x[1] < start]
    weekly = []
    for week in report["weekly"]:
        rows = [x for x in current if week["start"] <= x[1].astimezone(ZoneInfo(timezone)).date().isoformat() <= week["end"]]
        stats = _stats(rows, 0, DAY)
        weekly.append({"start": week["start"], "end": week["end"], "keys": [x[0] for x in rows], "median": stats["median"], "mean": stats["mean"],
                       "partial": (datetime.fromisoformat(week["end"]) - datetime.fromisoformat(week["start"])).days != 6})
    return {"start": start.timestamp() * 1000, "end": now.timestamp() * 1000, "previousStart": previous_start.timestamp() * 1000,
            "current": _stats(current, report["received"], span), "previous": _stats(previous, sum(previous_start <= x < start for x in arrivals), span), "weekly": weekly}
