"""No-op metrics adapter (used when Postgres DSN is not configured)."""

from typing import Any, Dict, Optional

from app.ports.metrics_repository import MetricsRepository


class NullMetricsRepository(MetricsRepository):
    """Does nothing; keeps code paths simple when metrics are disabled."""

    async def record_event(
        self,
        event: str,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        user_id: Optional[int] = None,
        status: str = "ok",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None
