"""Tests for models."""

import pytest

from app.domain.participant import Participant
from app.domain.session import Session, SessionFactory
from app.domain.task import Task
from config import UserRole


class TestTask:
    """Tests for Task model."""

    def test_task_creation(self):
        """Test task creation."""
        task = Task(
            jira_key="TEST-1",
            summary="Test task",
            url="https://test.com/TEST-1",
            story_points=5,
        )
        assert task.jira_key == "TEST-1"
        assert task.summary == "Test task"
        assert task.url == "https://test.com/TEST-1"
        assert task.story_points == 5

    def test_task_to_dict(self):
        """Test task serialization."""
        task = Task(
            jira_key="TEST-1",
            summary="Test task",
            votes={1: "5", 2: "8"},
        )
        data = task.to_dict()
        assert data["jira_key"] == "TEST-1"
        assert data["summary"] == "Test task"
        assert "1" in data["votes"]
        assert data["votes"]["1"] == "5"

    def test_task_from_dict(self):
        """Test task deserialization."""
        data = {
            "jira_key": "TEST-1",
            "summary": "Test task",
            "votes": {"1": "5", "2": "8"},
        }
        task = Task.from_dict(data)
        assert task.jira_key == "TEST-1"
        assert task.summary == "Test task"
        assert 1 in task.votes
        assert task.votes[1] == "5"

    def test_task_text_property(self):
        """Test task text property."""
        task = Task(summary="Test", url="https://test.com")
        assert "Test" in task.text
        assert "https://test.com" in task.text


class TestParticipant:
    """Tests for Participant model."""

    def test_participant_creation(self):
        """Test participant creation."""
        participant = Participant(
            user_id=123,
            name="Test User",
            role=UserRole.PARTICIPANT,
        )
        assert participant.user_id == 123
        assert participant.name == "Test User"
        assert participant.role == UserRole.PARTICIPANT

    def test_participant_to_dict(self):
        """Test participant serialization."""
        participant = Participant(
            user_id=123,
            name="Test User",
            role=UserRole.LEAD,
        )
        data = participant.to_dict()
        assert data["name"] == "Test User"
        assert data["role"] == UserRole.LEAD.value

    def test_participant_from_dict(self):
        """Test participant deserialization."""
        data = {"name": "Test User", "role": "lead"}
        participant = Participant.from_dict(123, data)
        assert participant.user_id == 123
        assert participant.name == "Test User"
        assert participant.role == UserRole.LEAD


class TestSession:
    """Tests for Session model."""

    def test_session_creation(self):
        """Test session creation."""
        session = Session(chat_id=123, topic_id=456)
        assert session.chat_id == 123
        assert session.topic_id == 456
        assert len(session.participants) == 0
        assert len(session.tasks_queue) == 0

    def test_current_task_property(self):
        """Test current task property."""
        session = Session(chat_id=123, topic_id=456)
        assert session.current_task is None

        task = Task(summary="Test")
        session.tasks_queue.append(task)
        session.current_task_index = 0
        assert session.current_task == task

    def test_is_voting_active(self):
        """Test voting active flag."""
        session = Session(chat_id=123, topic_id=456)
        assert session.is_voting_active is False

        task = Task(summary="Test")
        session.tasks_queue.append(task)
        session.current_task_index = 0
        assert session.is_voting_active is False

        session.current_batch_started_at = "now"
        assert session.is_voting_active is True

        session.batch_completed = True
        assert session.is_voting_active is False

    def test_can_vote(self):
        """Test can_vote method."""
        session = Session(chat_id=123, topic_id=456)
        participant = Participant(user_id=1, name="User", role=UserRole.PARTICIPANT)
        session.participants[1] = participant

        assert session.can_vote(1) is True
        assert session.can_vote(999) is False

        admin = Participant(user_id=2, name="Admin", role=UserRole.ADMIN)
        session.participants[2] = admin
        assert session.can_vote(2) is False

    def test_can_manage(self):
        """Test can_manage method."""
        session = Session(chat_id=123, topic_id=456)
        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        assert session.can_manage(1) is True

        participant = Participant(user_id=2, name="User", role=UserRole.PARTICIPANT)
        session.participants[2] = participant
        assert session.can_manage(2) is False

    def test_session_factory_roundtrip(self):
        """Test centralized session serialization."""
        session = Session(chat_id=123, topic_id=456)
        session.tasks_queue = [Task(jira_key="TEST-1", summary="Task 1", source="jira")]
        session.tasks_version = 7
        session.revealed_task_id = session.tasks_queue[0].task_id

        loaded = SessionFactory.from_dict(SessionFactory.to_dict(session))

        assert loaded.chat_id == 123
        assert loaded.topic_id == 456
        assert loaded.tasks_version == 7
        assert loaded.revealed_task_id == session.revealed_task_id
        assert loaded.tasks_queue[0].task_id == session.tasks_queue[0].task_id
        assert loaded.tasks_queue[0].source == "jira"

    def test_session_factory_assigns_unique_legacy_ids_for_duplicate_tasks(self):
        """Legacy queue items without task_id must not collide by content only."""
        payload = {
            "chat_id": 123,
            "topic_id": None,
            "tasks_queue": [
                {"summary": "Same manual task", "source": "manual"},
                {"summary": "Same manual task", "source": "manual"},
            ],
        }

        loaded = SessionFactory.from_dict(payload)

        assert loaded.tasks_queue[0].task_id != loaded.tasks_queue[1].task_id
        assert loaded.tasks_queue[0].task_id.startswith("legacy-")
        assert loaded.tasks_queue[1].task_id.startswith("legacy-")

    def test_revealed_task_id_switches_web_state_to_results(self):
        """Manual reveal should expose results before every voter has voted."""
        pytest.importorskip("redis")
        from services.voting_service.web_api import _build_web_session_state

        session = Session(chat_id=123, topic_id=None)
        task = Task(summary="Estimate checkout")
        session.tasks_queue = [task]
        session.current_batch_started_at = "now"
        session.participants[1] = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[2] = Participant(user_id=2, name="Dev", role=UserRole.PARTICIPANT)
        task.votes[1] = "5"

        assert _build_web_session_state(session)["phase"] == "voting"

        session.revealed_task_id = task.task_id

        state = _build_web_session_state(session)
        assert state["phase"] == "results"
        assert state["results"] == [{"name": "Lead", "value": "5"}]
