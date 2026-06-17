"""Regression tests for the web-flow use cases extracted out of web_api.

The public ``POST /api/v1/web/vote`` and ``POST /api/v1/web/join`` HTTP
handlers used to embed their mutators inline. Encoding the business rules
as use cases lets us assert their contract without spinning up FastAPI.
"""

from pathlib import Path

import pytest

from app.adapters.session_file import FileSessionRepository
from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.web_join import JoinWebSessionUseCase
from app.usecases.web_vote import WebVoteError, WebVoteUseCase
from config import UserRole


def _temp_repo(name: str) -> tuple[FileSessionRepository, Path]:
    path = Path(f"/tmp/{name}.json")
    if path.exists():
        path.unlink()
    return FileSessionRepository(path), path


# ---------------------------------------------------------------------------
# WebVoteUseCase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_vote_persists_when_voter_is_eligible():
    repo, path = _temp_repo("test_web_vote_eligible")
    try:
        session = Session(chat_id=1, topic_id=None)
        session.participants[-42] = Participant(
            user_id=-42, name="Alice", role=UserRole.PARTICIPANT
        )
        session.tasks_queue.append(Task(jira_key="BB-1", summary="Login"))
        session.current_batch_started_at = "2025-01-01T10:00:00"
        await repo.save_session(session)

        use_case = WebVoteUseCase(repo)
        post = await use_case.execute(1, None, user_id=-42, vote_value="5")

        assert post.current_task.votes[-42] == "5"
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_web_vote_rejects_story_points_above_21():
    repo, path = _temp_repo("test_web_vote_max_21")
    try:
        session = Session(chat_id=1, topic_id=None)
        session.participants[-42] = Participant(
            user_id=-42, name="Alice", role=UserRole.PARTICIPANT
        )
        session.tasks_queue.append(Task(jira_key="BB-1", summary="Login"))
        session.current_batch_started_at = "2025-01-01T10:00:00"
        await repo.save_session(session)

        use_case = WebVoteUseCase(repo)
        with pytest.raises(WebVoteError) as info:
            await use_case.execute(1, None, user_id=-42, vote_value="34")

        assert info.value.status_code == 400
        assert "21 SP" in str(info.value)
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_web_vote_rejects_when_no_active_task_with_400():
    repo, path = _temp_repo("test_web_vote_no_task")
    try:
        session = Session(chat_id=1, topic_id=None)
        session.participants[-42] = Participant(
            user_id=-42, name="Alice", role=UserRole.PARTICIPANT
        )
        # tasks_queue stays empty -> no current_task
        await repo.save_session(session)

        use_case = WebVoteUseCase(repo)
        with pytest.raises(WebVoteError) as info:
            await use_case.execute(1, None, user_id=-42, vote_value="5")
        assert info.value.status_code == 400
    finally:
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_web_vote_rejects_unauthorized_voter_with_403():
    repo, path = _temp_repo("test_web_vote_unauthorized")
    try:
        session = Session(chat_id=1, topic_id=None)
        # The user_id is NOT in session.participants -> can_vote() rejects.
        session.tasks_queue.append(Task(jira_key="BB-1", summary="Login"))
        session.current_batch_started_at = "2025-01-01T10:00:00"
        await repo.save_session(session)

        use_case = WebVoteUseCase(repo)
        with pytest.raises(WebVoteError) as info:
            await use_case.execute(1, None, user_id=-42, vote_value="5")
        assert info.value.status_code == 403
    finally:
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# JoinWebSessionUseCase
# ---------------------------------------------------------------------------


class _MemoryRepo:
    """Minimal repo that supports the ``mutate_session`` contract."""

    def __init__(self, session: Session):
        self.session = session

    async def get_session(self, chat_id: int, topic_id):
        return self.session

    async def save_session(self, session: Session) -> None:
        self.session = session

    async def mutate_session(self, chat_id, topic_id, mutator):
        result = mutator(self.session)
        return self.session, result


@pytest.mark.asyncio
async def test_join_web_session_inserts_new_participant_and_reports_added():
    session = Session(chat_id=1, topic_id=None)
    repo = _MemoryRepo(session)

    use_case = JoinWebSessionUseCase(repo)
    result = await use_case.execute(1, None, user_id=-7, display_name="bob@x.test")

    assert result.added is True
    assert result.session.participants[-7].name == "bob@x.test"
    assert result.session.participants[-7].role == UserRole.PARTICIPANT


@pytest.mark.asyncio
async def test_join_web_session_refreshes_existing_name_without_added_flag():
    session = Session(chat_id=1, topic_id=None)
    session.participants[-7] = Participant(
        user_id=-7, name="old@x.test", role=UserRole.PARTICIPANT
    )
    # Pretend the participant has an in-flight vote; the refresh must not
    # destroy it (page reload semantics).
    task = Task(jira_key="BB-1", summary="task")
    task.votes[-7] = "5"
    session.tasks_queue.append(task)
    repo = _MemoryRepo(session)

    use_case = JoinWebSessionUseCase(repo)
    result = await use_case.execute(1, None, user_id=-7, display_name="new@x.test")

    assert result.added is False
    assert result.session.participants[-7].name == "new@x.test"
    assert result.session.tasks_queue[0].votes[-7] == "5"
