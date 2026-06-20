#!/usr/bin/env python3
"""Voting Service - FastAPI microservice for session and voting management."""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

from app.ports.session_repository import SessionMutationConflictError
from services.voting_service.app_api import app_router
from services.voting_service.health import health_router
from services.voting_service.metrics import metrics_router
from services.voting_service.cms_api import cms_router
from services.voting_service.retro_api import retro_router
from services.voting_service.system_api import system_router
from services.voting_service.web_api import web_router, REDIS_URL

logger = logging.getLogger(__name__)

ALLOWED_CORS_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_CORS_HEADERS = ["Authorization", "Content-Type", "X-Requested-With"]


def _maybe_close(obj):
    """Return an awaitable that calls obj.close() if available, else None."""
    if obj is None or not hasattr(obj, "close"):
        return None
    return obj.close()


def _maybe_aclose(obj):
    """Return an awaitable for obj.aclose() / obj.close(), preferring aclose."""
    if obj is None:
        return None
    if hasattr(obj, "aclose"):
        return obj.aclose()
    if hasattr(obj, "close"):
        return obj.close()
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    import asyncio
    import aiohttp
    import redis.asyncio as aioredis

    print("🚀 Voting Service starting...")
    from services.voting_service.repository import get_repository
    repo = await get_repository()
    app.state.repository = repo

    cms_store = None
    postgres_dsn = os.getenv("POSTGRES_DSN")
    if postgres_dsn:
        try:
            from services.voting_service.cms_store import PostgresCmsStore
            cms_store = await PostgresCmsStore.create(postgres_dsn)
            await cms_store.ensure_access_defaults(
                os.getenv("CMS_USERNAME", "admin"),
                os.getenv("CMS_PASSWORD", ""),
            )
            if hasattr(repo, "set_cms_store"):
                repo.set_cms_store(cms_store)
        except Exception as exc:
            print(f"[CMS] Postgres read model unavailable: {exc!r}")
    app.state.cms_store = cms_store

    web_redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    app.state.web_redis = web_redis

    # Live retrospective store — shares the same Redis instance/URL as the
    # voting live state and pub/sub fan-out.
    from services.voting_service.retro_redis_repository import RedisRetroRepository
    app.state.retro_repository = RedisRetroRepository(REDIS_URL)

    # Long-lived HTTP session for outbound calls to the jira-service container
    # and to the Anthropic API. Re-used across requests so the connection pool
    # and TLS handshakes survive — without it the per-request
    # ``aiohttp.ClientSession()`` defeated the in-memory Jira cache by
    # tearing the pool down on every call.
    app.state.http_session = aiohttp.ClientSession()

    app.state.cms_backfill_task = None
    if cms_store:
        from services.voting_service.cms_store import backfill_cms_from_redis
        app.state.cms_backfill_task = asyncio.create_task(
            backfill_cms_from_redis(web_redis, cms_store)
        )

    yield

    print("🛑 Voting Service shutting down...")
    if getattr(app.state, "cms_backfill_task", None):
        app.state.cms_backfill_task.cancel()
        try:
            await app.state.cms_backfill_task
        except asyncio.CancelledError:
            pass
    # Each shutdown step is guarded individually so a broken adapter cannot
    # prevent the rest from cleaning up (and so adapters without close() like
    # FileSessionRepository do not crash the lifespan).
    for label, closer in (
        ("repository", _maybe_close(getattr(app.state, "repository", None))),
        ("cms_store", _maybe_close(getattr(app.state, "cms_store", None))),
        ("retro_repository", _maybe_close(getattr(app.state, "retro_repository", None))),
        ("web_redis", _maybe_aclose(getattr(app.state, "web_redis", None))),
        ("http_session", _maybe_close(getattr(app.state, "http_session", None))),
    ):
        if closer is None:
            continue
        try:
            await closer
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shutdown step %s failed: %r", label, exc)


app = FastAPI(
    title="Voting Service",
    description="Microservice for Planning Poker session and voting management",
    version="1.0.0",
    lifespan=lifespan,
)


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS") or os.getenv("WEB_UI_URL", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if origins:
        return origins
    return [
        "http://localhost:3001",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=ALLOWED_CORS_METHODS,
    allow_headers=ALLOWED_CORS_HEADERS,
)


@app.exception_handler(SessionMutationConflictError)
async def _on_session_mutation_conflict(
    request: Request, exc: SessionMutationConflictError
) -> JSONResponse:
    """Convert atomic-mutation conflicts into a retriable 409 instead of 500."""
    logger.warning("Session mutation conflict on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=409,
        content={"detail": "Session is busy, please retry."},
    )

# Include routers
app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(metrics_router, prefix="/metrics", tags=["metrics"])
app.include_router(system_router, prefix="/api/v1", tags=["system"])
app.include_router(app_router, prefix="/api/v1", tags=["app"])
app.include_router(web_router, prefix="/api/v1", tags=["web"])
app.include_router(cms_router, prefix="/api/v1", tags=["cms"])
app.include_router(retro_router, prefix="/api/v1", tags=["retro"])


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VOTING_SERVICE_PORT", "8002"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
