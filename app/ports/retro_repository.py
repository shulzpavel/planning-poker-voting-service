"""Retrospective repository interface.

Mirrors ``SessionRepository`` but keyed by a single ``retro_id``. The live
state is bootstrapped from the CMS configuration via ``ensure_retro`` the
first time a manager opens the cockpit; subsequent reads/mutations operate
on the persisted live state.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Optional, TypeVar

# Reuse the session conflict type so the existing FastAPI exception handler
# (main.py) translates retro mutation conflicts into HTTP 409 as well.
from app.ports.session_repository import SessionMutationConflictError  # noqa: F401
from app.domain.retro import Retrospective

MutationResult = TypeVar("MutationResult")


class RetroRepository(ABC):
    """Interface for live retrospective persistence."""

    @abstractmethod
    async def get_retro(self, retro_id: int) -> Optional[Retrospective]:
        """Return the live retro state or ``None`` when not bootstrapped yet."""

    @abstractmethod
    async def save_retro(self, retro: Retrospective) -> None:
        """Persist the full retro state."""

    @abstractmethod
    async def ensure_retro(self, retro_id: int, default: Retrospective) -> Retrospective:
        """Return existing live state or initialise it from ``default``."""

    @abstractmethod
    async def delete_retro(self, retro_id: int) -> None:
        """Remove the live retro state."""

    async def mutate_retro(
        self,
        retro_id: int,
        mutator: Callable[[Retrospective], MutationResult],
    ) -> tuple[Retrospective, MutationResult]:
        """Read, mutate, and save atomically.

        The fallback below is only safe for single-process adapters; the
        Redis adapter overrides it with optimistic locking.
        """
        retro = await self.get_retro(retro_id)
        if retro is None:
            raise KeyError(retro_id)
        result = mutator(retro)
        await self.save_retro(retro)
        return retro, result
