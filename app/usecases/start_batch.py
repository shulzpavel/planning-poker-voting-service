"""Use case for starting voting batch."""

from datetime import datetime
from typing import Optional

from app.domain.estimation import clear_task_votes
from app.domain.session import Session
from app.ports.session_repository import SessionRepository


class StartBatchUseCase:
    """Use case for starting voting session."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(self, chat_id: int, topic_id: Optional[int]) -> bool:
        """Start voting session for tasks."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        if not session.tasks_queue:
            return False
        
        session.current_task_index = 0
        session.batch_completed = False
        session.current_batch_started_at = datetime.utcnow().isoformat()
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
        session.bump_tasks_version()
        
        await self.session_repo.save_session(session)
        return True
