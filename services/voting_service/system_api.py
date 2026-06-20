"""Public system status endpoints (deploy maintenance banner, etc.)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.voting_service.system_status import (
    SystemStatusResponse,
    read_maintenance_status,
)

system_router = APIRouter()

_SYSTEM_STATUS_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


@system_router.get("/system/status", response_model=SystemStatusResponse)
async def system_status(request: Request) -> JSONResponse:
    """Public status for the web shell (no auth)."""
    redis_client = getattr(request.app.state, "web_redis", None)
    maintenance = await read_maintenance_status(redis_client)
    payload = SystemStatusResponse(maintenance=maintenance)
    return JSONResponse(
        content=payload.model_dump(),
        headers=_SYSTEM_STATUS_HEADERS,
    )
