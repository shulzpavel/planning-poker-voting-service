"""CMS team delete endpoint and store guards."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import cms_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms_store.teams import TeamDeleteBlockedError


class FakeTeamsStore:
  include_inactive = False

  async def record_audit_event(self, **kwargs: Any) -> None:
    return None

  async def list_teams(self, *, is_superuser: bool, actor_team_ids: list[int], include_inactive: bool = False) -> list[dict[str, Any]]:
    self.include_inactive = include_inactive
    return []

  async def delete_team(self, team_id: int) -> Optional[dict[str, Any]]:
    if team_id == 404:
      return None
    if team_id == 409:
      raise TeamDeleteBlockedError(
        "active_sessions",
        count=2,
        message="Закройте 2 активных сессий перед удалением команды",
      )
    return {
      "id": team_id,
      "slug": "alpha",
      "name": "Alpha",
      "detached": {
        "sessions": 1,
        "sprint_plans": 0,
        "retros": 0,
        "scope_boards": 1,
        "standups_deleted": 3,
        "standup_rosters_deleted": 1,
        "admin_links_removed": 2,
      },
    }


def _app(*, superuser: bool = True) -> FastAPI:
  store = FakeTeamsStore()
  app = FastAPI()

  async def _actor() -> CmsPrincipal:
    return CmsPrincipal(
      id=1,
      username="root",
      display_name=None,
      is_superuser=superuser,
      permissions=frozenset(),
      roles=(),
      pages=(),
      team_ids=frozenset(),
      teams=(),
    )

  app.state.cms_store = store
  app.dependency_overrides[_require_auth] = _actor
  app.include_router(cms_api.cms_router, prefix="/api/v1")
  return app


def test_list_teams_requests_inactive_for_superuser() -> None:
  store = FakeTeamsStore()
  app = FastAPI()

  async def _actor() -> CmsPrincipal:
    return CmsPrincipal(
      id=1,
      username="root",
      display_name=None,
      is_superuser=True,
      permissions=frozenset(),
      roles=(),
      pages=(),
      team_ids=frozenset(),
      teams=(),
    )

  app.state.cms_store = store
  app.dependency_overrides[_require_auth] = _actor
  app.include_router(cms_api.cms_router, prefix="/api/v1")

  with TestClient(app) as client:
    response = client.get("/api/v1/cms/teams")
  assert response.status_code == 200
  assert store.include_inactive is True


def test_delete_team_requires_superuser() -> None:
  with TestClient(_app(superuser=False)) as client:
    response = client.delete("/api/v1/cms/teams/5")
  assert response.status_code == 403


def test_delete_team_not_found() -> None:
  with TestClient(_app()) as client:
    response = client.delete("/api/v1/cms/teams/404")
  assert response.status_code == 404


def test_delete_team_blocked_with_active_sessions() -> None:
  with TestClient(_app()) as client:
    response = client.delete("/api/v1/cms/teams/409")
  assert response.status_code == 409
  assert "активных сессий" in response.json()["detail"]


def test_delete_team_success_returns_detach_summary() -> None:
  with TestClient(_app()) as client:
    response = client.delete("/api/v1/cms/teams/7")
  assert response.status_code == 200
  payload = response.json()
  assert payload["id"] == 7
  assert payload["detached"]["scope_boards"] == 1
  assert payload["detached"]["standups_deleted"] == 3


def test_team_delete_blocked_error_carries_reason() -> None:
  err = TeamDeleteBlockedError("live_retros", count=1, message="finish retro")
  assert err.reason == "live_retros"
  assert err.count == 1
