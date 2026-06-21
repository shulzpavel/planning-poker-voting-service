"""Regression: CMS sessions list must not 500 (import/scope wiring)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import cms_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms_rbac import PERM_SESSIONS_VIEW


class FakeCmsStore:
    async def list_sessions(self, **kwargs: Any) -> dict:
        assert kwargs["is_superuser"] is True
        assert kwargs["actor_team_ids"] == []
        return {"items": [], "next_cursor": None, "limit": kwargs["limit"]}


def _app() -> FastAPI:
    store = FakeCmsStore()
    app = FastAPI()

    async def _actor() -> CmsPrincipal:
        return CmsPrincipal(
            id=1,
            username="admin",
            display_name=None,
            is_superuser=True,
            permissions=frozenset({PERM_SESSIONS_VIEW}),
            roles=(),
            pages=(),
            team_ids=frozenset(),
            teams=(),
        )

    app.state.cms_store = store
    app.dependency_overrides[_require_auth] = _actor
    app.include_router(cms_api.cms_router, prefix="/api/v1")
    return app


def test_cms_sessions_list_returns_200() -> None:
    with TestClient(_app()) as client:
        response = client.get("/api/v1/cms/sessions?limit=10")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None, "limit": 10}
