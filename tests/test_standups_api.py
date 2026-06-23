"""CMS standups API: permissions, roster seeding, publish visibility."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import cms_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms_rbac import PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW


class FakeRedis:
    """Minimal async Redis stand-in for AI job dedupe in standup publish tests."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.store[key] = value
        return True


def _noop_spawn_ai_job(coro: object) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        close()


class FakeStandupStore:
    def __init__(self) -> None:
        self._standups: dict[int, dict[str, Any]] = {}
        self._rosters: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    async def list_standups(self, **kwargs: Any) -> dict[str, Any]:
        items = list(self._standups.values())
        if kwargs.get("published_only"):
            items = [item for item in items if item["status"] == "published"]
        team_id = kwargs.get("team_id")
        if team_id is not None:
            items = [item for item in items if item["team_id"] == team_id]
        items.sort(key=lambda item: (item["meeting_date"], item["id"]), reverse=True)
        limit = int(kwargs.get("limit") or 50)
        return {"items": items[:limit], "next_cursor": None, "limit": limit}

    async def find_last_standup_jira_due_date(self, **kwargs: Any) -> Optional[dict[str, str]]:
        team_id = kwargs["team_id"]
        jira_key = str(kwargs.get("jira_key") or "").strip().upper()
        before = kwargs.get("before_meeting_date")
        rows = sorted(
            self._standups.values(),
            key=lambda row: (row["meeting_date"], row["id"]),
            reverse=True,
        )
        for row in rows:
            if row["team_id"] != team_id:
                continue
            meeting = date.fromisoformat(row["meeting_date"])
            if before is not None and meeting >= before:
                continue
            for participant in row["payload"].get("participants", []):
                for item in participant.get("items", []):
                    if str(item.get("jira_key") or "").strip().upper() != jira_key:
                        continue
                    due = str(item.get("due_date") or "").strip()
                    if due:
                        return {"due_date": due, "meeting_date": row["meeting_date"]}
        return None

    async def find_previous_published_standup(self, **kwargs: Any) -> Optional[dict[str, Any]]:
        team_id = kwargs["team_id"]
        before = kwargs["before_meeting_date"]
        exclude = kwargs.get("exclude_standup_id")
        rows = sorted(
            self._standups.values(),
            key=lambda row: (row["meeting_date"], row["id"]),
            reverse=True,
        )
        for row in rows:
            if row["team_id"] != team_id or row["status"] != "published":
                continue
            meeting = date.fromisoformat(row["meeting_date"])
            if meeting >= before:
                continue
            if exclude is not None and row["id"] == exclude:
                continue
            return dict(row)
        return None

    async def get_standup(self, standup_id: int) -> Optional[dict[str, Any]]:
        row = self._standups.get(standup_id)
        return dict(row) if row else None

    async def get_standup_for_team_date(self, team_id: int, meeting_date: date) -> Optional[dict[str, Any]]:
        for row in self._standups.values():
            if row["team_id"] == team_id and row["meeting_date"] == meeting_date.isoformat():
                return dict(row)
        return None

    async def create_standup(self, **kwargs: Any) -> dict[str, Any]:
        standup_id = next(self._ids)
        row = {
            "id": standup_id,
            "team_id": kwargs["team_id"],
            "meeting_date": kwargs["meeting_date"].isoformat(),
            "status": kwargs.get("status", "draft"),
            "payload": kwargs["payload"],
            "created_by": kwargs.get("created_by"),
            "published_by": None,
            "published_at": None,
            "ai_summary": None,
            "created_at": "2026-06-23T10:00:00+00:00",
            "updated_at": "2026-06-23T10:00:00+00:00",
            "team": {"id": kwargs["team_id"], "slug": "alpha", "name": "Alpha"},
        }
        self._standups[standup_id] = row
        return dict(row)

    async def save_standup_ai_summary(self, standup_id: int, ai_summary: dict[str, Any]) -> Optional[dict[str, Any]]:
        row = self._standups.get(standup_id)
        if not row:
            return None
        row = dict(row)
        row["ai_summary"] = ai_summary
        self._standups[standup_id] = row
        return dict(row)

    async def update_standup(self, standup_id: int, **kwargs: Any) -> Optional[dict[str, Any]]:
        row = self._standups.get(standup_id)
        if not row:
            return None
        row = dict(row)
        row["payload"] = kwargs["payload"]
        if kwargs.get("status"):
            row["status"] = kwargs["status"]
            if kwargs["status"] == "published":
                row["published_by"] = kwargs.get("published_by")
                row["published_at"] = "2026-06-23T11:00:00+00:00"
        self._standups[standup_id] = row
        return dict(row)

    async def publish_standup(self, standup_id: int, *, published_by: Optional[int]) -> Optional[dict[str, Any]]:
        return await self.update_standup(
            standup_id,
            payload=self._standups[standup_id]["payload"],
            status="published",
            published_by=published_by,
        )

    async def delete_standup(self, standup_id: int) -> bool:
        return self._standups.pop(standup_id, None) is not None

    async def get_standup_roster(self, team_id: int) -> Optional[dict[str, Any]]:
        row = self._rosters.get(team_id)
        return dict(row) if row else None

    async def upsert_standup_roster(self, team_id: int, members: list[dict[str, Any]], updated_by: Optional[int]) -> dict[str, Any]:
        row = {
            "team_id": team_id,
            "members": members,
            "updated_by": updated_by,
            "created_at": "2026-06-23T09:00:00+00:00",
            "updated_at": "2026-06-23T09:00:00+00:00",
        }
        self._rosters[team_id] = row
        return dict(row)

    async def record_audit_event(self, *args: Any, **kwargs: Any) -> None:
        return None


