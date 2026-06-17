"""Health check endpoints for Voting Service."""

from fastapi import APIRouter
from pydantic import BaseModel

from services.voting_service.repository import get_repository

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
async def readiness_check() -> dict:
    """Readiness: инициализация и закрытие репозитория."""
    repo = None
    try:
        repo = await get_repository()
        # Простая операция: сохранить/прочитать ничего не требуется, важно создать и закрыть
        return {"status": "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "not_ready", "error": str(exc)}
    finally:
        if repo and hasattr(repo, "close"):
            try:
                await repo.close()
            except Exception:
                pass


@router.get("/live")
async def liveness_check() -> dict:
    """Liveness endpoint."""
    return {"status": "alive"}
