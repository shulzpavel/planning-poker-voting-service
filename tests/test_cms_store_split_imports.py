"""Regression: cms_store split must not leave helper imports missing."""

from __future__ import annotations

import os
import uuid
from typing import Optional

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import app_api
from services.voting_service._http_shared import CmsPrincipal
from services.voting_service.cms_rbac import PERM_APP_SESSIONS_MANAGE
from services.voting_service.cms_store import PostgresCmsStore

POSTGRES_DSN = os.getenv("TEST_POSTGRES_DSN") or os.getenv("POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="POSTGRES_DSN or TEST_POSTGRES_DSN required for cms_store integration tests",
)


@pytest_asyncio.fixture
async def cms_store():
    store = await PostgresCmsStore.create(POSTGRES_DSN)
    yield store
    await store.pool.close()


@pytest.mark.asyncio
async def test_set_session_team_by_chat_does_not_nameerror(cms_store: PostgresCmsStore) -> None:
    chat_id = -int(uuid.uuid4().int % 9_000_000_000_000) - 1_000_000_000_000
    assert await cms_store.set_session_team_by_chat(chat_id, None, None) is True


@pytest.mark.asyncio
async def test_set_session_title_by_chat_does_not_nameerror(cms_store: PostgresCmsStore) -> None:
    chat_id = -int(uuid.uuid4().int % 9_000_000_000_000) - 1_000_000_000_000
    assert await cms_store.set_session_title_by_chat(chat_id, None, "Regression title") is True


class _FakeRedis:
    async def setex(self, key: str, ttl: int, value: str) -> bool:
        return True


class _FakeRepo:
    async def get_session(self, chat_id: int, topic_id: Optional[int]):
        from app.domain.session import Session

        return Session(chat_id=chat_id, topic_id=topic_id)

    async def save_session(self, session) -> None:
        return None


class _FakeCmsStore:
    def __init__(self) -> None:
        self.team_calls: list[tuple[int, Optional[int], Optional[int]]] = []
        self.title_calls: list[tuple[int, Optional[int], str]] = []

    async def set_session_team_by_chat(self, chat_id: int, topic_id: Optional[int], team_id: Optional[int]) -> bool:
        self.team_calls.append((chat_id, topic_id, team_id))
        return True

    async def record_web_token(self, token: str, chat_id: int, topic_id: Optional[int], ttl: int) -> None:
        return None

    async def set_session_title_by_chat(self, chat_id: int, topic_id: Optional[int], title: Optional[str], **kwargs) -> bool:
        self.title_calls.append((chat_id, topic_id, title or ""))
        return True

    async def record_audit_event(self, **kwargs) -> None:
        return None


def test_create_app_session_returns_200() -> None:
    store = _FakeCmsStore()
    app = FastAPI()
    app.include_router(app_api.app_router, prefix="/api/v1")
    app.state.repository = _FakeRepo()
    app.state.cms_store = store
    app.state.web_redis = _FakeRedis()
    app.dependency_overrides[app_api._manager_dep] = lambda: CmsPrincipal(
        id=1,
        username="admin",
        display_name=None,
        is_superuser=True,
        permissions=frozenset({PERM_APP_SESSIONS_MANAGE}),
        roles=(),
        pages=(),
        team_ids=frozenset(),
        teams=(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/app/sessions",
            json={"title": "Split regression session"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Split regression session"
    assert isinstance(body["chat_id"], int)
    assert body["chat_id"] < 0
    assert len(store.team_calls) == 1
    assert len(store.title_calls) == 1
