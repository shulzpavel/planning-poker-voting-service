"""Lightweight readiness probes against lifespan-managed app.state singletons."""

from __future__ import annotations

from typing import Any


async def ping_redis(redis_client: Any) -> None:
    """Verify a shared Redis client responds to PING."""
    pong = await redis_client.ping()
    if pong not in (True, "PONG", b"PONG"):
        raise RuntimeError("Redis ping failed")


async def ping_postgres_pool(pool: Any) -> None:
    """Verify a shared asyncpg pool can serve SELECT 1."""
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT 1")
    if value != 1:
        raise RuntimeError("Postgres probe failed")


async def ping_repository(repository: Any) -> None:
    """Probe the session repository without creating a new adapter."""
    if repository is None:
        raise RuntimeError("repository not initialized")

    get_client = getattr(repository, "_get_client", None)
    if callable(get_client):
        client = await get_client()
        await ping_redis(client)
        return

    pool = getattr(repository, "pool", None)
    if pool is not None:
        await ping_postgres_pool(pool)
        return

    # FileSessionRepository and other local adapters need no network probe.


async def ping_cms_store(cms_store: Any) -> None:
    """Probe the CMS Postgres read model when configured."""
    if cms_store is None:
        return
    pool = getattr(cms_store, "pool", None)
    if pool is None:
        raise RuntimeError("cms_store pool not initialized")
    await ping_postgres_pool(pool)


async def check_voting_readiness(
    *,
    repository: Any,
    web_redis: Any,
    cms_store: Any,
) -> None:
    """Run all voting-service readiness probes in dependency order."""
    if web_redis is None:
        raise RuntimeError("web_redis not initialized")
    await ping_repository(repository)
    await ping_redis(web_redis)
    await ping_cms_store(cms_store)
