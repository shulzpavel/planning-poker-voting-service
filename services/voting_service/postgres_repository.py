"""Postgres adapter for session repository."""

import json
import logging
from collections.abc import Callable
from typing import Optional, TypeVar

import asyncpg

from app.domain.session import Session, SessionFactory
from app.ports.session_repository import SessionRepository
from services.voting_service.cms_sync import CmsSyncScheduler

logger = logging.getLogger(__name__)
MutationResult = TypeVar("MutationResult")


class PostgresSessionRepository(SessionRepository):
    """Postgres implementation of session repository."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.cms_store = None
        self._cms_sync: Optional[CmsSyncScheduler] = None

    def set_cms_store(self, cms_store) -> None:
        """Attach optional CMS read-model writer."""
        self.cms_store = cms_store
        self._cms_sync = CmsSyncScheduler(cms_store) if cms_store else None

    @classmethod
    async def create(cls, dsn: str) -> "PostgresSessionRepository":
        """Create repository with connection pool."""
        pool = await asyncpg.create_pool(dsn)
        repo = cls(pool)
        await repo._ensure_schema()
        return repo

    async def _ensure_schema(self) -> None:
        """Ensure database schema exists."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id BIGINT NOT NULL,
                    topic_id BIGINT,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_pkey;
                ALTER TABLE sessions ALTER COLUMN topic_id DROP NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_identity
                    ON sessions (chat_id, COALESCE(topic_id, -1));
                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC);
            """)

    def _make_key(self, chat_id: int, topic_id: Optional[int]) -> tuple:
        """Make key for session."""
        return (chat_id, topic_id)

    def _identity_key(self, chat_id: int, topic_id: Optional[int]) -> str:
        topic_part = "none" if topic_id is None else str(topic_id)
        return f"{chat_id}:{topic_part}"

    async def get_session(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session."""
        return await self.get_session_async(chat_id, topic_id)

    async def get_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Get or create session atomically (async)."""
        return await self.ensure_session_async(chat_id, topic_id)

    async def ensure_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
        """Ensure a session exists under a transaction-scoped advisory lock."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", self._identity_key(chat_id, topic_id))
                row = await conn.fetchrow(
                    "SELECT data FROM sessions WHERE chat_id = $1 AND topic_id IS NOT DISTINCT FROM $2",
                    chat_id,
                    topic_id,
                )
                if row:
                    data = row["data"]
                    return self._deserialize_session(data, chat_id, topic_id)

                session = Session(chat_id=chat_id, topic_id=topic_id)
                data = self._serialize_session(session)
                await conn.execute(
                    """
                    INSERT INTO sessions (chat_id, topic_id, data, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    """,
                    session.chat_id,
                    session.topic_id,
                    json.dumps(data),
                )
        self._schedule_cms_sync(session)
        return session

    async def save_session(self, session: Session) -> None:
        """Save session."""
        await self.save_session_async(session)

    async def save_session_async(self, session: Session) -> None:
        """Save session (async)."""
        data = self._serialize_session(session)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    self._identity_key(session.chat_id, session.topic_id),
                )
                result = await conn.execute(
                    """
                    UPDATE sessions
                    SET data = $3, updated_at = NOW()
                    WHERE chat_id = $1 AND topic_id IS NOT DISTINCT FROM $2
                    """,
                    session.chat_id,
                    session.topic_id,
                    json.dumps(data),
                )
                if result == "UPDATE 0":
                    await conn.execute(
                        """
                        INSERT INTO sessions (chat_id, topic_id, data, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        """,
                        session.chat_id,
                        session.topic_id,
                        json.dumps(data),
                    )
        self._schedule_cms_sync(session)

    async def mutate_session(
        self,
        chat_id: int,
        topic_id: Optional[int],
        mutator: Callable[[Session], MutationResult],
    ) -> tuple[Session, MutationResult]:
        """Read-modify-write session under an advisory transaction lock."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    self._identity_key(chat_id, topic_id),
                )
                row = await conn.fetchrow(
                    "SELECT data FROM sessions WHERE chat_id = $1 AND topic_id IS NOT DISTINCT FROM $2",
                    chat_id,
                    topic_id,
                )
                if row:
                    session = self._deserialize_session(row["data"], chat_id, topic_id)
                else:
                    session = Session(chat_id=chat_id, topic_id=topic_id)

                result = mutator(session)
                data = self._serialize_session(session)
                updated = await conn.execute(
                    """
                    UPDATE sessions
                    SET data = $3, updated_at = NOW()
                    WHERE chat_id = $1 AND topic_id IS NOT DISTINCT FROM $2
                    """,
                    session.chat_id,
                    session.topic_id,
                    json.dumps(data),
                )
                if updated == "UPDATE 0":
                    await conn.execute(
                        """
                        INSERT INTO sessions (chat_id, topic_id, data, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        """,
                        session.chat_id,
                        session.topic_id,
                        json.dumps(data),
                    )

        self._schedule_cms_sync(session)
        return session, result

    async def delete_session(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session."""
        await self.delete_session_async(chat_id, topic_id)

    async def delete_session_async(self, chat_id: int, topic_id: Optional[int]) -> None:
        """Delete session (async)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE chat_id = $1 AND topic_id IS NOT DISTINCT FROM $2",
                chat_id,
                topic_id,
            )

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
        """Close connection pool."""
        if self._cms_sync:
            await self._cms_sync.close()
        await self.pool.close()
