"""Use case for casting a vote."""

from typing import Optional

from app.domain.session import Session
from app.ports.session_repository import SessionRepository


class CastVoteUseCase:
    """Use case for casting a vote on current task."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        user_id: int,
        vote_value: str,
    ) -> bool:
        """Cast vote for current task."""
        if hasattr(self.session_repo, "cast_vote_atomic"):
            return await self.session_repo.cast_vote_atomic(chat_id, topic_id, user_id, vote_value)  # type: ignore[attr-defined]

        def mutate(session: Session) -> bool:
            if not session.current_task:
                return False
            if not session.is_voting_active:
                return False
            if not session.can_vote(user_id):
                return False
            session.current_task.votes[user_id] = vote_value
            return True

        _, success = await self.session_repo.mutate_session(chat_id, topic_id, mutate)
        return success

    async def all_voters_voted(self, chat_id: int, topic_id: Optional[int]) -> bool:
        """Check if all eligible voters have voted."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        if not session.current_task:
            return False
        
        eligible_voters = [uid for uid in session.participants if session.can_vote(uid)]
        if not eligible_voters:
            return False
        
        current_votes = session.current_task.votes
        return len(current_votes) >= len(eligible_voters)
