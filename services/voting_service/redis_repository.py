"""Redis adapter for session repository."""

import json
import logging
from collections.abc import Callable
from typing import Optional, TypeVar

import redis.asyncio as redis
from redis.exceptions import WatchError

from app.domain.session import Session, SessionFactory
from app.ports.session_repository import (
    SessionMutationConflictError,
    SessionRepository,
)
from services.voting_service.cms_sync import CmsSyncScheduler

logger = logging.getLogger(__name__)
MutationResult = TypeVar("MutationResult")


class RedisSessionRepository(SessionRepository):
    """Redis implementation of session repository."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self.cms_store = None
        self._cms_sync: Optional[CmsSyncScheduler] = None

    def set_cms_store(self, cms_store) -> None:
        """Attach optional CMS read-model writer."""
        self.cms_store = cms_store
        self._cms_sync = CmsSyncScheduler(cms_store) if cms_store else None

    async def _get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = await redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _make_key(self, chat_id: int, topic_id: Optional[int]) -> str:
        """Make Redis key for session."""
        topic_part = "none" if topic_id is None else str(topic_id)
        return f"session:{chat_id}:{topic_part}"

    async def get_session(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session."""
        return await self.get_session_async(chat_id, topic_id)

    async def get_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session atomically (async)."""
        return await self.ensure_session_async(chat_id, topic_id)

    async def ensure_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Ensure a session exists using Redis SET NX, then return persisted state."""
        client = await self._get_client()
        key = self._make_key(chat_id, topic_id)

        data = await client.get(key)
        if data:
            return self._deserialize_session(json.loads(data), chat_id, topic_id)

        session = Session(chat_id=chat_id, topic_id=topic_id)
        serialized = self._serialize_session(session)
        created = await client.set(key, json.dumps(serialized), nx=True)
        if created:
            self._schedule_cms_sync(session)

        data = await client.get(key)
        if data:
            return self._deserialize_session(json.loads(data), chat_id, topic_id)
        return session

    async def save_session(self, session: Session) -> None:
        """Save session."""
        await self.save_session_async(session)

    async def save_session_async(self, session: Session) -> None:
        """Save session (async)."""
        client = await self._get_client()
        key = self._make_key(session.chat_id, session.topic_id)
        data = self._serialize_session(session)
        await client.set(key, json.dumps(data))
        self._schedule_cms_sync(session)

    async def mutate_session(
        self,
        chat_id: int,
        topic_id: Optional[int],
        mutator: Callable[[Session], MutationResult],
    ) -> tuple[Session, MutationResult]:
        """Read-modify-write session using Redis optimistic locking."""
        client = await self._get_client()
        key = self._make_key(chat_id, topic_id)
        last_error: Optional[BaseException] = None

        for _ in range(10):
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    data = await pipe.get(key)
                    if data:
                        session = self._deserialize_session(json.loads(data), chat_id, topic_id)
                    else:
                        session = Session(chat_id=chat_id, topic_id=topic_id)

                    result = mutator(session)
                    serialized = self._serialize_session(session)
                    pipe.multi()
                    pipe.set(key, json.dumps(serialized))
                    await pipe.execute()
                    self._schedule_cms_sync(session)
                    return session, result
                except WatchError as exc:
                    last_error = exc
                    continue
                finally:
                    await pipe.reset()

        raise SessionMutationConflictError(
            "Session mutation conflict. Retry later."
        ) from last_error

    async def delete_session(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session."""
        await self.delete_session_async(chat_id, topic_id)

    async def delete_session_async(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session (async)."""
        client = await self._get_client()
        key = self._make_key(chat_id, topic_id)
        await client.delete(key)

    def _serialize_session(self, session: Session) -> dict:
        """Serialize session to dict."""
        return SessionFactory.to_dict(session)

    def _deserialize_session(self, data: dict, chat_id: int, topic_id: Optional[int]) -> Session:
        """Deserialize session from dict."""
        return SessionFactory.from_dict(data, chat_id, topic_id)

    def _schedule_cms_sync(self, session: Session) -> None:
        if self._cms_sync:
            self._cms_sync.schedule(session)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._cms_sync:
            await self._cms_sync.close()
        if self._client:
            await self._client.close()
