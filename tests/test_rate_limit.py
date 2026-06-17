"""Tests for the Redis-backed rate limit helpers.

The Redis adapter is mocked with a tiny in-memory implementation that
honours the contract our :mod:`services.voting_service.rate_limit` code
uses (``eval`` returning the post-INCR value). This keeps the tests
hermetic — no external Redis required — while still exercising the
production code path end to end.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import HTTPException

from services.voting_service.rate_limit import (
    RateLimitExceeded,  # noqa: F401  re-exported for downstream tests
    enforce_rate_limit,
    hit_rate_limit,
)


class FakeRedis:
    """In-memory stand-in that implements just enough of redis.asyncio.Redis
    for the rate-limit helpers. ``eval`` short-circuits the Lua script
    directly in Python so we can verify the helper's contract.

    Expiry is tracked by wall-clock; tests can fast-forward via
    ``advance_time``.
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}
        self._expiry: dict[str, float] = {}
        self.now: float = 0.0
        self.eval_calls: list[tuple[str, list[str], list[str]]] = []

    def _purge_expired(self) -> None:
        for key, deadline in list(self._expiry.items()):
            if deadline <= self.now:
                self._values.pop(key, None)
                self._expiry.pop(key, None)

    def advance_time(self, seconds: float) -> None:
        self.now += seconds
        self._purge_expired()

    async def eval(self, script: str, num_keys: int, *args: Any) -> int:
        self.eval_calls.append((script, list(args[:num_keys]), list(args[num_keys:])))
        self._purge_expired()
        key = args[0]
        ttl_seconds = int(args[num_keys])
        current = self._values.get(key, 0) + 1
        self._values[key] = current
        if current == 1:
            self._expiry[key] = self.now + ttl_seconds
        return current


# ---------------------------------------------------------------------------
# hit_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hit_rate_limit_increments_and_returns_counter() -> None:
    redis = FakeRedis()
    first = await hit_rate_limit(redis, "rl:test", 60)
    second = await hit_rate_limit(redis, "rl:test", 60)
    assert first == 1
    assert second == 2


@pytest.mark.asyncio
async def test_hit_rate_limit_swallows_redis_errors_returning_none() -> None:
    """Production must fail open: a Redis blip cannot turn the surface 500."""

    class BrokenRedis:
        async def eval(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("redis is down")

    out = await hit_rate_limit(BrokenRedis(), "rl:test", 60)
    assert out is None


# ---------------------------------------------------------------------------
# enforce_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_rate_limit_allows_under_threshold() -> None:
    redis = FakeRedis()
    for _ in range(3):
        await enforce_rate_limit(redis, key="rl:test", limit=3, window_seconds=60)


@pytest.mark.asyncio
async def test_enforce_rate_limit_blocks_with_429_after_limit() -> None:
    redis = FakeRedis()
    await enforce_rate_limit(redis, key="rl:test", limit=2, window_seconds=60)
    await enforce_rate_limit(redis, key="rl:test", limit=2, window_seconds=60)
    with pytest.raises(HTTPException) as info:
        await enforce_rate_limit(
            redis,
            key="rl:test",
            limit=2,
            window_seconds=60,
            error_detail="Too many requests",
        )
    assert info.value.status_code == 429
    assert info.value.detail == "Too many requests"


@pytest.mark.asyncio
async def test_enforce_rate_limit_resets_after_window_expires() -> None:
    redis = FakeRedis()
    await enforce_rate_limit(redis, key="rl:test", limit=1, window_seconds=10)
    with pytest.raises(HTTPException):
        await enforce_rate_limit(redis, key="rl:test", limit=1, window_seconds=10)

    redis.advance_time(11.0)
    # New window: counter should start from zero again.
    await enforce_rate_limit(redis, key="rl:test", limit=1, window_seconds=10)


@pytest.mark.asyncio
async def test_enforce_rate_limit_fails_open_when_redis_unavailable() -> None:
    """If hit_rate_limit returns None, enforce_rate_limit must allow the call."""

    class BrokenRedis:
        async def eval(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("redis is down")

    # No exception, returns 0 (allowed).
    result = await enforce_rate_limit(
        BrokenRedis(), key="rl:test", limit=1, window_seconds=60
    )
    assert result == 0


@pytest.mark.asyncio
async def test_enforce_rate_limit_isolates_keys() -> None:
    """Two keys must have independent counters — the whole point of rate
    limiting per (login user, IP, participant, actor, ...)."""
    redis = FakeRedis()
    await enforce_rate_limit(redis, key="rl:user:a", limit=1, window_seconds=60)
    # Hitting the SAME key again would 429; a DIFFERENT key must not.
    await enforce_rate_limit(redis, key="rl:user:b", limit=1, window_seconds=60)
    with pytest.raises(HTTPException):
        await enforce_rate_limit(redis, key="rl:user:a", limit=1, window_seconds=60)
    with pytest.raises(HTTPException):
        await enforce_rate_limit(redis, key="rl:user:b", limit=1, window_seconds=60)


# ---------------------------------------------------------------------------
# /web/token integration: prove the wiring inside web_api actually fires
# ---------------------------------------------------------------------------


class FakeRedisForWebToken(FakeRedis):
    """FakeRedis with the extra hooks ``create_web_token`` exercises:

    * ``setex`` to register the new web:<token> entry — we don't care about
      content, only that it doesn't blow up.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setex_calls: list[tuple[str, int, str]] = []

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.setex_calls.append((key, ttl, value))
        return True


@pytest.mark.asyncio
async def test_web_token_endpoint_blocks_after_quota() -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from services.voting_service.web_api import (
        WEB_TOKEN_RATE_LIMIT_MAX,
        web_router,
    )

    app = FastAPI()
    app.state.web_redis = FakeRedisForWebToken()
    app.state.cms_store = None
    app.include_router(web_router, prefix="/api/v1")

    with TestClient(app) as client:
        # Burn the entire quota — every call must succeed.
        for _ in range(WEB_TOKEN_RATE_LIMIT_MAX):
            ok = client.post(
                "/api/v1/web/token",
                json={"chat_id": 42, "topic_id": None},
            )
            assert ok.status_code == 200, ok.text

        # The very next call must trip 429.
        blocked = client.post(
            "/api/v1/web/token",
            json={"chat_id": 42, "topic_id": None},
        )
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many web token requests"
