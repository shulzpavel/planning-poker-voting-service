"""Tests for Redis-backed async AI job helpers."""

from __future__ import annotations

import pytest

from services.voting_service.ai_jobs import (
    complete_job,
    fail_job,
    find_cached_scope_summary,
    get_job,
    get_job_for_poll,
    get_or_create_job,
    is_active_job_stale,
    job_public_view,
    update_job,
)


class JobRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._values[key] = value
        self._expiry[key] = ttl

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)
        self._expiry.pop(key, None)


@pytest.mark.asyncio
async def test_get_or_create_job_dedupes_active_job() -> None:
    redis = JobRedis()
    first_id, first_new = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:1",
        actor="admin",
    )
    second_id, second_new = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:1",
        actor="admin",
    )

    assert first_new is True
    assert second_new is False
    assert first_id == second_id


@pytest.mark.asyncio
async def test_job_lifecycle_updates_status_and_result() -> None:
    redis = JobRedis()
    job_id, _ = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:2",
        actor="admin",
    )

    await update_job(redis, job_id, status="running", phase="calling_llm")
    job = await get_job(redis, job_id)
    assert job is not None
    assert job["status"] == "running"
    assert job["phase"] == "calling_llm"
    assert job["message"] == "AI генерирует ответ"

    await complete_job(
        redis,
        job_id,
        {"ai_summary": {"health": "green"}, "board": {"id": 2}},
        kind="scope",
        resource_key="board:2",
    )
    done = await get_job(redis, job_id)
    assert done is not None
    assert done["status"] == "done"
    assert done["result"]["board"]["id"] == 2
    public = job_public_view(done)
    assert public["result"]["ai_summary"]["health"] == "green"


@pytest.mark.asyncio
async def test_fail_job_records_error_and_clears_dedupe() -> None:
    redis = JobRedis()
    job_id, _ = await get_or_create_job(
        redis,
        kind="retro",
        resource_key="retro:3",
        actor="admin",
    )
    await fail_job(redis, job_id, "LLM failed", kind="retro", resource_key="retro:3")
    job = await get_job(redis, job_id)
    assert job is not None
    assert job["status"] == "error"
    assert job["error"] == "LLM failed"
    assert await redis.get("ai_job_dedupe:retro:retro:3") is None


@pytest.mark.asyncio
async def test_get_or_create_job_replaces_stale_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.voting_service.ai_jobs.AI_JOB_STALE_SECONDS", 60)
    redis = JobRedis()
    stale_id, _ = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:9",
        actor="admin",
    )
    stale_job = await get_job(redis, stale_id)
    assert stale_job is not None
    await update_job(
        redis,
        stale_id,
        status="running",
        phase="calling_llm",
        updated_at="2020-01-01T00:00:00+00:00",
    )

    new_id, is_new = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:9",
        actor="admin",
    )

    assert is_new is True
    assert new_id != stale_id
    failed = await get_job(redis, stale_id)
    assert failed is not None
    assert failed["status"] == "error"


@pytest.mark.asyncio
async def test_get_job_for_poll_fails_stale_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.voting_service.ai_jobs.AI_JOB_STALE_SECONDS", 30)
    redis = JobRedis()
    job_id, _ = await get_or_create_job(
        redis,
        kind="scope",
        resource_key="board:10",
        actor="admin",
    )
    await update_job(redis, job_id, status="running", phase="calling_llm", updated_at="2020-01-01T00:00:00+00:00")

    polled = await get_job_for_poll(redis, job_id)
    assert polled is not None
    assert polled["status"] == "error"
    public = job_public_view(polled)
    assert public["error"]


def test_find_cached_scope_summary_matches_snapshot_refreshed_at() -> None:
    board = {
        "ai_summary_history": [
            {
                "id": "a",
                "snapshot_refreshed_at": "2026-06-13T12:00:00+00:00",
                "analysis": {"health": "green", "summary": "ok"},
            }
        ]
    }
    cached = find_cached_scope_summary(board, "2026-06-13T12:00:00+00:00")
    assert cached is not None
    assert cached["health"] == "green"
    assert find_cached_scope_summary(board, "2026-06-14T12:00:00+00:00") is None
