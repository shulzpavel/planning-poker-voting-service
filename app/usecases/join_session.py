"""Use case for joining a session."""

from typing import Optional

from app.domain.participant import Participant
from app.domain.session import Session
from app.ports.session_repository import SessionRepository
from config import UserRole


class JoinSessionUseCase:
    """Use case for joining a planning poker session."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        user_id: int,
        user_name: str,
        role: UserRole,
    ) -> Session:
        """Join user to session with given role."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        session.participants[user_id] = Participant(
            user_id=user_id,
            name=user_name,
            role=role,
        )
        
        # Drop votes if admin
        if role == UserRole.ADMIN and session.current_task:
            session.current_task.votes.pop(user_id, None)
        
        await self.session_repo.save_session(session)
        return session
