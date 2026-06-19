"""Team scope for destructive CMS token revoke and participant hard-delete."""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import cms_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms_rbac import (
    PERM_APP_SESSIONS_MANAGE,
    PERM_WEB_PARTICIPANTS_DELETE,
)

OWN_TEAM_ID = 1
FOREIGN_TEAM_ID = 2
TOKEN_ID = 42
USER_ID = 9001
USER_NAME = "Alice Example"


class FakeCmsStore:
    def __init__(
        self,
        *,
        token_team_id: Optional[int],
        token_active: bool = True,
        user_team_ids: Optional[list[Optional[int]]] = None,
    ) -> None:
        self.token_team_id = token_team_id
        self.token_active = token_active
        self.user_team_ids = user_team_ids if user_team_ids is not None else [FOREIGN_TEAM_ID]
        self.revoke_called = False
        self.hard_delete_called = False

    async def get_web_token(self, token_id: int) -> Optional[dict]:
        if token_id != TOKEN_ID:
            return None
        return {
            "id": TOKEN_ID,
            "token_hash": "abc123",
            "chat_id": -1001,
            "topic_id": 10,
            "team_id": self.token_team_id,
            "is_active": self.token_active,
        }

    async def revoke_web_token(self, token_id: int) -> Optional[str]:
        self.revoke_called = True
        return "abc123" if token_id == TOKEN_ID else None

    async def get_user_session_team_ids(self, user_id: int) -> list[Optional[int]]:
        if user_id != USER_ID:
            return []
        return list(self.user_team_ids)

    async def hard_delete_user(self, user_id: int, confirm_name: str) -> Optional[dict]:
        self.hard_delete_called = True
        if user_id != USER_ID or confirm_name != USER_NAME:
            return None
        return {
            "user_id": str(USER_ID),
            "votes_deleted": 1,
            "session_participants_deleted": 1,
            "web_participants_deleted": 0,
        }

    async def record_audit_event(self, **kwargs) -> None:
        return None


def _actor(*, team_ids: frozenset[int], permissions: frozenset[str]) -> CmsPrincipal:
    return CmsPrincipal(
        id=7,
        username="team-admin",
        display_name="Team Admin",
        is_superuser=False,
        permissions=permissions,
        roles=(),
        pages=(),
        team_ids=team_ids,
        teams=(),
    )


def _app(
    *,
    store: FakeCmsStore,
    team_ids: frozenset[int],
    permissions: frozenset[str],
) -> FastAPI:
    app = FastAPI()
    app.include_router(cms_api.cms_router, prefix="/api/v1")
    app.state.cms_store = store
    app.dependency_overrides[_require_auth] = lambda: _actor(
        team_ids=team_ids,
        permissions=permissions,
    )
    return app


def test_revoke_token_denied_for_foreign_team():
    store = FakeCmsStore(token_team_id=FOREIGN_TEAM_ID)
    with TestClient(
        _app(
            store=store,
            team_ids=frozenset({OWN_TEAM_ID}),
            permissions=frozenset({PERM_APP_SESSIONS_MANAGE}),
        )
    ) as client:
        response = client.delete(f"/api/v1/cms/tokens/{TOKEN_ID}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"
    assert store.revoke_called is False


def test_revoke_token_allowed_for_own_team():
    store = FakeCmsStore(token_team_id=OWN_TEAM_ID)
    with TestClient(
        _app(
            store=store,
            team_ids=frozenset({OWN_TEAM_ID}),
            permissions=frozenset({PERM_APP_SESSIONS_MANAGE}),
        )
    ) as client:
        response = client.delete(f"/api/v1/cms/tokens/{TOKEN_ID}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "token_id": TOKEN_ID, "revoked": True}
    assert store.revoke_called is True


def test_hard_delete_user_denied_for_foreign_team():
    store = FakeCmsStore(token_team_id=OWN_TEAM_ID, user_team_ids=[FOREIGN_TEAM_ID])
    with TestClient(
        _app(
            store=store,
            team_ids=frozenset({OWN_TEAM_ID}),
            permissions=frozenset({PERM_WEB_PARTICIPANTS_DELETE}),
        )
    ) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/cms/users/{USER_ID}",
            json={"confirm_name": USER_NAME},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"
    assert store.hard_delete_called is False


def test_hard_delete_user_allowed_for_own_team():
    store = FakeCmsStore(token_team_id=OWN_TEAM_ID, user_team_ids=[OWN_TEAM_ID])
    with TestClient(
        _app(
            store=store,
            team_ids=frozenset({OWN_TEAM_ID}),
            permissions=frozenset({PERM_WEB_PARTICIPANTS_DELETE}),
        )
    ) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/cms/users/{USER_ID}",
            json={"confirm_name": USER_NAME},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert store.hard_delete_called is True


def test_hard_delete_user_allowed_when_only_legacy_null_team():
    store = FakeCmsStore(token_team_id=OWN_TEAM_ID, user_team_ids=[None])
    with TestClient(
        _app(
            store=store,
            team_ids=frozenset({OWN_TEAM_ID}),
            permissions=frozenset({PERM_WEB_PARTICIPANTS_DELETE}),
        )
    ) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/cms/users/{USER_ID}",
            json={"confirm_name": USER_NAME},
        )

    assert response.status_code == 200
    assert store.hard_delete_called is True
