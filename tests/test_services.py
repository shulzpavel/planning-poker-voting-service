"""Tests for usecases and voting policy."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.add_tasks import AddTasksFromJiraUseCase
from app.usecases.show_results import VotingPolicy
from app.usecases.start_batch import StartBatchUseCase
from app.usecases.cast_vote import CastVoteUseCase
from app.usecases.finish_batch import FinishBatchUseCase
from app.usecases.reset_queue import ResetQueueUseCase
from app.adapters.session_file import FileSessionRepository
from config import UserRole


class TestVotingPolicy:
    """Tests for VotingPolicy."""

    def test_get_max_vote(self):
        """Test getting maximum vote value."""
        votes = {1: "3", 2: "3", 3: "5", 4: "13"}
        assert VotingPolicy.get_max_vote(votes) == 13

    def test_get_max_vote_with_skip(self):
        """Test getting maximum vote value ignoring skip votes."""
        votes = {1: "3", 2: "skip", 3: "5", 4: "13", 5: "skip"}
        assert VotingPolicy.get_max_vote(votes) == 13

    def test_get_max_vote_all_skip(self):
        """Test getting maximum vote value when all votes are skip."""
        votes = {1: "skip", 2: "skip", 3: "skip"}
        assert VotingPolicy.get_max_vote(votes) == 0

    def test_get_max_vote_empty(self):
        """Test getting maximum vote value from empty votes."""
        votes = {}
        assert VotingPolicy.get_max_vote(votes) == 0

    def test_get_most_common_vote(self):
        """Test getting most common vote."""
        votes = {1: "5", 2: "8", 3: "5", 4: "5"}
        assert VotingPolicy.get_most_common_vote(votes) == 5

    def test_get_most_common_vote_with_skip(self):
        """Test getting most common vote ignoring skip votes."""
        votes = {1: "3", 2: "skip", 3: "5", 4: "5", 5: "skip"}
        assert VotingPolicy.get_most_common_vote(votes) == 5

    def test_get_most_common_vote_all_skip(self):
        """Test getting most common vote when all votes are skip."""
        votes = {1: "skip", 2: "skip", 3: "skip"}
        assert VotingPolicy.get_most_common_vote(votes) == 0

    def test_calculate_average_vote(self):
        """Test calculating average vote."""
        votes = {1: "5", 2: "8", 3: "3"}
        avg = VotingPolicy.calculate_average_vote(votes)
        assert abs(avg - 5.33) < 0.1

    def test_calculate_average_vote_with_skip(self):
        """Test calculating average vote ignoring skip votes."""
        votes = {1: "3", 2: "skip", 3: "5", 4: "8", 5: "skip"}
        avg = VotingPolicy.calculate_average_vote(votes)
        # Should calculate average of 3, 5, 8 = 16/3 = 5.33
        assert abs(avg - 5.33) < 0.1

    def test_calculate_average_vote_all_skip(self):
        """Test calculating average vote when all votes are skip."""
        votes = {1: "skip", 2: "skip"}
        avg = VotingPolicy.calculate_average_vote(votes)
        assert avg == 0.0


class TestCastVoteUseCase:
    """Tests for CastVoteUseCase."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.use_case = CastVoteUseCase(self.repo)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_all_voters_voted(self):
        """Test checking if all voters voted."""
        session = Session(chat_id=123, topic_id=456)
        task = Task(summary="Test")
        session.tasks_queue.append(task)
        session.current_task_index = 0

        participant1 = Participant(user_id=1, name="User1", role=UserRole.PARTICIPANT)
        participant2 = Participant(user_id=2, name="User2", role=UserRole.PARTICIPANT)
        session.participants[1] = participant1
        session.participants[2] = participant2

        await self.repo.save_session(session)

        assert await self.use_case.all_voters_voted(123, 456) is False

        task.votes[1] = "5"
        await self.repo.save_session(session)
        assert await self.use_case.all_voters_voted(123, 456) is False

        task.votes[2] = "8"
        await self.repo.save_session(session)
        assert await self.use_case.all_voters_voted(123, 456) is True

    @pytest.mark.asyncio
    async def test_all_voters_voted_with_skip(self):
        """Test checking if all voters voted including skip votes."""
        session = Session(chat_id=123, topic_id=456)
        task = Task(summary="Test")
        session.tasks_queue.append(task)
        session.current_task_index = 0

        participant1 = Participant(user_id=1, name="User1", role=UserRole.PARTICIPANT)
        participant2 = Participant(user_id=2, name="User2", role=UserRole.PARTICIPANT)
        session.participants[1] = participant1
        session.participants[2] = participant2

        await self.repo.save_session(session)

        assert await self.use_case.all_voters_voted(123, 456) is False

        task.votes[1] = "5"
        await self.repo.save_session(session)
        assert await self.use_case.all_voters_voted(123, 456) is False

        # Skip vote should count as voted
        task.votes[2] = "skip"
        await self.repo.save_session(session)
        assert await self.use_case.all_voters_voted(123, 456) is True


