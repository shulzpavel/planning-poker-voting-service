"""Use case for leaving a session."""

from typing import Optional

from app.domain.session import Session
from app.ports.session_repository import SessionRepository


class LeaveSessionUseCase:
    """Use case for leaving a planning poker session."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(self, chat_id: int, topic_id: Optional[int], user_id: int) -> bool:
        """Remove user from session."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        if user_id not in session.participants:
            return False
        
        session.participants.pop(user_id, None)
        if session.current_task:
            session.current_task.votes.pop(user_id, None)
        
        await self.session_repo.save_session(session)
        return True
