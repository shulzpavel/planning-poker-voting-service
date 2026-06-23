"""CMS overview and team management endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from services.voting_service.cms_rbac import PERM_OVERVIEW_VIEW
from services.voting_service.cms_team_access import require_superuser, team_scope
from services.voting_service.cms_store.teams import TeamDeleteBlockedError
from services.voting_service._http_shared import (
    AuthDep,
    CmsPrincipal,
    _audit,
    _get_cms_store,
    require_permission,
)

router = APIRouter()


class TeamCreateRequest(BaseModel):
    slug: Optional[str] = Field(default=None, min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class TeamUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None

@router.get("/cms/overview")
async def cms_overview(
    request: Request,
    team_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_OVERVIEW_VIEW)),
) -> dict:
    scope = team_scope(actor)
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _get_cms_store(request).overview(team_id=team_id, **scope)


@router.get("/cms/teams")
async def cms_list_teams(
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    items = await _get_cms_store(request).list_teams(**team_scope(actor))
    return {"items": items}


@router.post("/cms/teams")
async def cms_create_team(
    body: TeamCreateRequest,
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    require_superuser(actor)
    try:
        team = await _get_cms_store(request).create_team(
            slug=body.slug or body.name,
            name=body.name,
            description=body.description,
        )
    except Exception as exc:
        await _audit(request, "cms.team.create", actor.username, "failed", {"error": str(exc)})
        raise HTTPException(status_code=400, detail="Team could not be created") from exc
    await _audit(request, "cms.team.create", actor.username, "ok", {"team_id": team["id"]})
    return team


@router.patch("/cms/teams/{team_id}")
async def cms_update_team(
    team_id: int,
    body: TeamUpdateRequest,
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    require_superuser(actor)
    team = await _get_cms_store(request).update_team(
        team_id,
        name=body.name,
        description=body.description,
        is_active=body.is_active,
    )
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    await _audit(request, "cms.team.update", actor.username, "ok", {"team_id": team_id})
    return team


@router.delete("/cms/teams/{team_id}")
async def cms_delete_team(
    team_id: int,
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    require_superuser(actor)
    store = _get_cms_store(request)
    try:
        result = await store.delete_team(team_id)
    except TeamDeleteBlockedError as exc:
        await _audit(
            request,
            "cms.team.delete",
            actor.username,
            "failed",
            {"team_id": team_id, "reason": exc.reason, "count": exc.count},
        )
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except Exception as exc:
        await _audit(
            request,
            "cms.team.delete",
            actor.username,
            "failed",
            {"team_id": team_id, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail="Team could not be deleted") from exc
    if not result:
        raise HTTPException(status_code=404, detail="Team not found")
    await _audit(
        request,
        "cms.team.delete",
        actor.username,
        "ok",
        {"team_id": team_id, "detached": result.get("detached")},
    )
    return result

