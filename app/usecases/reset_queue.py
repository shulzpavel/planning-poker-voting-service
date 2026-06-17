"""Use case for resetting tasks queue."""

from typing import Optional

from app.domain.session import Session
from app.ports.session_repository import SessionRepository


class ResetQueueUseCase:
    """Use case for resetting tasks queue."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(self, chat_id: int, topic_id: Optional[int]) -> int:
        """Reset tasks queue and voting state. Returns number of tasks removed."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        task_count = len(session.tasks_queue)
        
        # Clear votes for current task
        if session.current_task:
            session.current_task.votes.clear()
        
        # Clear queue
        session.tasks_queue.clear()
        session.current_task_index = 0
        
        # Reset voting state
        session.batch_completed = False
        session.current_batch_started_at = None
        session.current_batch_id = None
        session.active_vote_message_id = None
        session.revealed_task_id = None
        session.bump_tasks_version()
        
        # Note: last_batch and history are preserved
        
        await self.session_repo.save_session(session)
        return task_count
