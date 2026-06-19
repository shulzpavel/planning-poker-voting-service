"""Tests for CMS session token policy (absolute expiry, token version, cookie Secure)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

from services.voting_service.cms_auth import (
    build_cms_token_payload,
    cms_token_is_expired,
    cms_token_version_matches,
    resolve_cms_cookie_secure,
)
from services.voting_service.cms_store import PostgresCmsStore
from services.voting_service._http_shared import CMS_TOKEN_TTL, _require_auth

POSTGRES_DSN = os.getenv("TEST_POSTGRES_DSN") or os.getenv("POSTGRES_DSN")

pytestmark_integration = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="POSTGRES_DSN or TEST_POSTGRES_DSN required for token_version integration tests",
)


class FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self.now: float = 1_000_000.0
        self.deleted: list[str] = []

    async def get(self, key: str) -> Optional[str]:
        deadline = self._expiry.get(key)
        if deadline is not None and self.now >= deadline:
            self._values.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._values[key] = value
        self._expiry[key] = self.now + ttl

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self._values.pop(key, None)
        self._expiry.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        if key in self._values:
            self._expiry[key] = self.now + ttl


class FakeCmsStore:
    def __init__(self, *, principal: Optional[dict[str, Any]]) -> None:
        self.principal = principal

    async def get_admin_principal(
        self,
        admin_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if self.principal is None:
            return None
        if admin_id is not None and int(self.principal["id"]) != int(admin_id):
            return None
        if username is not None and self.principal["username"] != username:
            return None
        return dict(self.principal)


def test_build_cms_token_payload_sets_absolute_expiry() -> None:
    payload = build_cms_token_payload(
        admin_id=7,
        username="admin",
        ip="127.0.0.1",
        token_version=2,
        ttl_seconds=3600,
        now=100.0,
    )
    assert payload["expires_at"] == 3700.0
    assert payload["token_version"] == 2
    assert payload["issued_at"] == 100.0


def test_cms_token_is_expired_rejects_legacy_payload() -> None:
    legacy = {"admin_id": 1, "username": "admin", "ip": "127.0.0.1"}
    assert cms_token_is_expired(legacy, now=100.0) is True


def test_cms_token_is_expired_honours_deadline() -> None:
    payload = build_cms_token_payload(
        admin_id=1,
        username="admin",
        ip="127.0.0.1",
        token_version=1,
        ttl_seconds=60,
        now=100.0,
    )
    assert cms_token_is_expired(payload, now=159.0) is False
    assert cms_token_is_expired(payload, now=160.0) is True


def test_cms_token_version_matches() -> None:
    payload = build_cms_token_payload(
        admin_id=1,
        username="admin",
        ip="127.0.0.1",
        token_version=3,
        ttl_seconds=60,
        now=100.0,
    )
    assert cms_token_version_matches(payload, {"token_version": 3}) is True
    assert cms_token_version_matches(payload, {"token_version": 4}) is False
    assert cms_token_version_matches({"admin_id": 1}, {"token_version": 1}) is False


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CMS_COOKIE_SECURE": "true"}, True),
        ({"CMS_COOKIE_SECURE": "false"}, False),
        ({"APP_ENV": "dev"}, False),
        ({"DEPLOY_ENVIRONMENT": "production"}, True),
        ({}, True),
    ],
)
def test_resolve_cms_cookie_secure(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: bool) -> None:
    for key in ("CMS_COOKIE_SECURE", "APP_ENV", "DEPLOY_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert resolve_cms_cookie_secure() is expected


@pytest.mark.asyncio
async def test_require_auth_does_not_extend_redis_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("services.voting_service.cms_auth.time.time", lambda: redis.now)
    store = FakeCmsStore(
        principal={
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "is_superuser": True,
            "permissions": [],
            "roles": [],
            "pages": [],
            "team_ids": [],
            "teams": [],
            "token_version": 1,
        }
    )
    token = "session-token"
    payload = build_cms_token_payload(
        admin_id=1,
        username="admin",
        ip="127.0.0.1",
        token_version=1,
        ttl_seconds=CMS_TOKEN_TTL,
        now=redis.now,
    )
    await redis.setex(f"cms_token:{token}", CMS_TOKEN_TTL, json.dumps(payload))

    app = FastAPI()
    app.state.web_redis = redis
    app.state.cms_store = store

    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    request.scope["app"] = app

    first_deadline = redis._expiry[f"cms_token:{token}"]
    await _require_auth(request, cookie_token=token)
    assert redis._expiry[f"cms_token:{token}"] == first_deadline

    redis.now += CMS_TOKEN_TTL + 1
    with pytest.raises(HTTPException) as exc:
        await _require_auth(request, cookie_token=token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_auth_rejects_stale_token_version(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("services.voting_service.cms_auth.time.time", lambda: redis.now)
    store = FakeCmsStore(
        principal={
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "is_superuser": True,
            "permissions": [],
            "roles": [],
            "pages": [],
            "team_ids": [],
            "teams": [],
            "token_version": 2,
        }
    )
    token = "stale-token"
    payload = build_cms_token_payload(
        admin_id=1,
        username="admin",
        ip="127.0.0.1",
        token_version=1,
        ttl_seconds=CMS_TOKEN_TTL,
        now=redis.now,
    )
    await redis.setex(f"cms_token:{token}", CMS_TOKEN_TTL, json.dumps(payload))

    app = FastAPI()
    app.state.web_redis = redis
    app.state.cms_store = store
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    request.scope["app"] = app

    with pytest.raises(HTTPException) as exc:
        await _require_auth(request, cookie_token=token)
    assert exc.value.status_code == 401
    assert f"cms_token:{token}" in redis.deleted


@pytest_asyncio.fixture
async def cms_store():
    store = await PostgresCmsStore.create(POSTGRES_DSN)
    yield store
    await store.pool.close()


@pytest.mark.asyncio
@pytestmark_integration
async def test_password_change_bumps_token_version(cms_store: PostgresCmsStore) -> None:
    username = f"token-version-{uuid.uuid4().hex}"
    await cms_store.create_cms_admin(
        username=username,
        password="initial-password-1",
        display_name="Token Version Test",
        is_active=True,
        role_ids=[],
    )
    admin = await cms_store.get_admin_principal(username=username)
    assert admin is not None
    assert admin["token_version"] == 1

    updated = await cms_store.update_cms_admin(
        admin_id=int(admin["id"]),
        display_name="Token Version Test",
        is_active=True,
        role_ids=[],
        password="rotated-password-2",
    )
    assert updated is not None
    refreshed = await cms_store.get_admin_principal(admin_id=int(admin["id"]))
    assert refreshed is not None
    assert refreshed["token_version"] == 2
