"""Redis-backed async AI job status for long-running LLM calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

_STALE_JOB_MESSAGE = "Генерация прервана или зависла — повторите запрос"

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

AI_JOB_TTL_SECONDS = max(60, int(os.getenv("AI_JOB_TTL_SECONDS", "3600")))
AI_JOB_STALE_SECONDS = max(120, int(os.getenv("AI_JOB_STALE_SECONDS", "300")))

PHASE_MESSAGES: dict[str, str] = {
    "queued": "В очереди",
    "building_context": "Собираем контекст",
    "calling_llm": "AI генерирует ответ",
    "validating": "Проверяем результат",
    "saving": "Сохраняем",
    "done": "Готово",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_key(job_id: str) -> str:
    return f"ai_job:{job_id}"


def _dedupe_key(kind: str, resource_key: str) -> str:
    return f"ai_job_dedupe:{kind}:{resource_key}"


def _serialize_job(job: dict[str, Any]) -> str:
    return json.dumps(job, ensure_ascii=False, default=str)


def _deserialize_job(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("AI job payload must be an object")
    return data


def _parse_job_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _active_job_age_seconds(job: dict[str, Any]) -> Optional[float]:
    if job.get("status") not in {"queued", "running"}:
        return None
    updated_at = _parse_job_timestamp(job.get("updated_at")) or _parse_job_timestamp(job.get("started_at"))
    if updated_at is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())


def is_active_job_stale(job: dict[str, Any]) -> bool:
    age = _active_job_age_seconds(job)
    return age is not None and age > AI_JOB_STALE_SECONDS


async def fail_job_if_stale(redis: aioredis.Redis, job: dict[str, Any]) -> bool:
    """Mark orphaned running jobs as failed. Returns True when the job was failed."""
    if not is_active_job_stale(job):
        return False
    job_id = str(job.get("job_id") or "")
    kind = str(job.get("kind") or "")
    resource_key = str(job.get("resource_key") or "")
    if not job_id or not kind or not resource_key:
        return False
    logger.warning(
        "AI job stale job_id=%s kind=%s resource=%s age_seconds=%s",
        job_id,
        kind,
        resource_key,
        _active_job_age_seconds(job),
    )
    await fail_job(redis, job_id, _STALE_JOB_MESSAGE, kind=kind, resource_key=resource_key)
    return True


async def get_job(redis: aioredis.Redis, job_id: str) -> Optional[dict[str, Any]]:
    raw = await redis.get(_job_key(job_id))
    if not raw:
        return None
    try:
        return _deserialize_job(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Corrupt AI job record job_id=%s", job_id)
        return None


async def get_job_for_poll(redis: aioredis.Redis, job_id: str) -> Optional[dict[str, Any]]:
    job = await get_job(redis, job_id)
    if not job:
        return None
    if await fail_job_if_stale(redis, job):
        job = await get_job(redis, job_id)
    return job


async def _save_job(redis: aioredis.Redis, job_id: str, job: dict[str, Any]) -> None:
    await redis.setex(_job_key(job_id), AI_JOB_TTL_SECONDS, _serialize_job(job))


async def get_or_create_job(
    redis: aioredis.Redis,
    *,
    kind: str,
    resource_key: str,
    actor: str,
) -> tuple[str, bool]:
    """Return ``(job_id, is_new)``. Reuses an active dedupe job when present."""
    dedupe = _dedupe_key(kind, resource_key)
    existing_job_id = await redis.get(dedupe)
    if existing_job_id:
        job = await get_job(redis, existing_job_id)
        if job and job.get("status") in {"queued", "running"}:
            if await fail_job_if_stale(redis, job):
                job = None
            else:
                return existing_job_id, False

    job_id = str(uuid.uuid4())
    now = _now_iso()
    job = {
        "job_id": job_id,
        "kind": kind,
        "resource_key": resource_key,
        "actor": actor,
        "status": "queued",
        "phase": "queued",
        "message": PHASE_MESSAGES["queued"],
        "started_at": now,
        "updated_at": now,
        "error": None,
        "result": None,
    }
    await _save_job(redis, job_id, job)
    await redis.setex(dedupe, AI_JOB_TTL_SECONDS, job_id)
    return job_id, True


async def update_job(redis: aioredis.Redis, job_id: str, **updates: Any) -> None:
    job = await get_job(redis, job_id)
    if not job:
        return
    job.update(updates)
    if "updated_at" not in updates:
        job["updated_at"] = _now_iso()
    if "phase" in updates and "message" not in updates:
        phase = str(updates["phase"])
        job["message"] = PHASE_MESSAGES.get(phase, job.get("message", ""))
    await _save_job(redis, job_id, job)


async def complete_job(
    redis: aioredis.Redis,
    job_id: str,
    result: dict[str, Any],
    *,
    kind: str,
    resource_key: str,
) -> None:
    await update_job(
        redis,
        job_id,
        status="done",
        phase="done",
        message=PHASE_MESSAGES["done"],
        result=result,
        error=None,
    )
    await redis.delete(_dedupe_key(kind, resource_key))


async def fail_job(
    redis: aioredis.Redis,
    job_id: str,
    error: str,
    *,
    kind: str,
    resource_key: str,
) -> None:
    await update_job(
        redis,
        job_id,
        status="error",
        phase="done",
        message=error,
        error=error,
        result=None,
    )
    await redis.delete(_dedupe_key(kind, resource_key))


def job_public_view(job: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "message": job.get("message"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
    }
    if job.get("status") == "error":
        payload["error"] = job.get("error")
    if job.get("status") == "done" and job.get("result") is not None:
        payload["result"] = job.get("result")
    return payload


def spawn_ai_job(coro: Awaitable[None]) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_background_task_failure)


def _log_background_task_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("Background AI job crashed", exc_info=exc)


class AiJobTimer:
    """Lightweight timing helper for AI job phases."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._started = time.perf_counter()
        self.phases: dict[str, float] = {}

    def mark(self, phase: str) -> None:
        self.phases[phase] = round(time.perf_counter() - self._started, 3)

    def finish(self) -> dict[str, float]:
        self.phases["total"] = round(time.perf_counter() - self._started, 3)
        logger.info("AI job timing label=%s phases=%s", self.label, self.phases)
        return dict(self.phases)


