"""Tests for readiness probes using lifespan singletons."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service.health import health_router
from services.voting_service.health_checks import (
    check_voting_readiness,
    ping_postgres_pool,
    ping_redis,
    ping_repository,
)


def _app_with_state(**state) -> FastAPI:
    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        for key, value in state.items():
            setattr(app.state, key, value)
        yield

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(health_router, prefix="/health")
    return app


@pytest.mark.asyncio
async def test_ping_redis_accepts_pong() -> None:
    redis_client = AsyncMock()
    redis_client.ping.return_value = True
    await ping_redis(redis_client)
    redis_client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_ping_postgres_pool_runs_select_one() -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await ping_postgres_pool(pool)

    conn.fetchval.assert_awaited_once_with("SELECT 1")


@pytest.mark.asyncio
async def test_ping_repository_uses_existing_redis_client() -> None:
    redis_client = AsyncMock()
    redis_client.ping.return_value = True
    repository = AsyncMock()
    repository._get_client.return_value = redis_client

    await ping_repository(repository)

    repository._get_client.assert_awaited_once()
    redis_client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_voting_readiness_probes_shared_clients() -> None:
    redis_client = AsyncMock()
    redis_client.ping.return_value = True
    web_redis = AsyncMock()
    web_redis.ping.return_value = True
    repository = AsyncMock()
    repository._get_client.return_value = redis_client

    await check_voting_readiness(
        repository=repository,
        web_redis=web_redis,
        cms_store=None,
    )

    repository._get_client.assert_awaited_once()
    assert redis_client.ping.await_count == 1
    web_redis.ping.assert_awaited_once()


def test_readiness_endpoint_uses_app_state_without_new_repository() -> None:
    redis_client = AsyncMock()
    redis_client.ping.return_value = True
    web_redis = AsyncMock()
    web_redis.ping.return_value = True
    repository = MagicMock()
    repository.pool = None
    repository._get_client = AsyncMock(return_value=redis_client)

    app = _app_with_state(repository=repository, web_redis=web_redis, cms_store=None)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    repository._get_client.assert_awaited_once()
    web_redis.ping.assert_awaited_once()


def test_readiness_endpoint_reports_missing_web_redis() -> None:
    repository = MagicMock()
    app = _app_with_state(repository=repository, web_redis=None, cms_store=None)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert "web_redis" in body["error"]
