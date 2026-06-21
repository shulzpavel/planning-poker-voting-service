"""CMS sprint planner endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.voting_service.cms_rbac import PERM_PLANNER_VIEW
from services.voting_service.cms_team_access import assert_record_access, resolve_create_team_id, team_scope
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    require_permission,
)

router = APIRouter()


class SprintPlanTrack(BaseModel):
    """One configurable planning track (e.g. back, front, qa, design).

    The frontend planner is tag-driven: each role is pinned to a track and
    velocity / capacity / plan limit are computed per track independently.
    Backend just stores the user-declared tracks verbatim — no business
    logic relies on the slug values.
    """

    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=80)


class SprintPlanRoleInput(BaseModel):
    """One role line inside the detailed capacity input."""

    name: str = Field(min_length=1, max_length=80)
    headcount: float = Field(ge=0, le=999)
    absences: float = Field(default=0, ge=0, le=99999)
    # Tag-driven planner: which track this role belongs to. Optional for
    # back-compat with payloads saved before the tag split.
    track_id: Optional[str] = Field(default=None, max_length=40)


class SprintPlanHistoryEntry(BaseModel):
    """One closed sprint inside the velocity history.

    The tag-driven planner stores closed SP per track in ``by_track`` (a
    map of track slug → SP). Earlier shapes are preserved so legacy plans
    keep loading:

    * ``story_points``                     — pre-split single SP per sprint
    * ``story_points_dev`` / ``..._test``  — dev/test split phase
    """

    label: str = Field(default="", max_length=120)
    story_points: Optional[float] = Field(default=None, ge=0, le=99999)
    story_points_dev: Optional[float] = Field(default=None, ge=0, le=99999)
    story_points_test: Optional[float] = Field(default=None, ge=0, le=99999)
    # New canonical field for the tag-driven planner.
    by_track: Optional[dict[str, float]] = Field(default=None)


class SprintPlanPayload(BaseModel):
    """User-editable inputs for the sprint planner.

    The result is recomputed on the frontend on every change for live preview
    and stored alongside the inputs so list views can show a one-line summary
    without recomputing.

    ``tracks`` is optional so payloads saved before the tag-driven planner
    keep deserialising; the frontend re-creates default tracks for those.
    """

    working_days: float = Field(ge=0, le=200)
    # Deprecated — kept at zero for new payloads. Previously held the global
    # baseline capacity; the tag-driven planner derives capacity per track.
    average_capacity: float = Field(default=0, ge=0, le=999999)
    buffer_percent: float = Field(default=20, ge=0, le=80)
    tracks: Optional[list[SprintPlanTrack]] = Field(default=None, max_length=20)
    velocity_history: list[SprintPlanHistoryEntry] = Field(default_factory=list, max_length=20)
    roles: list[SprintPlanRoleInput] = Field(default_factory=list, max_length=30)
    # Actual SP closed during this sprint, per track. Entered by the
    # manager at sprint end (compare with the recommended plan).
    actual_by_track: Optional[dict[str, float]] = Field(default=None)
    notes: str = Field(default="", max_length=2000)
    result_summary: Optional[str] = Field(default=None, max_length=200)


class SprintPlanCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    payload: SprintPlanPayload
    team_id: Optional[int] = None


class SprintPlanUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    payload: SprintPlanPayload

# ---------------------------------------------------------------------------
# Sprint planner (velocity + capacity calculator with persistence).
# ---------------------------------------------------------------------------


@router.get("/cms/sprint-plans")
async def cms_list_sprint_plans(
    request: Request,
    team_id: Optional[int] = None,
    sort: Optional[str] = Query(default=None, pattern="^(team_then_updated)?$"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    items = await _get_cms_store(request).list_sprint_plans(
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )
    return {"items": items}


@router.post("/cms/sprint-plans")
async def cms_create_sprint_plan(
    body: SprintPlanCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    plan = await _get_cms_store(request).create_sprint_plan(
        name=body.name,
        payload=body.payload.model_dump(),
        created_by=actor.id,
        team_id=resolved_team_id,
    )
    await _audit(
        request,
        "cms.sprint_plan.create",
        actor.username,
        "ok",
        {"plan_id": plan["id"], "name": plan["name"]},
    )
    return plan


@router.get("/cms/sprint-plans/{plan_id}")
async def cms_get_sprint_plan(
    plan_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    plan = await _get_cms_store(request).get_sprint_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Sprint plan not found")
    assert_record_access(actor, plan)
    return plan


@router.put("/cms/sprint-plans/{plan_id}")
async def cms_update_sprint_plan(
    plan_id: int,
    body: SprintPlanUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_sprint_plan(plan_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint plan not found")
    assert_record_access(actor, existing)
    plan = await _get_cms_store(request).update_sprint_plan(
        plan_id=plan_id,
        name=body.name,
        payload=body.payload.model_dump(),
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Sprint plan not found")
    await _audit(
        request,
        "cms.sprint_plan.update",
        actor.username,
        "ok",
        {"plan_id": plan_id, "name": plan["name"]},
    )
    return plan


@router.delete("/cms/sprint-plans/{plan_id}")
async def cms_delete_sprint_plan(
    plan_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_sprint_plan(plan_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint plan not found")
    assert_record_access(actor, existing)
    deleted = await _get_cms_store(request).delete_sprint_plan(plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Sprint plan not found")
    await _audit(
        request,
        "cms.sprint_plan.delete",
        actor.username,
        "ok",
        {"plan_id": plan_id},
    )
    return {"ok": True, "id": plan_id}

