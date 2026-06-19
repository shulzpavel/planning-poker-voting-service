"""RBAC for ``POST /app/sessions/{chat_id}/jira-story-points/sync``.

Managers must only sync story points for sessions belonging to their team.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.session import Session
from app.domain.task import Task
from services.voting_service import app_api
from services.voting_service._http_shared import CmsPrincipal
from services.voting_service.cms_rbac import PERM_APP_SESSIONS_MANAGE


CHAT_ID = -1001
FOREIGN_TEAM_ID = 2
OWN_TEAM_ID = 1


class FakeCmsStore:
    def __init__(self, *, team_id: int) -> None:
        self.team_id = team_id
        self.audit: list[dict] = []

    async def get_session_by_chat(self, chat_id: int, topic_id: Optional[int]) -> dict:
        return {
            "id": 99,
            "chat_id": chat_id,
            "topic_id": topic_id,
            "team_id": self.team_id,
            "title": "Sprint planning",
        }

    async def record_audit_event(self, **kwargs) -> None:
        self.audit.append(kwargs)


class FakeRepo:
    async def get_session(self, chat_id: int, topic_id: Optional[int]) -> Session:
        session = Session(chat_id=chat_id, topic_id=topic_id)
        session.last_batch = [
            Task(jira_key="TEAM-1", summary="Task", votes={1: "5"}),
        ]
        return session


def _manager(*, team_ids: frozenset[int]) -> CmsPrincipal:
    return CmsPrincipal(
        id=7,
        username="team-lead",
        display_name="Team Lead",
        is_superuser=False,
        permissions=frozenset({PERM_APP_SESSIONS_MANAGE}),
        roles=(),
        pages=(),
        team_ids=team_ids,
        teams=(),
    )


def _app(*, cms_team_id: int, actor_team_ids: frozenset[int]) -> FastAPI:
    app = FastAPI()
    app.include_router(app_api.app_router, prefix="/api/v1")
    app.state.repository = FakeRepo()
    app.state.cms_store = FakeCmsStore(team_id=cms_team_id)
    app.dependency_overrides[app_api._manager_dep] = lambda: _manager(team_ids=actor_team_ids)
    return app


def test_sync_jira_sp_denied_for_foreign_team():
    """Regression: PERM_APP_SESSIONS_MANAGE alone must not bypass team scope."""
    with TestClient(_app(cms_team_id=FOREIGN_TEAM_ID, actor_team_ids=frozenset({OWN_TEAM_ID}))) as client:
        response = client.post(
            f"/api/v1/app/sessions/{CHAT_ID}/jira-story-points/sync",
            json={"skip_errors": True},
        )

    # assert_record_access hides foreign records as 404 (no existence leak).
    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"


@patch("app.usecases.update_jira_sp.UpdateJiraStoryPointsUseCase")
@patch("app.adapters.jira_service_client.JiraServiceHttpClient")
def test_sync_jira_sp_allowed_for_own_team(mock_jira_client, mock_use_case):
    execute = AsyncMock(return_value=(1, [], []))
    mock_use_case.return_value.execute = execute
    mock_jira_client.return_value.close = AsyncMock()

    with TestClient(_app(cms_team_id=OWN_TEAM_ID, actor_team_ids=frozenset({OWN_TEAM_ID}))) as client:
        response = client.post(
            f"/api/v1/app/sessions/{CHAT_ID}/jira-story-points/sync",
            json={"skip_errors": True},
        )

    assert response.status_code == 200
    assert response.json() == {"updated": 1, "failed": [], "skipped": []}
    execute.assert_awaited_once()
