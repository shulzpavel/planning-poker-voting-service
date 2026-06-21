"""Regression tests for the fixes from the comprehensive review.

These tests cover failure paths that previously caused user-visible breakage
or silently dropped data:

- CmsSyncScheduler race: snapshots scheduled mid-flight must still be synced.
- mutate_session conflict path: the FastAPI app must convert
  SessionMutationConflictError to HTTP 409 instead of a generic 500.
- _publish_state: a Redis pub/sub failure must not poison the HTTP response
  of a successful mutation.
- web_vote: a Redis pub/sub failure must not roll back the vote.
- app_skip_task: skip must produce exactly one audit event (not skip + next).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.session import Session
from app.domain.task import Task
from app.ports.session_repository import SessionMutationConflictError


# ---------------------------------------------------------------------------
# CmsSyncScheduler race condition
# ---------------------------------------------------------------------------


class _RecordingCmsStore:
    """Captures every sync_session call and lets tests pause the first one."""

    def __init__(self) -> None:
        self.synced: list[tuple[int, Optional[int], int]] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self._call_count = 0

    async def sync_session(self, session: Session) -> None:
        self._call_count += 1
        if self._call_count == 1:
            self.first_started.set()
            await self.release_first.wait()
        self.synced.append((session.chat_id, session.topic_id, session.tasks_version))


@pytest.mark.asyncio
async def test_cms_sync_scheduler_does_not_drop_snapshots_during_race() -> None:
    """Regression: schedule() racing with _run() finalization must not lose data.

    Before the fix, if schedule() saw a not-yet-done task while _run() was
    between popping its slot and clearing self._tasks, the new pending entry
    would be stranded forever and the read model would silently stop updating.
    """
    from services.voting_service.cms_sync import CmsSyncScheduler

    store = _RecordingCmsStore()
    scheduler = CmsSyncScheduler(store, debounce_seconds=0)

    session_a = Session(chat_id=1, topic_id=None)
    session_a.tasks_version = 1
    scheduler.schedule(session_a)

    await store.first_started.wait()

    # Race: schedule a fresh snapshot while the first _run() is still busy.
    session_b = Session(chat_id=1, topic_id=None)
    session_b.tasks_version = 2
    scheduler.schedule(session_b)

    store.release_first.set()

    # Drive the event loop long enough for any rescheduled run to drain.
    for _ in range(50):
        await asyncio.sleep(0)

    await scheduler.close()

    versions = [item[2] for item in store.synced]
    assert 1 in versions, f"first snapshot must reach the store: {versions}"
    assert 2 in versions, (
        "the snapshot scheduled during the race must not be dropped; "
        f"got: {versions}"
    )


@pytest.mark.asyncio
async def test_cms_sync_scheduler_close_cancels_pending_tasks() -> None:
    """close() must not hang on a slow sync_session call."""
    from services.voting_service.cms_sync import CmsSyncScheduler

    class _SlowStore:
        async def sync_session(self, session: Session) -> None:
            await asyncio.sleep(60)

    scheduler = CmsSyncScheduler(_SlowStore(), debounce_seconds=0)
    scheduler.schedule(Session(chat_id=42, topic_id=None))

    # Let the task actually start before closing.
    await asyncio.sleep(0)

    await asyncio.wait_for(scheduler.close(), timeout=1.0)


# ---------------------------------------------------------------------------
# SessionMutationConflictError → HTTP 409
# ---------------------------------------------------------------------------


def test_session_mutation_conflict_returns_409() -> None:
    """FastAPI must translate retry-exhausted mutations into a retriable 409.

    We construct a tiny isolated FastAPI app that reuses the exception handler
    from the production module so the test does not touch the global app's
    lifespan (which would otherwise spin up Redis/Postgres bindings).
    """
    from services.voting_service.main import _on_session_mutation_conflict

    app = FastAPI()
    app.add_exception_handler(SessionMutationConflictError, _on_session_mutation_conflict)

    @app.get("/_test/conflict")
    async def _raise_conflict() -> None:  # pragma: no cover - exercised by client
        raise SessionMutationConflictError("forced")

    with TestClient(app) as client:
        response = client.get("/_test/conflict")

    assert response.status_code == 409
    assert response.json() == {"detail": "Session is busy, please retry."}


# ---------------------------------------------------------------------------
# _publish_state best-effort
# ---------------------------------------------------------------------------


class _BrokenRedis:
    """Redis client whose pub/sub publish always blows up."""

    async def publish(self, *_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("redis pub/sub down")


@pytest.mark.asyncio
async def test_publish_state_is_best_effort() -> None:
    """A pub/sub outage must not poison a successful mutation response."""
    from services.voting_service.app_api import _publish_state

    class _Req:
        class app:  # noqa: D401, N801
            class state:  # noqa: D401, N801
                web_redis = _BrokenRedis()

    # Should not raise — committed mutations must remain visible to the caller.
    await _publish_state(_Req(), Session(chat_id=1, topic_id=None))


@pytest.mark.asyncio
async def test_web_vote_pubsub_failure_does_not_fail_request() -> None:
    """A broken pub/sub must not roll back an already-persisted vote."""
    from app.constants import VALID_VOTE_VALUES
    from app.domain.participant import Participant
    from config import UserRole
    from services.voting_service.web_api import web_router

    assert "1" in VALID_VOTE_VALUES  # sanity-check fixture assumption

    class _Redis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        async def get(self, key: str):
            if key == "web:t1":
                return json.dumps({"chat_id": 7, "topic_id": None})
            if key.startswith("web_participant:t1:"):
                return json.dumps({"name": "Alice", "user_id": -42, "role": "backend"})
            return None

        async def publish(self, channel: str, payload: str) -> int:
            raise RuntimeError("forced pub/sub failure")

    class _Repo:
        def __init__(self) -> None:
            participant = Participant(user_id=-42, name="Alice", role=UserRole.PARTICIPANT)
            task = Task(jira_key="X-1", summary="task")
            self.session = Session(chat_id=7, topic_id=None)
            self.session.participants[-42] = participant
            self.session.tasks_queue.append(task)
            self.session.current_batch_started_at = "2024-01-01"

        async def get_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
            return self.session

        async def mutate_session(self, chat_id, topic_id, mutator):
            mutator(self.session)
            return self.session, None

    app = FastAPI()
    app.state.web_redis = _Redis()
    app.state.repository = _Repo()
    app.include_router(web_router, prefix="/api/v1")

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/web/vote",
            json={"token": "t1", "participant_id": "p-1", "value": "1"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    # Vote must be persisted even though pub/sub raised.
    assert app.state.repository.session.tasks_queue[0].votes[-42] == "1"


# ---------------------------------------------------------------------------
# app_skip_task records exactly one audit event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_skip_task_records_single_audit_event(monkeypatch) -> None:
    """skip must not double-audit by chaining into app_next_task."""
    from services.voting_service import app_api

    recorded: list[tuple[str, str]] = []

    async def fake_audit(_request, action, username, status, _extra=None):
        recorded.append((action, status))

    async def fake_publish(_request, _session) -> None:
        return None

    async def fake_get(_repo, _chat_id, _topic_id):
        session = Session(chat_id=1, topic_id=None)
        session.batch_completed = False
        return session

    async def fake_mutate(_repo, _chat_id, _topic_id, mutator):
        session = Session(chat_id=1, topic_id=None)
        session.tasks_queue.append(Task(summary="t1"))
        session.current_batch_started_at = "2024-01-01"
        mutator(session)
        return session, None

    async def fake_notify(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("services.voting_service.app.sessions._audit", fake_audit)
    monkeypatch.setattr("services.voting_service.app.sessions._publish_state", fake_publish)
    monkeypatch.setattr("services.voting_service.app.sessions._get_repo_session", fake_get)
    monkeypatch.setattr("services.voting_service.app.sessions._mutate_repo_session", fake_mutate)
    monkeypatch.setattr(app_api, "maybe_notify_session_finished", fake_notify)

    class _Actor:
        username = "manager"

    class _Req:
        class app:  # noqa: N801
            class state:  # noqa: N801
                repository = object()

    await app_api.app_skip_task(chat_id=1, request=_Req(), topic_id=None, actor=_Actor())

    actions = [item[0] for item in recorded]
    assert actions == ["app.session.skip"], (
        f"skip should emit exactly one audit event, got: {actions}"
    )


@pytest.mark.asyncio
async def test_app_next_task_notifies_when_last_task_auto_completes(monkeypatch) -> None:
    """Auto-next after the final estimate is a session finish for alerts."""
    from services.voting_service import app_api

    notifications: list[dict[str, Any]] = []

    async def fake_get(_repo, _chat_id, _topic_id):
        session = Session(chat_id=1, topic_id=None)
        session.batch_completed = False
        return session

    async def fake_mutate(_repo, _chat_id, _topic_id, mutator):
        session = Session(chat_id=1, topic_id=None)
        session.tasks_queue.append(Task(summary="final task"))
        session.current_batch_started_at = "2024-01-01"
        mutator(session)
        return session, None

    async def fake_noop(*_args, **_kwargs) -> None:
        return None

    async def fake_notify(_request, session, **kwargs) -> None:
        notifications.append({"session": session, **kwargs})

    monkeypatch.setattr("services.voting_service.app.sessions._get_repo_session", fake_get)
    monkeypatch.setattr("services.voting_service.app.sessions._mutate_repo_session", fake_mutate)
    monkeypatch.setattr("services.voting_service.app.sessions._ensure_current_task_description", fake_noop)
    monkeypatch.setattr("services.voting_service.app.sessions._publish_state", fake_noop)
    monkeypatch.setattr("services.voting_service.app.sessions._audit", fake_noop)
    monkeypatch.setattr(app_api, "maybe_notify_session_finished", fake_notify)

    class _Actor:
        username = "manager"

    class _Req:
        class app:  # noqa: N801
            class state:  # noqa: N801
                repository = object()

    await app_api.app_next_task(chat_id=1, request=_Req(), topic_id=None, actor=_Actor())

    assert len(notifications) == 1
    assert notifications[0]["was_completed"] is False
    assert notifications[0]["session"].batch_completed is True
    assert notifications[0]["close_method"] == "Last task completed"