async def run_phased_job(
    redis: aioredis.Redis,
    job_id: str,
    *,
    kind: str,
    resource_key: str,
    label: str,
    runner: Callable[[Callable[[str], Awaitable[None]]], Awaitable[dict[str, Any]]],
) -> None:
    timer = AiJobTimer(label)

    async def set_phase(phase: str) -> None:
        timer.mark(phase)
        await update_job(redis, job_id, status="running", phase=phase)

    try:
        await update_job(redis, job_id, status="running", phase="queued")
        result = await runner(set_phase)
        timer.finish()
        await complete_job(redis, job_id, result, kind=kind, resource_key=resource_key)
    except Exception as exc:
        timer.finish()
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        logger.warning("AI job failed kind=%s resource=%s error=%s", kind, resource_key, message)
        await fail_job(redis, job_id, message, kind=kind, resource_key=resource_key)


def find_cached_scope_summary(board: dict[str, Any], snapshot_refreshed_at: Optional[str]) -> Optional[dict[str, Any]]:
    if not snapshot_refreshed_at:
        return None
    history = board.get("ai_summary_history") or []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if entry.get("snapshot_refreshed_at") == snapshot_refreshed_at:
                analysis = entry.get("analysis")
                if isinstance(analysis, dict):
                    return analysis
    current = board.get("ai_summary")
    if isinstance(current, dict) and history:
        first = history[0] if isinstance(history[0], dict) else None
        if first and first.get("snapshot_refreshed_at") == snapshot_refreshed_at:
            return current
    return None
