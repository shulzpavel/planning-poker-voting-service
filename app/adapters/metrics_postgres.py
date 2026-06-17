"""Postgres adapter for MetricsRepository."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import asyncpg

from app.ports.metrics_repository import MetricsRepository

logger = logging.getLogger(__name__)


class PostgresMetricsRepository(MetricsRepository):
    """Stores operational events in Postgres for Grafana dashboards."""

    def __init__(self, dsn: str, table: str = "app_events", min_pool_size: int = 1, max_pool_size: int = 5):
        self.dsn = dsn
        self.table = table
        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()
        self._pool_kwargs = {"min_size": min_pool_size, "max_size": max_pool_size}

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(self.dsn, **self._pool_kwargs)
                    await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    event TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ok',
                    chat_id BIGINT,
                    topic_id BIGINT,
                    user_id BIGINT,
                    payload JSONB
                );
                CREATE INDEX IF NOT EXISTS idx_{self.table}_ts ON {self.table}(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_{self.table}_event ON {self.table}(event);
                CREATE INDEX IF NOT EXISTS idx_{self.table}_status ON {self.table}(status);
                """
            )

    async def record_event(
        self,
        event: str,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        user_id: Optional[int] = None,
        status: str = "ok",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record event. Swallows errors so the app works even when Postgres is unreachable."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self.table} (event, status, chat_id, topic_id, user_id, payload)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    event,
                    status,
                    chat_id,
                    topic_id,
                    user_id,
                    json.dumps(payload or {}),
                )
        except Exception as exc:
            logger.debug("Metrics record_event failed (Postgres unavailable): %s", exc)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
