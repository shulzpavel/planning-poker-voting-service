"""Use case for force-finalizing a planning session.

Both the manager-driven ``POST /app/sessions/{chat_id}/finish`` and the
admin-driven ``POST /cms/sessions/{session_id}/close`` HTTP endpoints used
to carry their own (subtly different) copy of the finalization mutator,
which was a maintenance hazard — they could drift, and they had drifted
(CMS replaced ``last_batch`` outright while the manager finish accumulated
into it).

This use case is the single source of truth for "drain the active queue
into history and mark the batch complete". It is idempotent: invoking it
twice in a row is safe — the second call sees an empty ``tasks_queue`` and
re-applies the terminal flags only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.domain.session import Session
from app.domain.task import Task
from app.ports.session_repository import SessionRepository


def _build_close_mutator():
    """Return a mutator suitable for ``SessionRepository.mutate_session``."""

    def mutate(session: Session) -> list[Task]:
        finished_at = datetime.utcnow().isoformat()
        pending = list(session.tasks_queue)
        if pending:
            for task in pending:
                if not task.completed_at:
                    task.completed_at = finished_at
            # Append rather than overwrite: a session can have prior
            # ``last_batch`` content if the manager added tasks after the
            # previous explicit finish (or auto-next-on-last has already
            # migrated some tasks). The summary screen and CSV export expect
            # everything that played in the active period.
            session.last_batch.extend(pending)
            session.history.extend(pending)
            session.tasks_queue.clear()
        session.current_task_index = 0
        session.batch_completed = True
        session.active_vote_message_id = None
        session.current_batch_started_at = None
        session.revealed_task_id = None
        session.bump_tasks_version()
        return list(session.last_batch)

    return mutate


class CloseSessionUseCase:
    """Force-finalize a session. Idempotent."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
    ) -> tuple[Session, list[Task]]:
        """Drain pending tasks into ``last_batch``/``history`` and mark the
        batch complete.

        Returns the post-mutation session together with the resulting
        ``last_batch`` so callers can broadcast new state and audit the
        completed task count without re-fetching.
        """
        repo = self.session_repo
        mutator = _build_close_mutator()

        if hasattr(repo, "mutate_session"):
            session, completed = await repo.mutate_session(chat_id, topic_id, mutator)
            return session, completed

        session = await _get_session(repo, chat_id, topic_id)
        completed = mutator(session)
        await _save_session(repo, session)
        return session, completed


async def _get_session(repo: SessionRepository, chat_id: int, topic_id: Optional[int]) -> Session:
    if hasattr(repo, "get_session_async"):
        return await repo.get_session_async(chat_id, topic_id)  # type: ignore[attr-defined]
    return await repo.get_session(chat_id, topic_id)


async def _save_session(repo: SessionRepository, session: Session) -> None:
    if hasattr(repo, "save_session_async"):
        await repo.save_session_async(session)  # type: ignore[attr-defined]
        return
    await repo.save_session(session)
