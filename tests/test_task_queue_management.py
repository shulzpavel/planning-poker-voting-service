"""Tests for task queue management use cases."""

from pathlib import Path

import pytest

from app.adapters.session_file import FileSessionRepository
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.manage_tasks import (
    AddManualTasksUseCase,
    DeleteTaskUseCase,
    MoveTaskUseCase,
    ReorderTasksUseCase,
    TaskQueueError,
    ReopenCompletedTaskUseCase,
    UpdateTaskUseCase,
)


def temp_repo(name: str) -> tuple[FileSessionRepository, Path]:
    path = Path(f"/tmp/{name}.json")
    if path.exists():
        path.unlink()
    return FileSessionRepository(path), path


class TestTaskIdentity:
    def test_task_serialization_preserves_stable_id_and_source(self):
        task = Task(jira_key="PROJ-1", summary="Build", source="jira")

        loaded = Task.from_dict(task.to_dict())

        assert loaded.task_id == task.task_id
        assert loaded.source == "jira"
        assert loaded.created_at == task.created_at

    def test_legacy_task_gets_deterministic_id(self):
        payload = {"jira_key": "OLD-1", "summary": "Legacy"}

        first = Task.from_dict(payload)
        second = Task.from_dict(payload)

        assert first.task_id == second.task_id
        assert first.source == "jira"


class TestTaskQueueManagement:
    @pytest.mark.asyncio
    async def test_add_manual_tasks_bumps_version(self):
        # ``AddManualTasksUseCase`` still accepts a list (used by both the
        # single-task endpoint and the Jira import path), but the legacy
        # "bulk paste" HTTP route has been removed — see the matching
        # frontend cleanup in ManagerPage / SessionsPage. This test
        # exercises the underlying use case directly.
        repo, path = temp_repo("test_add_manual_tasks")
        try:
            await repo.save_session(Session(chat_id=1, topic_id=None))
            use_case = AddManualTasksUseCase(repo)

            result = await use_case.execute(
                1,
                None,
                [
                    {"summary": "Manual 1"},
                    {"summary": "Manual 2", "jira_key": "PROJ-2"},
                ],
                expected_version=0,
            )

            session = await repo.get_session(1, None)
            assert result.session.tasks_version == 1
            assert len(session.tasks_queue) == 2
            assert session.tasks_queue[0].source == "manual"
            assert session.tasks_queue[1].jira_key == "PROJ-2"
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_update_rejects_stale_version(self):
        repo, path = temp_repo("test_update_rejects_stale_version")
        try:
            session = Session(chat_id=1, topic_id=None)
            task = Task(summary="Old")
            session.tasks_queue = [task]
            session.tasks_version = 2
            await repo.save_session(session)

            use_case = UpdateTaskUseCase(repo)

            with pytest.raises(TaskQueueError) as exc:
                await use_case.execute(1, None, task.task_id, "New", expected_version=1)

            assert exc.value.status_code == 409
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_delete_preserves_current_task_by_id(self):
        repo, path = temp_repo("test_delete_preserves_current_task_by_id")
        try:
            task1 = Task(summary="One")
            task2 = Task(summary="Two")
            task3 = Task(summary="Three")
            session = Session(chat_id=1, topic_id=None, tasks_queue=[task1, task2, task3], current_task_index=1)
            await repo.save_session(session)

            use_case = DeleteTaskUseCase(repo)
            await use_case.execute(1, None, task1.task_id, expected_version=0)

            updated = await repo.get_session(1, None)
            assert updated.current_task.task_id == task2.task_id
            assert updated.current_task_index == 0
            assert [task.summary for task in updated.tasks_queue] == ["Two", "Three"]
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_cannot_delete_or_move_current_task_while_active(self):
        repo, path = temp_repo("test_current_task_locked")
        try:
            task = Task(summary="Current")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[task, Task(summary="Next")],
                current_task_index=0,
                current_batch_started_at="2026-01-01T00:00:00",
            )
            await repo.save_session(session)

            with pytest.raises(TaskQueueError):
                await DeleteTaskUseCase(repo).execute(1, None, task.task_id, expected_version=0)

            with pytest.raises(TaskQueueError):
                await MoveTaskUseCase(repo).execute(1, None, task.task_id, 1, expected_version=0)
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_move_preserves_current_task_by_id(self):
        repo, path = temp_repo("test_move_preserves_current_task_by_id")
        try:
            task1 = Task(summary="One")
            task2 = Task(summary="Two")
            task3 = Task(summary="Three")
            session = Session(chat_id=1, topic_id=None, tasks_queue=[task1, task2, task3], current_task_index=1)
            await repo.save_session(session)

            await MoveTaskUseCase(repo).execute(1, None, task3.task_id, 0, expected_version=0)

            updated = await repo.get_session(1, None)
            assert [task.summary for task in updated.tasks_queue] == ["Three", "One", "Two"]
            assert updated.current_task.task_id == task2.task_id
            assert updated.current_task_index == 2
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_reorder_preserves_current_task_by_id(self):
        repo, path = temp_repo("test_reorder_preserves_current_task_by_id")
        try:
            task1 = Task(summary="One")
            task2 = Task(summary="Two")
            task3 = Task(summary="Three")
            session = Session(chat_id=1, topic_id=None, tasks_queue=[task1, task2, task3], current_task_index=1)
            await repo.save_session(session)

            await ReorderTasksUseCase(repo).execute(
                1,
                None,
                [task3.task_id, task1.task_id, task2.task_id],
                expected_version=0,
            )

            updated = await repo.get_session(1, None)
            assert [task.summary for task in updated.tasks_queue] == ["Three", "One", "Two"]
            assert updated.current_task.task_id == task2.task_id
            assert updated.current_task_index == 2
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_reorder_rejects_moving_current_task_while_active(self):
        repo, path = temp_repo("test_reorder_rejects_current_task_move")
        try:
            task1 = Task(summary="One")
            task2 = Task(summary="Two")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[task1, task2],
                current_task_index=0,
                current_batch_started_at="2026-01-01T00:00:00",
            )
            await repo.save_session(session)

            with pytest.raises(TaskQueueError) as exc:
                await ReorderTasksUseCase(repo).execute(1, None, [task2.task_id, task1.task_id], expected_version=0)

            assert exc.value.status_code == 409
        finally:
            if path.exists():
                path.unlink()


