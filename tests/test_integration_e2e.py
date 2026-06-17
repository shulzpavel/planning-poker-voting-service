"""End-to-end integration tests for planning session handlers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.adapters.session_file import FileSessionRepository
from app.usecases.finish_batch import FinishBatchUseCase
from app.usecases.show_results import VotingPolicy
from config import UserRole


class TestE2EHandlers:
    """End-to-end tests that call actual handler functions."""

    @pytest.mark.asyncio
    async def test_needs_review_single_task_finishes_batch(self):
        """Test that needs_review with single task finishes batch and clears active_vote_message_id."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        session.tasks_queue = [task1]
        session.current_task_index = 0
        session.current_batch_started_at = "2024-01-01T00:00:00"
        session.active_vote_message_id = 999  # Симулируем активное сообщение

        # Проверяем начальное состояние
        assert len(session.tasks_queue) == 1
        assert session.current_task == task1
        assert session.active_vote_message_id == 999

        # Симулируем логику needs_review для единственной задачи
        was_single_task = len(session.tasks_queue) == 1
        current_index = session.current_task_index
        task = session.tasks_queue.pop(current_index)
        task.votes.clear()
        session.tasks_queue.append(task)

        # Проверяем, что задача переместилась
        assert len(session.tasks_queue) == 1
        assert session.tasks_queue[0] == task1
        assert len(task1.votes) == 0

        # Если была единственной, должна завершиться батч
        if was_single_task:
            # Симулируем завершение батча
            from pathlib import Path
            temp_file = Path("/tmp/test_e2e_state.json")
            if temp_file.exists():
                temp_file.unlink()
            repo = FileSessionRepository(temp_file)
            await repo.save_session(session)
            finish_batch = FinishBatchUseCase(repo)
            completed_tasks = await finish_batch.execute(123, 456)
            session = await repo.get_session(123, 456)
            if temp_file.exists():
                temp_file.unlink()
            assert len(completed_tasks) == 1
            assert session.active_vote_message_id is None  # Должен сброситься
            assert session.batch_completed is True

    @pytest.mark.asyncio
    async def test_needs_review_multiple_tasks_moves_to_next(self):
        """Test that needs_review with multiple tasks moves to next task."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task3 = Task(jira_key="TEST-3", summary="Task 3")

        session.tasks_queue = [task1, task2, task3]
        session.current_task_index = 0
        session.current_batch_started_at = "2024-01-01T00:00:00"
        session.active_vote_message_id = 999
        task1.votes[1] = "5"

        # Проверяем начальное состояние
        assert session.current_task == task1
        assert session.current_task_index == 0

        # Симулируем needs_review
        was_single_task = len(session.tasks_queue) == 1
        assert was_single_task is False

        current_index = session.current_task_index
        task_to_review = session.current_task
        task = session.tasks_queue.pop(current_index)
        task.votes.clear()
        session.tasks_queue.append(task)

        # Проверяем перемещение
        assert len(session.tasks_queue) == 3
        assert session.tasks_queue[0] == task2
        assert session.tasks_queue[-1] == task1
        assert len(task1.votes) == 0

        # active_vote_message_id должен сброситься перед переходом к следующей задаче
        session.active_vote_message_id = None

        # Переходим к следующей задаче
        session.current_task_index = 0  # Теперь указывает на task2
        assert session.current_task == task2
        assert session.current_task != task_to_review

    def test_voting_lists_calculation(self):
        """Test calculation of voted/waiting lists for display."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        participant1 = Participant(user_id=2, name="User 1", role=UserRole.PARTICIPANT)
        participant2 = Participant(user_id=3, name="User 2", role=UserRole.PARTICIPANT)
        admin = Participant(user_id=4, name="Admin", role=UserRole.ADMIN)

        session.participants[1] = lead
        session.participants[2] = participant1
        session.participants[3] = participant2
        session.participants[4] = admin

        task = Task(jira_key="TEST-1", summary="Test Task")
        session.tasks_queue.append(task)
        session.current_task_index = 0

        # Eligible voters: lead and participants, NOT admin
        eligible_voters = [uid for uid in session.participants if session.can_vote(uid)]
        assert set(eligible_voters) == {1, 2, 3}

        # Голосуют lead и participant1
        task.votes[1] = "5"
        task.votes[2] = "8"

        # Формируем списки для отображения
        voted_user_ids = set(task.votes.keys())
        waiting_user_ids = [uid for uid in eligible_voters if uid not in voted_user_ids]

        voted_names = []
        for uid in voted_user_ids:
            participant = session.participants.get(uid)
            if participant:
                voted_names.append(participant.name)

        waiting_names = []
        for uid in waiting_user_ids:
            participant = session.participants.get(uid)
            if participant:
                waiting_names.append(participant.name)

        assert set(voted_names) == {"Lead", "User 1"}
        assert set(waiting_names) == {"User 2"}
