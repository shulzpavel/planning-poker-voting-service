"""Tests for deploy maintenance status endpoint."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service.system_api import system_router
from services.voting_service.system_status import MAINTENANCE_REDIS_KEY


class FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def get(self, key: str) -> str | None:
        if key != MAINTENANCE_REDIS_KEY:
            return None
        return self.value


def _app_with_redis(redis_client: FakeRedis | None) -> FastAPI:
    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        app.state.web_redis = redis_client
        yield

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(system_router, prefix="/api/v1")
    return app


def test_system_status_inactive_when_key_missing() -> None:
    with TestClient(_app_with_redis(FakeRedis())) as client:
        response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.json() == {"maintenance": {"active": False, "service": None}}


def test_system_status_active_with_service() -> None:
    payload = json.dumps({"active": True, "service": "voting-service"})
    with TestClient(_app_with_redis(FakeRedis(payload))) as client:
        response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.json() == {
        "maintenance": {"active": True, "service": "voting-service"},
    }


def test_system_status_inactive_when_redis_unavailable() -> None:
    with TestClient(_app_with_redis(None)) as client:
        response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.json()["maintenance"]["active"] is False
