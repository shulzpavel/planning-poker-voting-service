"""Use case for advancing to the next voting task."""

from typing import Optional, Tuple

from app.domain.session import Session
from app.domain.task import Task
from app.ports.session_repository import SessionRepository


class AdvanceToNextTaskUseCase:
    """Use case for advancing to the next task after all voters voted."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(self, chat_id: int, topic_id: Optional[int]) -> Tuple[bool, Optional[Task]]:
        """Advance to next task. Returns (batch_finished, next_task)."""
        session = await self.session_repo.get_session(chat_id, topic_id)

        if not session.current_task:
            return True, None

        session.current_task_index += 1
        session.revealed_task_id = None
        session.bump_tasks_version()
        await self.session_repo.save_session(session)

        next_task = session.current_task
        batch_finished = next_task is None
        return batch_finished, next_task