class TestAddTasksFromJiraUseCase:
    """Tests for AddTasksFromJiraUseCase deduplication."""

    @pytest.mark.asyncio
    async def test_skips_tasks_in_history(self):
        """Tasks with jira_key already in history are not re-added."""
        temp_file = Path("/tmp/test_add_tasks_dedup.json")
        if temp_file.exists():
            temp_file.unlink()
        try:
            repo = FileSessionRepository(temp_file)
            jira_client = AsyncMock()
            jira_client.parse_jira_request.return_value = [
                {"key": "PROJ-1", "summary": "Task 1", "url": None},
                {"key": "PROJ-2", "summary": "Task 2", "url": None},
            ]
            use_case = AddTasksFromJiraUseCase(jira_client, repo)

            session = Session(chat_id=123, topic_id=456)
            task_in_history = Task(jira_key="PROJ-1", summary="Done", url=None)
            session.history.append(task_in_history)
            await repo.save_session(session)

            added, skipped = await use_case.execute(123, 456, "project = PROJ")
            assert len(added) == 1
            assert added[0].jira_key == "PROJ-2"
            assert "PROJ-1" in skipped

            session = await repo.get_session(123, 456)
            assert len(session.tasks_queue) == 1
            assert session.tasks_queue[0].jira_key == "PROJ-2"
        finally:
            if temp_file.exists():
                temp_file.unlink()


class TestStartBatchUseCase:
    """Tests for StartBatchUseCase."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.use_case = StartBatchUseCase(self.repo)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_start_voting_session(self):
        """Test starting voting session."""
        session = Session(chat_id=123, topic_id=456)
        task = Task(summary="Test")
        session.tasks_queue.append(task)
        await self.repo.save_session(session)

        result = await self.use_case.execute(123, 456)
        assert result is True

        session = await self.repo.get_session(123, 456)
        assert session.current_task_index == 0
        assert session.batch_completed is False
        assert session.current_batch_started_at is not None

    @pytest.mark.asyncio
    async def test_start_voting_session_empty(self):
        """Test starting voting session with no tasks."""
        session = Session(chat_id=123, topic_id=456)
        await self.repo.save_session(session)

        result = await self.use_case.execute(123, 456)
        assert result is False


class TestFinishBatchUseCase:
    """Tests for FinishBatchUseCase."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.use_case = FinishBatchUseCase(self.repo)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_finish_batch(self):
        """Test finishing a batch."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        task2 = Task(summary="Task 2")
        session.tasks_queue = [task1, task2]
        await self.repo.save_session(session)

        completed = await self.use_case.execute(123, 456)
        assert len(completed) == 2

        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert len(session.last_batch) == 2
        assert len(session.history) == 2
        assert session.batch_completed is True

    @pytest.mark.asyncio
    async def test_finish_batch_resets_current_batch_started_at(self):
        """Test that finish_batch resets current_batch_started_at."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        session.tasks_queue = [task1]
        session.current_batch_started_at = "2024-01-01T00:00:00"
        await self.repo.save_session(session)

        await self.use_case.execute(123, 456)

        session = await self.repo.get_session(123, 456)
        assert session.current_batch_started_at is None


class TestResetQueueUseCase:
    """Tests for ResetQueueUseCase."""

    def setup_method(self):
        """Setup test fixtures."""
        self.temp_file = Path("/tmp/test_state.json")
        if self.temp_file.exists():
            self.temp_file.unlink()
        self.repo = FileSessionRepository(self.temp_file)
        self.use_case = ResetQueueUseCase(self.repo)

    def teardown_method(self):
        """Cleanup test fixtures."""
        if self.temp_file.exists():
            self.temp_file.unlink()

    @pytest.mark.asyncio
    async def test_reset_tasks_queue(self):
        """Test resetting tasks queue."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        task2 = Task(summary="Task 2")
        task3 = Task(summary="Task 3")
        session.tasks_queue = [task1, task2, task3]
        session.current_task_index = 2
        session.batch_completed = False
        session.current_batch_started_at = "2024-01-01T00:00:00"
        session.current_batch_id = "batch-123"
        session.active_vote_message_id = 999
        await self.repo.save_session(session)

        task_count = await self.use_case.execute(123, 456)
        assert task_count == 3

        session = await self.repo.get_session(123, 456)
        assert len(session.tasks_queue) == 0
        assert session.current_task_index == 0
        assert session.batch_completed is False
        assert session.current_batch_started_at is None
        assert session.current_batch_id is None
        assert session.active_vote_message_id is None

    @pytest.mark.asyncio
    async def test_reset_tasks_queue_preserves_history(self):
        """Test that reset_tasks_queue preserves history and last_batch."""
        session = Session(chat_id=123, topic_id=456)
        task1 = Task(summary="Task 1")
        task2 = Task(summary="Task 2")
        task3 = Task(summary="Task 3")
        session.tasks_queue = [task1, task2]
        session.history = [task3]
        session.last_batch = [task3]
        await self.repo.save_session(session)

        await self.use_case.execute(123, 456)

        session = await self.repo.get_session(123, 456)
        assert len(session.history) == 1
        assert len(session.last_batch) == 1
        assert session.history[0].summary == task3.summary
        assert session.last_batch[0].summary == task3.summary
        assert len(session.tasks_queue) == 0