def _app(*, permissions: set[str], team_ids: tuple[int, ...] = (3,), superuser: bool = False) -> FastAPI:
    store = FakeStandupStore()
    store._rosters[3] = {
        "team_id": 3,
        "members": [{"id": "m1", "name": "Alice", "role": "front", "active": True}],
        "updated_by": 1,
        "created_at": "2026-06-23T09:00:00+00:00",
        "updated_at": "2026-06-23T09:00:00+00:00",
    }
    app = FastAPI()

    async def _actor() -> CmsPrincipal:
        return CmsPrincipal(
            id=1,
            username="lead",
            display_name=None,
            is_superuser=superuser,
            permissions=frozenset(permissions),
            roles=(),
            pages=(),
            team_ids=frozenset(team_ids),
            teams=({"id": 3, "slug": "alpha", "name": "Alpha"},),
        )

    from services.voting_service import ai_jobs

    app.state.cms_store = store
    app.state.web_redis = FakeRedis()
    ai_jobs.spawn_ai_job = _noop_spawn_ai_job
    app.dependency_overrides[_require_auth] = _actor
    app.include_router(cms_api.cms_router, prefix="/api/v1")
    return app


def test_standups_list_requires_view_permission() -> None:
    with TestClient(_app(permissions=set())) as client:
        response = client.get("/api/v1/cms/standups")
    assert response.status_code == 403


def test_standups_create_seeds_from_roster() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        response = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-23"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["payload"]["participants"][0]["name"] == "Alice"


def test_standups_list_returns_cursor_page() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW}, superuser=True)) as client:
        client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-23"},
        )
        response = client.get("/api/v1/cms/standups", params={"sort": "team_then_date"})
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert len(body["items"]) >= 1
    assert "next_cursor" in body
    assert "limit" in body


def test_viewer_sees_only_published_standups() -> None:
    app = _app(permissions={PERM_STANDUPS_VIEW})
    store: FakeStandupStore = app.state.cms_store
    import asyncio

    asyncio.run(
        store.create_standup(
            team_id=3,
            meeting_date=date(2026, 6, 22),
            payload={"participants": [], "notes": ""},
            created_by=1,
            status="published",
        )
    )
    asyncio.run(
        store.create_standup(
            team_id=3,
            meeting_date=date(2026, 6, 21),
            payload={"participants": [], "notes": ""},
            created_by=1,
            status="draft",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/cms/standups")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "published"


def test_publish_standup() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-24"},
        )
        standup_id = created.json()["id"]
        response = client.post(f"/api/v1/cms/standups/{standup_id}/publish")
    assert response.status_code == 200
    assert response.json()["status"] == "published"


def test_update_strips_empty_task_rows() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-26"},
        )
        standup_id = created.json()["id"]
        response = client.patch(
            f"/api/v1/cms/standups/{standup_id}",
            json={
                "payload": {
                    "participants": [
                        {
                            "id": "p1",
                            "name": "Alice",
                            "role": "front",
                            "present": True,
                            "items": [
                                {"id": "i-empty", "task_title": "", "track": "today"},
                                {"id": "i-ok", "task_title": "Auth flow", "track": "today"},
                            ],
                        }
                    ]
                }
            },
        )
    assert response.status_code == 200
    items = response.json()["payload"]["participants"][0]["items"]
    assert len(items) == 1
    assert items[0]["task_title"] == "Auth flow"


def test_update_keeps_jira_only_task_rows() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-27"},
        )
        standup_id = created.json()["id"]
        response = client.patch(
            f"/api/v1/cms/standups/{standup_id}",
            json={
                "payload": {
                    "participants": [
                        {
                            "id": "p1",
                            "name": "Alice",
                            "role": "front",
                            "present": True,
                            "items": [
                                {"id": "i-jira", "task_title": "", "jira_key": "FLEX-326", "track": "today"},
                            ],
                        }
                    ]
                }
            },
        )
    assert response.status_code == 200
    items = response.json()["payload"]["participants"][0]["items"]
    assert len(items) == 1
    assert items[0]["jira_key"] == "FLEX-326"
    assert items[0]["task_title"] == "FLEX-326"


