"""Integration tests for full planning workflow."""

import pytest
from pathlib import Path

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.adapters.session_file import FileSessionRepository
from app.usecases.start_batch import StartBatchUseCase
from app.usecases.cast_vote import CastVoteUseCase
from app.usecases.finish_batch import FinishBatchUseCase
from app.usecases.reset_queue import ResetQueueUseCase
from app.usecases.show_results import VotingPolicy
from config import UserRole


class TestFullWorkflow:
    """Integration tests for complete planning workflow."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_integration_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.start_batch = StartBatchUseCase(self.repo)
        self.cast_vote = CastVoteUseCase(self.repo)
        self.finish_batch = FinishBatchUseCase(self.repo)
        self.reset_queue = ResetQueueUseCase(self.repo)
        self.policy = VotingPolicy()

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_full_workflow_with_reset(self):
        """Test complete workflow: create session → add tasks → vote → reset queue."""
        # 1. Создаём сессию
        session = Session(chat_id=123, topic_id=456)

        # Добавляем участников
        lead = Participant(user_id=1, name="Lead User", role=UserRole.LEAD)
        participant1 = Participant(user_id=2, name="User 1", role=UserRole.PARTICIPANT)
        participant2 = Participant(user_id=3, name="User 2", role=UserRole.PARTICIPANT)

        session.participants[1] = lead
        session.participants[2] = participant1
        session.participants[3] = participant2

        # 2. Добавляем две задачи
        task1 = Task(jira_key="TEST-1", summary="Test Task 1", url="https://test.com/TEST-1")
        task2 = Task(jira_key="TEST-2", summary="Test Task 2", url="https://test.com/TEST-2")

        session.tasks_queue.append(task1)
        session.tasks_queue.append(task2)

        assert len(session.tasks_queue) == 2
        assert len(session.history) == 0
        assert len(session.last_batch) == 0

        await self.repo.save_session(session)

        # 3. Старт голосования
        result = await self.start_batch.execute(123, 456)
        assert result is True

        session = await self.repo.get_session(123, 456)
        assert session.current_task_index == 0
        assert session.current_task.jira_key == task1.jira_key
        assert session.current_batch_started_at is not None
        assert session.is_voting_active is True

        # 4. Голосуем по первой задаче (с одним skip)
        # Lead голосует
        await self.cast_vote.execute(123, 456, 1, "5")
        # Participant1 голосует
        await self.cast_vote.execute(123, 456, 2, "8")
        # Participant2 пропускает
        await self.cast_vote.execute(123, 456, 3, "skip")

        # Проверяем, что все проголосовали
        assert await self.cast_vote.all_voters_voted(123, 456) is True

        # Переходим к следующей задаче (вручную, так как usecase не делает это автоматически)
        session = await self.repo.get_session(123, 456)
        session.current_task_index += 1
        await self.repo.save_session(session)

        session = await self.repo.get_session(123, 456)
        assert session.current_task == task2
        assert session.current_task_index == 1
        # Проверяем голоса в истории (task1 теперь в истории после перехода)
        # Или проверяем через session.history, если задача уже завершена
        # Но в данном случае задача еще в очереди, нужно проверить через индекс
        if len(session.tasks_queue) > 0:
            # task1 теперь на индексе 0 (после перехода к task2)
            previous_task = session.tasks_queue[0] if session.current_task_index > 0 else None
            if previous_task:
                assert len(previous_task.votes) == 3
        # Альтернативно: проверяем что голоса сохранились в task1 через поиск по jira_key
        task1_from_repo = next((t for t in session.tasks_queue if t.jira_key == "TEST-1"), None)
        if task1_from_repo:
            assert len(task1_from_repo.votes) == 3

        # 5. Голосуем по второй задаче
        await self.cast_vote.execute(123, 456, 1, "3")
        await self.cast_vote.execute(123, 456, 2, "3")
        await self.cast_vote.execute(123, 456, 3, "5")

        assert await self.cast_vote.all_voters_voted(123, 456) is True

        # Завершаем батч
        completed_tasks = await self.finish_batch.execute(123, 456)
        assert len(completed_tasks) == 2

        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert len(session.last_batch) == 2
        assert len(session.history) == 2
        assert session.batch_completed is True
        assert session.is_voting_active is False

        # Проверяем, что история сохранилась (сравниваем по содержимому, не по ссылке)
        assert session.history[0].jira_key == task1.jira_key
        assert session.history[1].jira_key == task2.jira_key
        assert session.last_batch[0].jira_key == task1.jira_key
        assert session.last_batch[1].jira_key == task2.jira_key

        # Проверяем голоса в истории
        assert len(session.history[0].votes) == 3
        assert session.history[0].votes[3] == "skip"
        assert self.policy.get_max_vote(session.history[0].votes) == 8  # Максимум из 5, 8

        # 6. Добавляем новые задачи для теста сброса
        task3 = Task(jira_key="TEST-3", summary="Test Task 3", url="https://test.com/TEST-3")
        task4 = Task(jira_key="TEST-4", summary="Test Task 4", url="https://test.com/TEST-4")
        session.tasks_queue.append(task3)
        session.tasks_queue.append(task4)

        assert len(session.tasks_queue) == 2

        await self.repo.save_session(session)

        # 7. Сбрасываем очередь
        task_count = await self.reset_queue.execute(123, 456)

        # Проверяем, что очередь очищена
        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert session.current_task_index == 0
        assert session.current_batch_started_at is None
        assert session.current_batch_id is None
        assert session.active_vote_message_id is None

        # Проверяем, что история и last_batch сохранены (сравниваем по содержимому)
        assert len(session.history) == 2
        assert len(session.last_batch) == 2
        assert session.history[0].jira_key == task1.jira_key
        assert session.history[1].jira_key == task2.jira_key
        assert session.last_batch[0].jira_key == task1.jira_key
        assert session.last_batch[1].jira_key == task2.jira_key

    @pytest.mark.asyncio
    async def test_needs_review_functionality(self):
        """Test 'needs review' functionality - task moved to end of queue."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task3 = Task(jira_key="TEST-3", summary="Task 3")

        session.tasks_queue = [task1, task2, task3]
        session.current_task_index = 0  # Начинаем с task1

        # Добавляем голоса в task1
        task1.votes[1] = "5"

        await self.repo.save_session(session)

        # Симулируем "Нужен пересмотр" - возвращаем task1 в конец
        session = await self.repo.get_session(123, 456)
        current_task = session.tasks_queue[session.current_task_index]
        assert current_task.jira_key == task1.jira_key

        # Перемещаем задачу в конец
        task = session.tasks_queue.pop(session.current_task_index)
        task.votes.clear()
        session.tasks_queue.append(task)

        # Проверяем, что task1 теперь в конце
        assert len(session.tasks_queue) == 3
        assert session.tasks_queue[0].jira_key == task2.jira_key  # Первая теперь task2
        assert session.tasks_queue[-1].jira_key == task1.jira_key  # Последняя теперь task1
        assert len(session.tasks_queue[-1].votes) == 0  # Голоса очищены (используем объект из репозитория)

        await self.repo.save_session(session)

    @pytest.mark.asyncio
    async def test_voting_lists_display(self):
        """Test that voting lists (voted/waiting) are correctly calculated."""
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

        await self.repo.save_session(session)

        # Старт голосования (иначе cast_vote отклоняется)
        await self.start_batch.execute(123, 456)

        # Eligible voters: lead (1) and participants (2, 3), but NOT admin (4)
        session = await self.repo.get_session(123, 456)
        eligible_voters = [uid for uid in session.participants if session.can_vote(uid)]
        assert set(eligible_voters) == {1, 2, 3}
        assert 4 not in eligible_voters

        # Голосуют lead и participant1
        await self.cast_vote.execute(123, 456, 1, "5")
        await self.cast_vote.execute(123, 456, 2, "8")

        session = await self.repo.get_session(123, 456)
        voted_user_ids = set(session.current_task.votes.keys())
        waiting_user_ids = [uid for uid in eligible_voters if uid not in voted_user_ids]

        assert voted_user_ids == {1, 2}
        assert waiting_user_ids == [3]  # Только participant2 ждёт
