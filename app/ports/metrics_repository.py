"""Metrics repository interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class MetricsRepository(ABC):
    """Interface for recording operational metrics and events."""

    @abstractmethod
    async def record_event(
        self,
        event: str,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        user_id: Optional[int] = None,
        status: str = "ok",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a single event with optional context."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Cleanup resources (connections/pools)."""
        raise NotImplementedError
