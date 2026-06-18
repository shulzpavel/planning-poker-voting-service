"""WebSocket connection manager for web voting UI."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections grouped by session token."""

    def __init__(self) -> None:
        self._connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, token: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(token, []).append(websocket)
        logger.debug("WS connected token=%s total=%d", token, len(self._connections[token]))

    def disconnect(self, token: str, websocket: WebSocket) -> None:
        conns = self._connections.get(token, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self._connections.pop(token, None)
        logger.debug("WS disconnected token=%s", token)

    async def broadcast(self, token: str, message: dict) -> None:
        conns = self._connections.get(token, [])
        if not conns:
            return
        payload = json.dumps(message)
        dead: List[WebSocket] = []
        for ws in list(conns):
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("WS broadcast failed token=%s: %s", token, exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(token, ws)


manager = ConnectionManager()


async def redis_pubsub_listener(redis_source: Any, token: str, channel: str, websocket: WebSocket) -> None:
    """Subscribe to a Redis pub/sub channel and forward messages to a WebSocket.

    Prefer the app's already configured Redis client/pool. Passing a URL is
    kept for compatibility with older callers and tests. When a shared client
    is passed, a dedicated connection is checked out from the same pool so
    pub/sub mode does not block regular GET/SET/PUBLISH on ``web_redis``.
    """
    import redis.asyncio as redis

    client: Optional[redis.Redis] = None
    pubsub = None
    owns_client = False
    try:
        if isinstance(redis_source, str):
            client = await redis.from_url(
                redis_source,
                decode_responses=True,
                socket_timeout=None,
                health_check_interval=30,
            )
            owns_client = True
        else:
            client = redis.Redis(connection_pool=redis_source.connection_pool)
            owns_client = True
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
            except redis.TimeoutError:
                logger.debug("Redis pubsub timeout token=%s channel=%s; continuing", token, channel)
                continue
            if message is None:
                continue
            if message["type"] == "message":
                try:
                    await websocket.send_text(message["data"])
                except Exception as exc:
                    logger.debug("WS pubsub forwarding failed token=%s: %s", token, exc)
                    break
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("pubsub listener error token=%s: %s", token, exc)
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception as exc:
                logger.debug("Redis pubsub close failed token=%s channel=%s: %s", token, channel, exc)
        if client and owns_client:
            try:
                await client.aclose()
            except Exception as exc:
                logger.debug("Redis client close failed token=%s: %s", token, exc)
