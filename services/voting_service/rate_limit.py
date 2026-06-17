"""Redis-backed rate limiting helpers for the voting service.

Designed to:

* support the basic anti-bruteforce / anti-spam patterns we need
  (login by username + IP, web join/vote per participant or IP, web
  token minting per IP, AI summary per CMS actor) without pulling in a
  new dependency — we already run Redis;
* expose a single ``enforce_rate_limit`` helper that handlers / FastAPI
  dependencies call. The helper raises a :class:`fastapi.HTTPException`
  with status 429 on threshold breach, so route handlers stay
  declarative;
* be atomic on the Redis side. ``INCR`` followed by ``EXPIRE`` from the
  client has a benign-but-real race where two concurrent INCR-from-zero
  callers could both see ``current == 1`` and both issue ``EXPIRE``;
  using a server-side script collapses both steps into one round-trip
  and avoids the rare "TTL never set, key lives forever" outcome that
  the same naive pattern in ``_ensure_login_not_limited`` is currently
  exposed to;
* preserve fail-open semantics on transient Redis errors. Rate limiting
  is a defence-in-depth control — we must not start returning 500s on
  every request because Redis blipped. Errors are logged and the call
  is allowed through.
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


# Lua script: atomically increment the counter and ensure a TTL is
# attached on first hit. Returns the new counter value.
#
# We don't use ``EXPIRE key ttl XX`` because we want to set TTL on the
# *first* hit (when the counter is created); after that, ``EXPIRE`` is a
# no-op because TTL is already in place.
_INCR_AND_EXPIRE = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""


class RateLimitExceeded(Exception):
    """Internal signal that a quota was breached. The HTTP layer surfaces
    this as :class:`fastapi.HTTPException` 429; tests can match against
    this richer type."""

    def __init__(self, key: str, current: int, limit: int):
        super().__init__(f"Rate limit exceeded for {key}: {current}/{limit}")
        self.key = key
        self.current = current
        self.limit = limit


async def hit_rate_limit(
    redis_client: aioredis.Redis,
    key: str,
    window_seconds: int,
) -> Optional[int]:
    """Increment the counter at ``key`` and ensure its TTL is set.

    Returns the new counter value, or ``None`` if Redis was briefly
    unavailable. Callers should treat ``None`` as "fail-open, allow".
    """
    try:
        result = await redis_client.eval(_INCR_AND_EXPIRE, 1, key, str(window_seconds))
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate_limit: Redis EVAL failed key=%s err=%r", key, exc)
        return None
    try:
        return int(result)
    except (TypeError, ValueError):
        logger.warning("rate_limit: unexpected EVAL result key=%s value=%r", key, result)
        return None


async def enforce_rate_limit(
    redis_client: aioredis.Redis,
    *,
    key: str,
    limit: int,
    window_seconds: int,
    error_detail: str = "Too many requests",
) -> int:
    """Increment + check. Raises ``HTTPException(429)`` when exceeded.

    On Redis transient failure the call is allowed through (returns 0).
    Rate limiting is defence-in-depth and must not be the cause of a
    production outage.
    """
    current = await hit_rate_limit(redis_client, key, window_seconds)
    if current is None:
        return 0
    if current > limit:
        raise HTTPException(status_code=429, detail=error_detail)
    return current


def client_ip(request: Request) -> str:
    """Best-effort client IP from proxy headers.

    Mirrors ``_http_shared._client_ip`` and lives here as a tiny
    convenience so the rate-limit call sites don't have to import both.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"
