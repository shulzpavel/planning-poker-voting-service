"""File-based adapter for session repository.

Mapping SessionState (session_store) <-> Session (domain):
- SessionState.votes: legacy global field; on load ignored (votes come from tasks_queue).
- Session stores votes inside Task.votes.
- On save: state.votes = session.current_task.votes (for backward compat).
- tasks_queue, history, last_batch: lists of Task <-> dict with 'votes' key.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Optional, TypeVar

from app.domain.session import Session, SessionFactory
from app.ports.session_repository import SessionRepository
from session_store import SessionState, SessionStore

MutationResult = TypeVar("MutationResult")


class FileSessionRepository(SessionRepository):
    """File-based implementation of session repository."""

    def __init__(self, state_file: Path):
        self.store = SessionStore(state_file)
        self._mutation_lock = asyncio.Lock()

    async def get_session(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session."""
        session_state = self.store.get_session(chat_id, topic_id)
        return self._state_to_session(session_state)

    async def save_session(self, session: Session) -> None:
        """Save session state."""
        session_state = self._session_to_state(session)
        self.store.save_session(session_state)

    async def delete_session(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session."""
        self.store.delete_session(chat_id, topic_id)

    async def mutate_session(
        self,
        chat_id: int,
        topic_id: Optional[int],
        mutator: Callable[[Session], MutationResult],
    ) -> tuple[Session, MutationResult]:
        """Read-modify-write session under a process-local lock."""
        async with self._mutation_lock:
            session_state = self.store.get_session(chat_id, topic_id)
            session = self._state_to_session(session_state)
            result = mutator(session)
            self.store.save_session(self._session_to_state(session))
            return session, result

    def _state_to_session(self, state: SessionState) -> Session:
        """Convert SessionState to Session model."""
        return SessionFactory.from_dict(state.to_dict(), state.chat_id, state.topic_id)

    async def close(self) -> None:
        """No-op for parity with networked repositories.

        Lifespan shutdown calls close() generically across all adapters; the
        file-backed adapter has nothing to release but must expose the method
        so a local-dev run without Redis/Postgres can still shut down cleanly.
        """
        return None

    def _session_to_state(self, session: Session) -> SessionState:
        """Convert Session model to SessionState."""
        data = SessionFactory.to_dict(session)

        # Convert votes to the format expected by SessionState
        votes = {}
        if session.current_task:
            votes = session.current_task.votes

        state = SessionState(
            chat_id=data["chat_id"],
            topic_id=data["topic_id"],
            participants=session.participants and {uid: p.to_dict() for uid, p in session.participants.items()},
            votes=votes,
            tasks_queue=data["tasks_queue"],
            current_task_index=data["current_task_index"],
            history=data["history"],
            last_batch=data["last_batch"],
            batch_completed=data["batch_completed"],
            active_vote_message_id=data["active_vote_message_id"],
            current_batch_id=data["current_batch_id"],
            current_batch_started_at=data["current_batch_started_at"],
            tasks_version=data["tasks_version"],
            estimation_mode=data["estimation_mode"],
        )
        return state
