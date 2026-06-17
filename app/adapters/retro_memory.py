"""In-memory retrospective repository.

Used by tests and as a single-process fallback. State is serialized through
``RetrospectiveFactory`` on every read/write so it exercises the same JSON
round-trip the Redis adapter relies on.
"""

from collections.abc import Callable
from typing import Dict, Optional, TypeVar

from app.domain.retro import Retrospective, RetrospectiveFactory
from app.ports.retro_repository import RetroRepository

MutationResult = TypeVar("MutationResult")


class MemoryRetroRepository(RetroRepository):
    """Dictionary-backed retro repository."""

    def __init__(self) -> None:
        self._store: Dict[int, dict] = {}

    async def get_retro(self, retro_id: int) -> Optional[Retrospective]:
        data = self._store.get(retro_id)
        if data is None:
            return None
        return RetrospectiveFactory.from_dict(data, retro_id)

    async def save_retro(self, retro: Retrospective) -> None:
        self._store[retro.retro_id] = RetrospectiveFactory.to_dict(retro)

    async def ensure_retro(self, retro_id: int, default: Retrospective) -> Retrospective:
        if retro_id not in self._store:
            self._store[retro_id] = RetrospectiveFactory.to_dict(default)
        return await self.get_retro(retro_id)  # type: ignore[return-value]

    async def delete_retro(self, retro_id: int) -> None:
        self._store.pop(retro_id, None)

    async def mutate_retro(
        self,
        retro_id: int,
        mutator: Callable[[Retrospective], MutationResult],
    ) -> tuple[Retrospective, MutationResult]:
        retro = await self.get_retro(retro_id)
        if retro is None:
            raise KeyError(retro_id)
        result = mutator(retro)
        await self.save_retro(retro)
        return retro, result
