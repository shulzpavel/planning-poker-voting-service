"""Grounded chart contexts and strict evidence validation for intake AI."""
from __future__ import annotations

from datetime import date
from typing import Any

KINDS = ("outcomes", "duration", "sources", "flow", "queue", "aging", "oldest")

def _fact(code: str, text: str) -> dict:
    return {"id": code, "text": text}

def _groups(prefix: str, rows: list[dict], total: int) -> list[dict]:
    return [_fact(f"{prefix}{i}", f"{x['label']}: {x['count']} из {total} ({round(100*x['count']/total) if total else 0}%).") for i, x in enumerate(rows)]

def _chart(facts: list[dict], requests: list[dict]) -> dict:
    allowed = ("key", "summary", "status", "age_days", "entered_at", "left_at", "assignee", "source")
    selected = sorted(requests, key=lambda x: x.get("age_days") or 0, reverse=True)[:10]
    return {"facts": facts, "requests": [{key: str(row[key])[:300] if isinstance(row.get(key), str) else row.get(key) for key in allowed} for row in selected],
            "request_sample": {"shown": len(selected), "total": len(requests)}}

def build_analysis_facts(snapshot: dict) -> dict:
    r, rows = snapshot["report"], snapshot["aging"]
    new = _fact("period", f"Период {snapshot['window_start']}–{snapshot['generated_at']}; поступило впервые {r['received']}, первых решений {r['decided']}, возвратов {r['returned']}.")
    current = [_fact("current", f"Текущая очередь {r['queue_end']}; старше 7 дней {r['older_week']}; ждут уточнений {r['needs_information']}; без ответственного {r['unassigned']}.")]
    for i, status in enumerate(sorted({x["status"] for x in rows})):
        subset = [x for x in rows if x["status"] == status]
        current.append(_fact(f"status{i}", f"{status}: {len(subset)}; старше 7 дней {sum(x.get('age_days') is not None and x['age_days'] > 7 for x in subset)}; без ответственного {sum(not x.get('assignee') for x in subset)}."))
    weekly, full = [], []
    for i, w in enumerate(r["weekly"]):
        complete = (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days == 6
        weekly.append(_fact(f"week{i}", f"{w['start']}–{w['end']} ({'полная' if complete else 'неполная'} неделя): поступило {w['received']}, решений {w['decided']}, разрыв потоков {w['received']-w['decided']} (не изменение очереди)."))
        if complete:
            full.append(w)
    if len(full) >= 2:
        a, b = full[-2:]
        weekly.append(_fact("comparison", f"Последние полные недели {a['start']} и {b['start']}: поступления {a['received']} → {b['received']}, первые решения {a['decided']} → {b['decided']}."))
    if full:
        gap = max(full, key=lambda x: x["received"] - x["decided"])
        weekly.append(_fact("imbalance", f"Максимальный разрыв потоков среди полных недель {gap['received']-gap['decided']}: {gap['start']}–{gap['end']}. Минимум первых решений среди всех полных недель {min(x['decided'] for x in full)}."))
    queue = [_fact("queue", f"Очередь в начале {r['queue_start']}, сейчас {r['queue_end']}, изменение {r['queue_end']-r['queue_start']}.")]
    if snapshot["daily"]:
        peak = max(snapshot["daily"], key=lambda x: x["queue"])
        queue.append(_fact("peak", f"Максимум очереди {peak['queue']}, {peak['date']}; после пика изменение {r['queue_end']-peak['queue']}."))
    waiting_keys = next((x["keys"] for x in r["outcomes"] if x["id"] == "waiting"), [])
    waiting = [x for x in rows if x["key"] in waiting_keys]
    slow_keys = next((x["keys"] for x in r["resolution_bands"] if x["id"] == "old"), [])
    slow = [{**x, "status": "Первый разбор ЗАВЕРШЁН", "assignee": None} for x in r["requests"] if x["key"] in slow_keys]
    durations = [x["age_days"] for x in slow if x.get("age_days") is not None]
    tail = _fact("tail", f"Завершённых первых разборов дольше 7 дней: {len(slow)}; диапазон {min(durations)}–{max(durations)} календарных дней." if durations else "Завершённых первых разборов дольше 7 дней нет. Точный максимум длительности неизвестен.")
    leaders = sorted((x for x in r["sources"] if x["label"] != "Не указан"), key=lambda x: x["count"], reverse=True)[:2]
    concentration = sum(x["count"] for x in leaders)
    source_keys = {key for x in leaders for key in x["keys"]}
    return {
        "outcomes": _chart([new, *_groups("outcome", r["outcomes"], r["received"]), _fact("waiting_detail", f"Ожидающие в новой когорте {len(waiting)}, без ответственного {sum(not x.get('assignee') for x in waiting)}. Это текущий исход, не доказательство отсутствия первого решения в прошлом.")], waiting),
        "duration": _chart([new, *_groups("duration", r["resolution_bands"], r["decided"]), _fact("median", f"Медиана календарных дней завершённого первого разбора: {r['typical_days']}."), tail], slow),
        "sources": _chart([new, *_groups("source", r["sources"], r["received"]), _fact("concentration", f"Два крупнейших источника: {', '.join(x['label'] for x in leaders)}; {concentration} из {r['received']}. Все проценты округлены.")], [x for x in r["requests"] if x["key"] in source_keys]),
        "flow": _chart([new, *weekly, *queue], []), "queue": _chart([*queue, *current, *weekly], rows),
        "aging": _chart(current, [x for x in rows if x.get("age_days") is not None and x["age_days"] > 7]), "oldest": _chart(current, rows),
    }

def validate_analysis(value: Any, facts: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid analysis object")
    output = {}
    for kind in KINDS:
        row = value.get(kind)
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() or len(row[k]) > 1400 for k in ("headline", "explanation", "focus")):
            raise ValueError(f"Invalid analysis text: {kind}")
        ids, keys = row.get("fact_ids"), row.get("keys")
        allowed_ids = {x["id"] for x in facts[kind]["facts"]}
        allowed_keys = {x["key"] for x in facts[kind]["requests"]}
        if not isinstance(ids, list) or not ids or len(ids) > len(allowed_ids) or any(not isinstance(x, str) or x not in allowed_ids for x in ids):
            raise ValueError(f"Invalid fact references: {kind}")
        if not isinstance(keys, list) or len(keys) > 10 or any(not isinstance(x, str) or x not in allowed_keys for x in keys):
            raise ValueError(f"Invalid issue references: {kind}")
        output[kind] = {k: row[k] for k in ("headline", "explanation", "focus", "fact_ids", "keys")}
        output[kind]["evidence"] = [x["text"] for x in facts[kind]["facts"] if x["id"] in ids]
    return output
