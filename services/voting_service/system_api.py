"""Public system status endpoints (deploy maintenance banner, etc.)."""

from fastapi import APIRouter, Request

from services.voting_service.system_status import (
    SystemStatusResponse,
    read_maintenance_status,
)

system_router = APIRouter()


@system_router.get("/system/status", response_model=SystemStatusResponse)
async def system_status(request: Request) -> SystemStatusResponse:
    """Public status for the web shell (no auth)."""
    redis_client = getattr(request.app.state, "web_redis", None)
    maintenance = await read_maintenance_status(redis_client)
    return SystemStatusResponse(maintenance=maintenance)
