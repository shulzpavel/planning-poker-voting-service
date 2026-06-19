"""Health check endpoints for Voting Service."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from services.voting_service.health_checks import check_voting_readiness

router = APIRouter()
health_router = router  # backward compatibility


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get("/", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic liveness."""
    return HealthResponse(status="healthy", service="voting-service", version="1.0.0")


@router.get("/ready")
async def readiness_check(request: Request) -> dict:
    """Readiness: ping lifespan-managed Redis/Postgres clients (no new pools)."""
    try:
        await check_voting_readiness(
            repository=getattr(request.app.state, "repository", None),
            web_redis=getattr(request.app.state, "web_redis", None),
            cms_store=getattr(request.app.state, "cms_store", None),
        )
        return {"status": "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "not_ready", "error": str(exc)}


@router.get("/live")
async def liveness_check() -> dict:
    """Liveness endpoint."""
    return {"status": "alive"}