def test_jira_lookup_requires_manage_permission() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_VIEW})) as client:
        response = client.get("/api/v1/cms/standups/jira-issues/FLEX-326")
    assert response.status_code == 403


def test_jira_lookup_returns_summary(monkeypatch) -> None:
    async def fake_fetch(_session: object, issue_key: str) -> dict[str, str]:
        assert issue_key == "FLEX-326"
        return {"key": "FLEX-326", "summary": "Auth flow", "url": "https://jira.example/browse/FLEX-326"}

    monkeypatch.setattr("services.voting_service.cms.standups._fetch_jira_issue_summary", fake_fetch)
    app = _app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})
    app.state.http_session = object()
    with TestClient(app) as client:
        response = client.get("/api/v1/cms/standups/jira-issues/FLEX-326")
    assert response.status_code == 200
    assert response.json() == {
        "key": "FLEX-326",
        "summary": "Auth flow",
        "url": "https://jira.example/browse/FLEX-326",
    }


def test_local_due_hint_returns_prior_standup_due_date() -> None:
    app = _app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})
    store: FakeStandupStore = app.state.cms_store
    import asyncio

    asyncio.run(
        store.create_standup(
            team_id=3,
            meeting_date=date(2026, 6, 23),
            payload={
                "participants": [
                    {
                        "id": "p1",
                        "name": "Alice",
                        "role": "front",
                        "present": True,
                        "items": [
                            {
                                "id": "i1",
                                "task_title": "Auth",
                                "jira_key": "FLEX-326",
                                "track": "today",
                                "due_date": "2026-06-25",
                            }
                        ],
                    }
                ]
            },
            created_by=1,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/cms/standups/local-due-hints/FLEX-326",
            params={"team_id": 3, "before": "2026-06-24"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["jira_key"] == "FLEX-326"
    assert body["due_date"] == "2026-06-25"
    assert body["meeting_date"] == "2026-06-23"


def test_local_due_hint_empty_when_no_prior_record() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_VIEW})) as client:
        response = client.get(
            "/api/v1/cms/standups/local-due-hints/FLEX-999",
            params={"team_id": 3, "before": "2026-06-24"},
        )
    assert response.status_code == 200
    assert response.json()["due_date"] is None


def test_sync_roster_adds_missing_participants() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-28"},
        )
        standup_id = created.json()["id"]
        client.put(
            "/api/v1/cms/standup-rosters/3",
            json={
                "members": [
                    {"id": "m1", "name": "Alice", "role": "front", "active": True},
                    {"id": "m2", "name": "Bob", "role": "back", "active": True},
                ]
            },
        )
        response = client.post(f"/api/v1/cms/standups/{standup_id}/sync-roster")
    assert response.status_code == 200
    names = {participant["name"] for participant in response.json()["payload"]["participants"]}
    assert names == {"Alice", "Bob"}


def test_sync_roster_rejects_published() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-29"},
        )
        standup_id = created.json()["id"]
        client.post(f"/api/v1/cms/standups/{standup_id}/publish")
        response = client.post(f"/api/v1/cms/standups/{standup_id}/sync-roster")
    assert response.status_code == 409


def test_blocker_requires_comment() -> None:
    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-25"},
        )
        standup_id = created.json()["id"]
        response = client.patch(
            f"/api/v1/cms/standups/{standup_id}",
            json={
                "payload": {
                    "participants": [
                        {
                            "id": "p1",
                            "name": "Alice",
                            "role": "front",
                            "present": True,
                            "items": [
                                {
                                    "id": "i1",
                                    "task_title": "Blocked task",
                                    "track": "blocker",
                                    "comment": "",
                                }
                            ],
                        }
                    ]
                }
            },
        )
    assert response.status_code == 422


def test_should_queue_standup_ai_only_on_publish_transition() -> None:
    from services.voting_service.cms.standups import _should_queue_standup_ai

    assert _should_queue_standup_ai({"status": "draft"}, {"status": "published"}) is True
    assert _should_queue_standup_ai({"status": "published", "ai_summary": {"summary": "ok"}}, {"status": "published"}) is False
    assert _should_queue_standup_ai({"status": "draft"}, {"status": "draft"}) is False


def test_publish_queues_ai_analysis(monkeypatch) -> None:
    queued: list[int] = []

    async def fake_queue(request, standup_id: int, actor_username: str, *, force_refresh: bool = False) -> None:
        queued.append(standup_id)

    monkeypatch.setattr("services.voting_service.cms.standups._queue_standup_ai_analysis", fake_queue)

    with TestClient(_app(permissions={PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW})) as client:
        created = client.post(
            "/api/v1/cms/standups",
            json={"team_id": 3, "meeting_date": "2026-06-30"},
        )
        standup_id = created.json()["id"]
        response = client.post(f"/api/v1/cms/standups/{standup_id}/publish")
    assert response.status_code == 200
    assert queued == [standup_id]
