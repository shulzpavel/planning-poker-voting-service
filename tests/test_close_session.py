"""Tests for ``CloseSessionUseCase``.

This use case is now the single source of truth for "drain the active queue
into history and mark the batch complete". Both the manager-driven
``POST /app/sessions/{chat_id}/finish`` and the admin-driven
``POST /cms/sessions/{session_id}/close`` endpoints delegate to it, so the
behavioural contract has to be regression-tested in one place.
"""

from pathlib import Path

import pytest

from app.adapters.session_file import FileSessionRepository
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.close_session import CloseSessionUseCase


def _temp_repo(name: str) -> tuple[FileSessionRepository, Path]:
    path = Path(f"/tmp/{name}.json")
    if path.exists():
        path.unlink()
    return FileSessionRepository(path), path


@pytest.mark.asyncio
async def test_close_session_drains_queue_into_last_batch_and_history():
    repo, path = _temp_repo("test_close_session_drains")
    try:
        session = Session(chat_id=10, topic_id=None)
        session.tasks_queue = [
            Task(jira_key="BB-1", summary="Login"),
            Task(jira_key="BB-2", summary="Search"),
        ]
        session.current_batch_started_at = "2025-01-01T10:00:00"
        await repo.save_session(session)

        use_case = CloseSessionUseCase(repo)
        post_session, completed = await use_case.execute(10, None)

        assert post_session.batch_completed is True
        assert post_session.tasks_queue == []
        assert post_session.current_batch_started_at is None
        assert post_session.revealed_task_id is None
        assert [task.jira_key for task in completed] == ["BB-1", "BB-2"]
        assert [task.jira_key for task in post_session.last_batch] == ["BB-1", "BB-2"]
        assert [task.jira_key for task in post_session.history] == ["BB-1", "BB-2"]
        assert all(task.completed_at for task in completed)
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_close_session_appends_to_existing_last_batch():
    """Regression: the CMS close handler used to *replace* ``last_batch`` with
    the pending queue, while the manager finish handler *extended* it. The
    use case now extends — matching the manager flow — so tasks the manager
    explicitly finished earlier in the same active period are not lost when
    an admin force-closes the session."""
    repo, path = _temp_repo("test_close_session_extends")
    try:
        session = Session(chat_id=11, topic_id=None)
        session.last_batch = [Task(jira_key="OLD-1", summary="Earlier finished")]
        session.history = [Task(jira_key="OLD-1", summary="Earlier finished")]
        session.tasks_queue = [Task(jira_key="NEW-1", summary="Added after finish")]
        await repo.save_session(session)

        use_case = CloseSessionUseCase(repo)
        post_session, completed = await use_case.execute(11, None)

        assert [task.jira_key for task in completed] == ["OLD-1", "NEW-1"]
        assert [task.jira_key for task in post_session.last_batch] == ["OLD-1", "NEW-1"]
        assert [task.jira_key for task in post_session.history] == ["OLD-1", "NEW-1"]
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_close_session_is_idempotent():
    """Calling close twice in a row must not raise and must leave the session
    in the same terminal state — admins and managers can race on it."""
    repo, path = _temp_repo("test_close_session_idempotent")
    try:
        session = Session(chat_id=12, topic_id=None)
        session.tasks_queue = [Task(jira_key="BB-1", summary="Login")]
        await repo.save_session(session)

        use_case = CloseSessionUseCase(repo)
        first_session, first_completed = await use_case.execute(12, None)
        second_session, second_completed = await use_case.execute(12, None)

        assert first_session.batch_completed is True
        assert second_session.batch_completed is True
        assert [task.jira_key for task in first_completed] == ["BB-1"]
        # Second call sees an empty queue — nothing new to drain, but
        # ``last_batch`` is preserved untouched.
        assert [task.jira_key for task in second_completed] == ["BB-1"]
        assert [task.jira_key for task in second_session.last_batch] == ["BB-1"]
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_close_session_preserves_existing_completed_at():
    """``completed_at`` set by previous handlers (auto-next-on-last) must not
    be overwritten by close."""
    repo, path = _temp_repo("test_close_session_completed_at")
    try:
        session = Session(chat_id=13, topic_id=None)
        previously_completed = Task(jira_key="BB-1", summary="Login")
        previously_completed.completed_at = "2025-01-01T09:00:00"
        session.tasks_queue = [
            previously_completed,
            Task(jira_key="BB-2", summary="Logout"),
        ]
        await repo.save_session(session)

        use_case = CloseSessionUseCase(repo)
        post_session, completed = await use_case.execute(13, None)

        completed_by_key = {task.jira_key: task for task in completed}
        assert completed_by_key["BB-1"].completed_at == "2025-01-01T09:00:00"
        # BB-2 had no prior timestamp, so close sets one.
        assert completed_by_key["BB-2"].completed_at is not None
    finally:
        if path.exists():
            path.unlink()