class TestReopenCompletedTask:
    @pytest.mark.asyncio
    async def test_reopen_completed_task_from_queue_slice(self):
        repo, path = temp_repo("test_reopen_completed_queue")
        try:
            played = Task(summary="Played", story_points=5)
            played.votes[1] = "5"
            played.completed_at = "2026-01-01T00:00:00"
            current = Task(summary="Current")
            future = Task(summary="Future")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[played, current, future],
                current_task_index=1,
            )
            await repo.save_session(session)

            result = await ReopenCompletedTaskUseCase(repo).execute(
                1,
                None,
                played.task_id,
                expected_version=0,
            )

            updated = await repo.get_session(1, None)
            assert result.task.task_id == played.task_id
            assert updated.current_task.task_id == played.task_id
            assert updated.current_task.story_points == 5
            assert updated.current_task.votes == {}
            assert updated.current_task.completed_at is None
            assert updated.batch_completed is False
            assert updated.current_batch_started_at is not None
            assert [task.summary for task in updated.tasks_queue] == ["Played", "Current", "Future"]
            assert updated.tasks_version == 1
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_reopen_removes_task_from_last_batch_and_history(self):
        repo, path = temp_repo("test_reopen_completed_batch")
        try:
            played_batch = Task(summary="Played", story_points=3)
            played_history = Task.from_dict(played_batch.to_dict())
            session = Session(
                chat_id=1,
                topic_id=None,
                last_batch=[played_batch],
                history=[played_history],
                batch_completed=True,
            )
            await repo.save_session(session)

            await ReopenCompletedTaskUseCase(repo).execute(
                1,
                None,
                played_batch.task_id,
                expected_version=0,
            )

            updated = await repo.get_session(1, None)
            assert updated.last_batch == []
            assert updated.history == []
            assert updated.current_task.summary == "Played"
            assert updated.current_task.story_points == 3
            assert updated.current_task.votes == {}
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_rejects_future_queue_task(self):
        repo, path = temp_repo("test_rejects_future_task_reopen")
        try:
            played = Task(summary="Played", story_points=5)
            future = Task(summary="Future")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[played, future],
                current_task_index=1,
            )
            await repo.save_session(session)

            with pytest.raises(TaskQueueError) as exc:
                await ReopenCompletedTaskUseCase(repo).execute(
                    1,
                    None,
                    future.task_id,
                    expected_version=0,
                )

            assert exc.value.status_code == 404
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_rejects_reopen_while_voting_active(self):
        repo, path = temp_repo("test_rejects_reopen_while_voting")
        try:
            played = Task(summary="Played", story_points=5)
            current = Task(summary="Current")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[played, current],
                current_task_index=1,
                current_batch_started_at="2026-01-01T00:00:00",
            )
            await repo.save_session(session)

            with pytest.raises(TaskQueueError) as exc:
                await ReopenCompletedTaskUseCase(repo).execute(
                    1,
                    None,
                    played.task_id,
                    expected_version=0,
                )

            assert exc.value.status_code == 409
        finally:
            if path.exists():
                path.unlink()

    @pytest.mark.asyncio
    async def test_reopen_then_recomplete_updates_summary_totals(self):
        """Full re-estimate loop: played task reopens, gets new SP, returns to completed slice."""
        from services.voting_service.app_api import _summary_payload

        repo, path = temp_repo("test_reopen_recomplete_summary")
        try:
            played = Task(summary="Played", story_points=5)
            played.votes[1] = "5"
            played.completed_at = "2026-01-01T00:00:00"
            current = Task(summary="Current")
            session = Session(
                chat_id=1,
                topic_id=None,
                tasks_queue=[played, current],
                current_task_index=1,
            )
            await repo.save_session(session)

            before = _summary_payload(session, title="Session")
            assert before["stats"]["total_completed"] == 1
            assert before["stats"]["total_story_points"] == 5

            await ReopenCompletedTaskUseCase(repo).execute(1, None, played.task_id, expected_version=0)

            mid = await repo.get_session(1, None)
            during = _summary_payload(mid, title="Session")
            assert during["stats"]["total_completed"] == 0
            assert during["stats"]["total_story_points"] == 0
            assert mid.current_task.story_points == 5
            assert mid.current_task.votes == {}

            mid.current_task.story_points = 8
            mid.current_task.votes[1] = "8"
            mid.current_task_index = 1
            await repo.save_session(mid)

            after = await repo.get_session(1, None)
            final = _summary_payload(after, title="Session")
            assert final["stats"]["total_completed"] == 1
            assert final["stats"]["total_story_points"] == 8
            assert final["completed_tasks"][0]["story_points"] == 8
        finally:
            if path.exists():
                path.unlink()
