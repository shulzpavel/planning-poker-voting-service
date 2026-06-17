"""Tests for SessionState <-> Session conversion (session_file adapter).

Mapping: SessionState (session_store) has global `votes` for current task.
Session (domain) stores votes inside each Task. FileSessionRepository converts:
- state_to_session: tasks_queue items become Task (with votes inside)
- session_to_state: current_task.votes -> state.votes (for backward compat)
"""

import pytest
from pathlib import Path

from app.adapters.session_file import FileSessionRepository
from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from config import UserRole
from session_store import SessionState


@pytest.fixture
def temp_state_file(tmp_path):
    """Temporary state file for tests."""
    return tmp_path / "state.json"


@pytest.fixture
def repo(temp_state_file):
    """FileSessionRepository instance."""
    return FileSessionRepository(temp_state_file)


@pytest.mark.asyncio
async def test_session_roundtrip_preserves_data(repo, temp_state_file):
    """Session -> state -> session roundtrip preserves all fields."""
    session = Session(
        chat_id=123,
        topic_id=456,
        participants={
            1: Participant(user_id=1, name="Alice", role=UserRole.LEAD),
            2: Participant(user_id=2, name="Bob", role=UserRole.PARTICIPANT),
        },
        tasks_queue=[
            Task(jira_key="KEY-1", summary="Task 1", votes={1: "5", 2: "8"}),
            Task(jira_key="KEY-2", summary="Task 2", votes={1: "3"}),
        ],
        current_task_index=1,
        history=[Task(summary="Done", votes={}, completed_at="2025-01-01")],
        last_batch=[Task(summary="Batch", votes={1: "5"})],
        batch_completed=True,
        active_vote_message_id=999,
        current_batch_id="batch-123",
        current_batch_started_at="2025-01-01T12:00:00",
    )

    await repo.save_session(session)
    loaded = await repo.get_session(123, 456)

    assert loaded.chat_id == session.chat_id
    assert loaded.topic_id == session.topic_id
    assert len(loaded.participants) == 2
    assert loaded.participants[1].name == "Alice"
    assert loaded.participants[2].role == UserRole.PARTICIPANT
    assert len(loaded.tasks_queue) == 2
    assert loaded.tasks_queue[0].jira_key == "KEY-1"
    assert loaded.tasks_queue[0].votes == {1: "5", 2: "8"}
    assert loaded.tasks_queue[1].votes == {1: "3"}
    assert loaded.current_task_index == 1
    assert loaded.current_task.summary == "Task 2"
    assert len(loaded.history) == 1
    assert len(loaded.last_batch) == 1
    assert loaded.batch_completed is True
    assert loaded.active_vote_message_id == 999
    assert loaded.current_batch_id == "batch-123"
    assert loaded.current_batch_started_at == "2025-01-01T12:00:00"


@pytest.mark.asyncio
async def test_votes_stored_in_tasks_not_global_state(repo):
    """Votes live inside Task objects; SessionState.votes is derived on save."""
    session = Session(chat_id=1, topic_id=None)
    task = Task(summary="T", votes={100: "5", 200: "8"})
    session.tasks_queue = [task]
    session.current_task_index = 0

    await repo.save_session(session)
    loaded = await repo.get_session(1, None)

    assert loaded.current_task.votes == {100: "5", 200: "8"}
    assert loaded.current_task.summary == "T"


@pytest.mark.asyncio
async def test_empty_session_roundtrip(repo):
    """Empty session roundtrips correctly."""
    session = Session(chat_id=0, topic_id=None)
    await repo.save_session(session)
    loaded = await repo.get_session(0, None)

    assert loaded.chat_id == 0
    assert loaded.topic_id is None
    assert not loaded.participants
    assert not loaded.tasks_queue
    assert loaded.current_task is None
    assert loaded.current_task_index == 0
