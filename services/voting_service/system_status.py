"""Deploy maintenance flag stored in Redis and exposed to the web UI."""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field

MAINTENANCE_REDIS_KEY = "system:maintenance"
MAINTENANCE_TTL_SECONDS = 30 * 60


class MaintenanceStatus(BaseModel):
    active: bool = False
    service: Optional[str] = None


class SystemStatusResponse(BaseModel):
    maintenance: MaintenanceStatus = Field(default_factory=MaintenanceStatus)


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def read_maintenance_status(redis_client: Any) -> MaintenanceStatus:
    """Read the deploy maintenance flag from Redis."""
    if redis_client is None:
        return MaintenanceStatus()

    raw = await redis_client.get(MAINTENANCE_REDIS_KEY)
    if not raw:
        return MaintenanceStatus()

    payload = _coerce_payload(raw)
    if not payload.get("active"):
        return MaintenanceStatus()

    service = payload.get("service")
    return MaintenanceStatus(
        active=True,
        service=str(service) if service else None,
    )
