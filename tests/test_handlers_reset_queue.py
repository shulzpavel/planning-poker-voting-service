"""Tests for reset queue handlers (access control and behavior)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.adapters.session_file import FileSessionRepository
from app.usecases.reset_queue import ResetQueueUseCase
from config import UserRole


class TestResetQueueAccess:
    """Tests for reset queue access control."""

    def setup_method(self):
        """Setup test fixtures."""
        from pathlib import Path
        self.temp_file = Path("/tmp/test_reset_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.use_case = ResetQueueUseCase(self.repo)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_reset_tasks_queue_preserves_history_and_last_batch(self):
        """Test that reset_tasks_queue preserves history and last_batch."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        task2 = Task(summary="Task 2")
        task3 = Task(summary="Task 3")
        session.tasks_queue = [task1, task2]
        session.history = [task3]  # Предыдущие задачи в истории
        session.last_batch = [task3]  # Последний батч

        await self.repo.save_session(session)
        await self.use_case.execute(123, 456)

        session = await self.repo.get_session(123, 456)
        # История и last_batch должны сохраниться
        assert len(session.history) == 1
        assert len(session.last_batch) == 1
        assert session.history[0].summary == task3.summary
        assert session.last_batch[0].summary == task3.summary
        # Очередь очищена
        assert len(session.tasks_queue) == 0

    def test_reset_tasks_queue_only_manage_roles(self):
        """Test that only manage roles (LEAD, ADMIN) can reset queue."""
        session = Session(chat_id=123, topic_id=456)
        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        admin = Participant(user_id=2, name="Admin", role=UserRole.ADMIN)
        participant = Participant(user_id=3, name="User", role=UserRole.PARTICIPANT)

        session.participants[1] = lead
        session.participants[2] = admin
        session.participants[3] = participant

        # Только лид и админ могут управлять
        assert session.can_manage(1) is True  # LEAD
        assert session.can_manage(2) is True  # ADMIN
        assert session.can_manage(3) is False  # PARTICIPANT

    @pytest.mark.asyncio
    async def test_reset_tasks_queue_clears_all_state_fields(self):
        """Test that reset_tasks_queue clears all voting state fields."""
        session = Session(chat_id=123, topic_id=456)
        task = Task(summary="Test")
        session.tasks_queue = [task]
        session.current_task_index = 1
        session.batch_completed = True
        session.current_batch_started_at = "2024-01-01T00:00:00"
        session.current_batch_id = "batch-123"
        session.active_vote_message_id = 999

        await self.repo.save_session(session)
        await self.use_case.execute(123, 456)

        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert session.current_task_index == 0
        assert session.batch_completed is False
        assert session.current_batch_started_at is None
        assert session.current_batch_id is None
        assert session.active_vote_message_id is None

    @pytest.mark.asyncio
    async def test_reset_tasks_queue_during_active_voting(self):
        """Test resetting queue during active voting."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        task2 = Task(summary="Task 2")
        session.tasks_queue = [task1, task2]
        session.current_task_index = 0
        session.current_batch_started_at = "2024-01-01T00:00:00"
        session.active_vote_message_id = 12345
        task1.votes = {1: "5", 2: "8"}

        await self.repo.save_session(session)

        # Проверяем, что голосование активно
        session = await self.repo.get_session(123, 456)
        assert session.is_voting_active is True

        await self.use_case.execute(123, 456)

        session = await self.repo.get_session(123, 456)
        # Голосование должно быть остановлено
        assert session.is_voting_active is False
        assert len(session.tasks_queue) == 0
        assert session.active_vote_message_id is None

    @pytest.mark.asyncio
    async def test_reset_tasks_queue_empty_queue(self):
        """Test resetting already empty queue."""
        session = Session(chat_id=123, topic_id=456)
        session.tasks_queue = []
        session.current_task_index = 0

        await self.repo.save_session(session)

        # Должно работать без ошибок
        task_count = await self.use_case.execute(123, 456)
        assert task_count == 0

        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert session.current_task_index == 0
        assert session.current_batch_started_at is None
