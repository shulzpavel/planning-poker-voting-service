"""Redis adapter for the retrospective repository.

Clones the optimistic-locking strategy of ``RedisSessionRepository``:
live state lives under ``retro:{retro_id}`` and every read-modify-write
goes through a ``WATCH/MULTI/EXEC`` transaction, retrying on contention
before surfacing a retriable ``SessionMutationConflictError`` (HTTP 409).
"""

import json
import logging
from collections.abc import Callable
from typing import Optional, TypeVar

import redis.asyncio as redis
from redis.exceptions import WatchError

from app.domain.retro import Retrospective, RetrospectiveFactory
from app.ports.retro_repository import RetroRepository, SessionMutationConflictError

logger = logging.getLogger(__name__)
MutationResult = TypeVar("MutationResult")


class RedisRetroRepository(RetroRepository):
    """Redis implementation of the retrospective repository."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[redis.Redis] = None

    async def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = await redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _make_key(self, retro_id: int) -> str:
        return f"retro:{retro_id}"

    async def get_retro(self, retro_id: int) -> Optional[Retrospective]:
        client = await self._get_client()
        data = await client.get(self._make_key(retro_id))
        if not data:
            return None
        return RetrospectiveFactory.from_dict(json.loads(data), retro_id)

    async def save_retro(self, retro: Retrospective) -> None:
        client = await self._get_client()
        await client.set(self._make_key(retro.retro_id), json.dumps(RetrospectiveFactory.to_dict(retro)))

    async def ensure_retro(self, retro_id: int, default: Retrospective) -> Retrospective:
        client = await self._get_client()
        key = self._make_key(retro_id)
        await client.set(key, json.dumps(RetrospectiveFactory.to_dict(default)), nx=True)
        data = await client.get(key)
        if data:
            return RetrospectiveFactory.from_dict(json.loads(data), retro_id)
        return default

    async def delete_retro(self, retro_id: int) -> None:
        client = await self._get_client()
        await client.delete(self._make_key(retro_id))

    async def mutate_retro(
        self,
        retro_id: int,
        mutator: Callable[[Retrospective], MutationResult],
    ) -> tuple[Retrospective, MutationResult]:
        client = await self._get_client()
        key = self._make_key(retro_id)
        last_error: Optional[BaseException] = None

        for _ in range(10):
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    data = await pipe.get(key)
                    if not data:
                        raise KeyError(retro_id)
                    retro = RetrospectiveFactory.from_dict(json.loads(data), retro_id)
                    result = mutator(retro)
                    pipe.multi()
                    pipe.set(key, json.dumps(RetrospectiveFactory.to_dict(retro)))
                    await pipe.execute()
                    return retro, result
                except WatchError as exc:
                    last_error = exc
                    continue
                finally:
                    await pipe.reset()

        raise SessionMutationConflictError("Retro mutation conflict. Retry later.") from last_error

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
