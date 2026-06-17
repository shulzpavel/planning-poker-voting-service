"""Session repository interface."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Optional, TypeVar

from app.domain.session import Session

MutationResult = TypeVar("MutationResult")


class SessionMutationConflictError(RuntimeError):
    """Raised when a session mutation cannot be applied atomically.

    Transport adapters should translate this into HTTP 409 so clients can
    retry against a fresh state instead of seeing an opaque 500.
    """


class SessionRepository(ABC):
    """Interface for session persistence."""

    @abstractmethod
    async def get_session(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session."""
        pass

    @abstractmethod
    async def save_session(self, session: Session) -> None:
        """Save session state."""
        pass

    @abstractmethod
    async def delete_session(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session."""
        pass

    async def mutate_session(
        self,
        chat_id: int,
        topic_id: Optional[int],
        mutator: Callable[[Session], MutationResult],
    ) -> tuple[Session, MutationResult]:
        """Read, mutate, and save a session.

        Concrete repositories should override this with an atomic implementation.
        The fallback is only safe for single-process adapters.
        """
        session = await self.get_session(chat_id, topic_id)
        result = mutator(session)
        await self.save_session(session)
        return session, result
