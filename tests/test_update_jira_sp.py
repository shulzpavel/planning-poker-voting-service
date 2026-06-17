"""Tests for update Jira SP handler with busy flag and error handling.

The Telegram-era ``DIContainer.busy_ops`` flag (which guarded duplicate
button clicks) was removed together with the legacy router and the
``VotingServiceHttpClient`` adapter. The remaining tests here exercise the
``UpdateJiraStoryPointsUseCase`` and ``VotingPolicy.get_max_vote``
directly — those are still the live code paths for the web-only manager
app.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.adapters.session_file import FileSessionRepository
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.update_jira_sp import UpdateJiraStoryPointsUseCase


class TestShowBatchResultsSP:
    """Tests for SP display in batch results."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_batch_results_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    def test_batch_results_include_sp_summary(self):
        """Test that batch results include SP summary."""
        session = Session(chat_id=123, topic_id=456)

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task1.votes = {1: "5", 2: "8"}

        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task2.votes = {1: "3", 2: "3"}

        session.last_batch = [task1, task2]
        # Тест проверяет VotingPolicy напрямую, сохранение в repo не требуется

        # Проверяем, что SP правильно вычисляются
        from app.usecases.show_results import VotingPolicy
        policy = VotingPolicy()

        sp1 = policy.get_max_vote(task1.votes)
        sp2 = policy.get_max_vote(task2.votes)
        total_sp = sp1 + sp2

        assert sp1 == 8  # Максимум из 5, 8
        assert sp2 == 3  # Максимум из 3, 3
        assert total_sp == 11

    def test_batch_results_skip_votes_excluded_from_sp(self):
        """Test that skip votes are excluded from SP calculation."""
        session = Session(chat_id=123, topic_id=456)

        task = Task(jira_key="TEST-1", summary="Task 1")
        task.votes = {1: "5", 2: "skip", 3: "8"}

        from app.usecases.show_results import VotingPolicy
        policy = VotingPolicy()

        sp = policy.get_max_vote(task.votes)

        # Skip должен быть проигнорирован, максимум из 5, 8 = 8
        assert sp == 8


class CountingFileSessionRepository(FileSessionRepository):
    def __init__(self, state_file: Path):
        super().__init__(state_file)
        self.save_count = 0

    async def save_session(self, session: Session) -> None:
        self.save_count += 1
        await super().save_session(session)


class TestUpdateJiraStoryPointsUseCase:
    def setup_method(self):
        self.temp_file = Path("/tmp/test_update_sp_usecase_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = CountingFileSessionRepository(self.temp_file)
        self.jira_client = AsyncMock()
        self.use_case = UpdateJiraStoryPointsUseCase(self.jira_client, self.repo)

    def teardown_method(self):
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_does_not_save_when_nothing_updated(self):
        session = Session(chat_id=123, topic_id=456)
        task = Task(jira_key="TEST-1", summary="Task 1", votes={1: "skip"})
        session.last_batch = [task]
        await self.repo.save_session(session)
        self.repo.save_count = 0

        updated, failed, skipped = await self.use_case.execute(123, 456, skip_errors=True)

        assert updated == 0
        assert failed == []
        assert skipped == ["TEST-1: нет финальной оценки SP"]
        assert self.repo.save_count == 0

    @pytest.mark.asyncio
    async def test_prefers_manager_final_sp_over_max_vote(self):
        session = Session(chat_id=123, topic_id=456)
        session.last_batch = [
            Task(jira_key="TEST-1", summary="Task 1", votes={1: "8", 2: "5"}, story_points=3),
        ]
        await self.repo.save_session(session)
        self.repo.save_count = 0
        self.jira_client.update_story_points.return_value = True

        updated, failed, skipped = await self.use_case.execute(123, 456, skip_errors=True)

        assert updated == 1
        assert failed == []
        assert skipped == []
        self.jira_client.update_story_points.assert_awaited_once_with("TEST-1", 3)

    @pytest.mark.asyncio
    async def test_skip_errors_updates_all_valid_tasks(self):
        session = Session(chat_id=123, topic_id=456)
        session.last_batch = [
            Task(jira_key="TEST-1", summary="Task 1", votes={1: "5"}),
            Task(jira_key="TEST-2", summary="Task 2", votes={1: "8"}),
            Task(jira_key="TEST-3", summary="Task 3", votes={1: "13"}),
        ]
        await self.repo.save_session(session)
        self.repo.save_count = 0
        self.jira_client.update_story_points.side_effect = [True, False, True]

        updated, failed, skipped = await self.use_case.execute(123, 456, skip_errors=True)

        assert updated == 2
        assert failed == ["TEST-2"]
        assert skipped == []
        assert self.jira_client.update_story_points.await_count == 3
        assert self.repo.save_count == 1

    @pytest.mark.asyncio
    async def test_split_estimates_update_configured_fields_partially(self, monkeypatch):
        from app.usecases import update_jira_sp

        session = Session(chat_id=123, topic_id=456, estimation_mode="sp_dev_test")
        task = Task(jira_key="TEST-1", summary="Task 1")
        task.story_points_by_track = {"dev": 5, "test": 3}
        session.last_batch = [task]
        await self.repo.save_session(session)
        self.repo.save_count = 0
        monkeypatch.setitem(update_jira_sp.TRACK_FIELD_ENV, "dev", ("JIRA_SP_DEV_FIELD", "customfield_111"))
        monkeypatch.setitem(update_jira_sp.TRACK_FIELD_ENV, "test", ("JIRA_SP_TEST_FIELD", ""))
        self.jira_client.update_story_points_fields.return_value = {"customfield_111": True}

        updated, failed, skipped = await self.use_case.execute(123, 456, skip_errors=True)

        assert updated == 1
        assert failed == []
        assert skipped == ["TEST-1 SP Test: поле Jira не настроено или не найдено (JIRA_SP_TEST_FIELD)"]
        self.jira_client.update_story_points_fields.assert_awaited_once_with(
            "TEST-1",
            {"customfield_111": 5},
        )
        assert self.repo.save_count == 1

    @pytest.mark.asyncio
    async def test_split_estimates_report_rejected_configured_field(self, monkeypatch):
        from app.usecases import update_jira_sp

        session = Session(chat_id=123, topic_id=456, estimation_mode="sp_split")
        task = Task(jira_key="TEST-2", summary="Task 2")
        task.story_points_by_track = {"front": 8, "back": 5}
        session.last_batch = [task]
        await self.repo.save_session(session)
        monkeypatch.setitem(update_jira_sp.TRACK_FIELD_ENV, "front", ("JIRA_SP_FRONT_FIELD", "customfield_201"))
        monkeypatch.setitem(update_jira_sp.TRACK_FIELD_ENV, "back", ("JIRA_SP_BACK_FIELD", "customfield_202"))
        self.jira_client.update_story_points_fields.return_value = {
            "customfield_201": True,
            "customfield_202": False,
        }

        updated, failed, skipped = await self.use_case.execute(123, 456, skip_errors=True)

        assert updated == 1
        assert failed == ["TEST-2 SP Back: поле Jira customfield_202 не найдено или запись отклонена"]
        assert skipped == []
