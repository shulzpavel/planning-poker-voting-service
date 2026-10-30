"""Owner-only intake analytics inside authenticated CMS."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from services.voting_service._http_shared import CmsPrincipal, require_permission
from services.voting_service.cms_rbac import PERM_PLANNER_VIEW
from services.voting_service.intake_dashboard_service import IntakeDashboardService
from services.voting_service.intake_settings import IntakeSettings

router = APIRouter()

def _service(request: Request, actor: CmsPrincipal):
    try:
        settings = IntakeSettings.from_env()
    except (ValueError, KeyError) as exc:
        raise HTTPException(503, "Dashboard configuration unavailable") from exc
    if not settings.permits(actor):
        raise HTTPException(404, "Dashboard not found")
    if not getattr(request.app.state, "intake_service", None):
        request.app.state.intake_service = IntakeDashboardService(request.app.state.web_redis, request.app.state.http_session, settings)
    return request.app.state.intake_service

async def _response(operation):
    try:
        value = await operation
    except Exception as exc:
        raise HTTPException(503, "Не удалось обновить данные дэшборда") from exc
    return JSONResponse(value if value is not None else {"status": "pending"}, status_code=200 if value is not None else 202,
                        headers={"Cache-Control": "private, no-store", "Retry-After": "2"})

@router.get("/cms/dashboards/intake")
async def intake_snapshot(request: Request, days: Literal["7", "30", "90"] = Query("30"), actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW))):
    return await _response(_service(request, actor).snapshot(int(days)))

@router.post("/cms/dashboards/intake/analysis")
async def intake_start_analysis(request: Request, days: Literal["7", "30", "90"] = Query("30"), actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW))):
    return await _response(_service(request, actor).analysis(int(days), start=True))

@router.get("/cms/dashboards/intake/analysis")
async def intake_analysis(request: Request, days: Literal["7", "30", "90"] = Query("30"), actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW))):
    return await _response(_service(request, actor).analysis(int(days)))
