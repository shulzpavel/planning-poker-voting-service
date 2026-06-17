"""Use case for finishing voting batch."""

from datetime import datetime
from typing import List, Optional

from app.domain.session import Session
from app.domain.task import Task
from app.ports.session_repository import SessionRepository


class FinishBatchUseCase:
    """Use case for finishing current voting batch."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(self, chat_id: int, topic_id: Optional[int]) -> List[Task]:
        """Finish current batch and move tasks to history."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        # Protection against double call
        if session.batch_completed:
            return []
        
        completed_tasks = []
        finished_at = datetime.utcnow().isoformat()

        for task in session.tasks_queue:
            task.completed_at = finished_at
            completed_tasks.append(task)

        session.last_batch.clear()
        session.last_batch = completed_tasks.copy()
        session.history.extend(completed_tasks)
        session.tasks_queue.clear()
        session.current_task_index = 0
        session.batch_completed = True
        session.active_vote_message_id = None
        session.current_batch_started_at = None
        session.revealed_task_id = None
        session.bump_tasks_version()

        await self.session_repo.save_session(session)
        return completed_tasks
