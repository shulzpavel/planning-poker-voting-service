"""CMS admin API for superuser dashboards.

Cross-cutting HTTP helpers (auth principal, audit logging, common pydantic
models, jira-preview proxy, broadcast publishing) used to live here and were
imported by ``app_api`` — which also caused a circular import in the other
direction. They now live in ``_http_shared`` and are re-exported below so
existing imports keep working.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.domain.session import Session
from app.domain.task import Task
from app.domain.scope_board import (
    apply_priority_queue_comment,
    apply_priority_queue_reorder,
    build_release_context,
    build_scope_snapshot,
    compute_scope_metrics,
    compute_scope_metrics_from_sections,
    compute_scope_report,
    compute_scope_report_from_sections,
    infer_scope_report_type,
    infer_release_version_lookup,
    is_scope_creep,
    merge_priority_queue,
    merge_jira_role_fields_configured,
    merge_scope_issues,
    normalise_workload_mode,
    normalize_scope_issue,
    normalize_scope_sections,
    normalize_version_meta,
    pause_supplement_jql,
    refresh_scope_snapshot_metrics,
    priority_queue_label,
    priority_queue_milestone_targets,
    queue_significance_positions,
    release_scope_sections,
    sync_legacy_jql_from_sections,
)
from app.usecases.close_session import CloseSessionUseCase
from app.usecases.manage_tasks import (
    AddManualTaskUseCase,
    AddManualTasksUseCase,
    DeleteTaskUseCase,
    MoveTaskUseCase,
    ReorderTasksUseCase,
    TaskMutationResult,
    TaskQueueError,
    UpdateTaskUseCase,
)
from services.voting_service.cms_auth import build_cms_token_payload
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT, token_hash as compute_token_hash
from services.voting_service.session_finish_notify import maybe_notify_session_finished
from services.voting_service.cms_team_access import (
    assert_record_access,
    assert_user_sessions_access,
    require_superuser,
    resolve_create_team_id,
    team_scope,
)
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service.scope_ai_jira_export import normalize_plan_epic_key
from services.voting_service.cms_rbac import (
    PERM_ACCESS_MANAGE,
    PERM_ACCESS_VIEW,
    PERM_APP_SESSIONS_MANAGE,
    PERM_EVENTS_VIEW,
    PERM_OVERVIEW_VIEW,
    PERM_PLANNER_VIEW,
    PERM_SESSIONS_VIEW,
    PERM_TOKENS_VIEW,
    PERM_TASKS_MANAGE,
    PERM_USERS_VIEW,
    PERM_VOTES_VIEW,
    PERM_WEB_PARTICIPANTS_DELETE,
    PERM_WEB_VIEW,
)
from services.voting_service._http_shared import (
    ALLOWED_THEME_PREFERENCES,
    AuthDep,
    CMS_COOKIE_NAME,
    CMS_COOKIE_SECURE,
    CMS_LOGIN_IP_MAX_ATTEMPTS,
    CMS_LOGIN_IP_WINDOW_SECONDS,
    CMS_LOGIN_MAX_ATTEMPTS,
    CMS_LOGIN_WINDOW_SECONDS,
    CMS_TOKEN_TTL,
    CmsPrincipal,
    DEFAULT_THEME_PREFERENCE,
    JiraImportRequest,
    JiraPreviewRequest,
    TaskCreateRequest,
    TaskInput,
    TaskMoveRequest,
    TaskReorderRequest,
    TaskUpdateRequest,
    ThemePreference,
    _audit,
    _client_ip,
    _existing_jira_keys,
    _extract_bearer,
    _fetch_jira_description,
    _get_cms_store,
    _get_redis,
    _get_repo_session,
    _jira_preview,
    _jira_preview_payload,
    _mutate_repo_session,
    _mutation_payload,
    _principal_from_record,
    _publish_state,
    _raise_task_error,
    _require_auth,
    _save_repo_session,
    _task_payload,
    require_permission,
)

logger = logging.getLogger(__name__)

cms_router = APIRouter()


# ---------------------------------------------------------------------------
# Re-exports for backward compatibility (do NOT delete — external callers and
# tests import these names from ``services.voting_service.cms_api``).
# ---------------------------------------------------------------------------

__all__ = [
    "CmsPrincipal",
    "_audit",
    "_existing_jira_keys",
    "_jira_preview",
    "_jira_preview_payload",
    "_mutation_payload",
    "_raise_task_error",
    "_publish_state",
    "require_permission",
    "cms_router",
]


class LoginRequest(BaseModel):
    username: str
    password: str


class RoleCreateRequest(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    permission_keys: list[str] = Field(default_factory=list)


class RoleUpdateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    permission_keys: list[str] = Field(default_factory=list)


class ParticipantHardDeleteRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, min_length=1, max_length=32)
    confirm_name: str = Field(min_length=1, max_length=200)


class AdminCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=120)
    is_active: bool = True
    role_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)


class AdminUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    is_active: bool = True
    role_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)


class TeamCreateRequest(BaseModel):
    slug: Optional[str] = Field(default=None, min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class TeamUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None


class SessionRenameRequest(BaseModel):
    """Rename a CMS session. Empty string clears the custom title and
    callers fall back to the technical identifier."""

    title: Optional[str] = Field(default=None, max_length=200)


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


class ScopeSectionConfigRequest(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    jql: str = Field(min_length=1, max_length=4000)
    kind: Literal["planned", "unplanned"] = "planned"
    order: int = Field(ge=0, le=99)


class ScopeBoardCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    month: str = Field(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$")
    capacity_sp: float = Field(ge=0, le=99999)
    capacity_sp_dev: Optional[float] = Field(default=None, ge=0, le=99999)
    capacity_sp_test: Optional[float] = Field(default=None, ge=0, le=99999)
    workload_mode: Literal["sp", "sp_dev_test"] = "sp"
    scope_sections: list[ScopeSectionConfigRequest] = Field(min_length=1, max_length=20)
    plan_jql: str = Field(default="", max_length=4000)
    unplan_jql: str = Field(default="", max_length=4000)
    todo_jql: str = Field(default="", max_length=4000)
    test_jql: str = Field(default="", max_length=4000)
    previous_release_jql: str = Field(default="", max_length=4000)
    next_release_jql: str = Field(default="", max_length=4000)
    custom_release_name: str = Field(default="", max_length=200)
    custom_release_jql: str = Field(default="", max_length=4000)
    release_queries: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)
    plan_epic_key: str = Field(default="", max_length=64)
    team_id: Optional[int] = None


class ScopeBoardUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    month: str = Field(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$")
    capacity_sp: float = Field(ge=0, le=99999)
    capacity_sp_dev: Optional[float] = Field(default=None, ge=0, le=99999)
    capacity_sp_test: Optional[float] = Field(default=None, ge=0, le=99999)
    workload_mode: Literal["sp", "sp_dev_test"] = "sp"
    scope_sections: list[ScopeSectionConfigRequest] = Field(min_length=1, max_length=20)
    plan_jql: str = Field(default="", max_length=4000)
    unplan_jql: str = Field(default="", max_length=4000)
    todo_jql: str = Field(default="", max_length=4000)
    test_jql: str = Field(default="", max_length=4000)
    previous_release_jql: str = Field(default="", max_length=4000)
    next_release_jql: str = Field(default="", max_length=4000)
    custom_release_name: str = Field(default="", max_length=200)
    custom_release_jql: str = Field(default="", max_length=4000)
    release_queries: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)
    plan_epic_key: str = Field(default="", max_length=64)


class ScopeBoardReleaseCommentsRequest(BaseModel):
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)


class ScopeBoardLayoutRequest(BaseModel):
    layout_order: list[str] = Field(min_length=1, max_length=20)


SCOPE_LAYOUT_BLOCK_KEYS = frozenset({
    "topItems",
    "capacity",
    "roleWorkload",
    "planInsights",
    "aiSummary",
    "report",
    "priorityQueues",
    "activity",
    "snapshotSections",
    "settings",
})

DEFAULT_SCOPE_LAYOUT_ORDER = [
    "topItems",
    "capacity",
    "roleWorkload",
    "planInsights",
    "aiSummary",
    "report",
    "priorityQueues",
    "activity",
    "snapshotSections",
    "settings",
]


def _normalize_scope_layout_order(layout_order: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in layout_order:
        key = str(item).strip()
        if not key or key not in SCOPE_LAYOUT_BLOCK_KEYS or key in seen:
            continue
        cleaned.append(key)
        seen.add(key)
    for key in DEFAULT_SCOPE_LAYOUT_ORDER:
        if key not in seen:
            cleaned.append(key)
            seen.add(key)
    return cleaned


class ScopeIssueCommentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ScopeReportCommentRequest(BaseModel):
    text: str = Field(default="", max_length=2000)


class ScopeIssueDueDateRequest(BaseModel):
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ScopeManualQuestionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ScopeTopItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ScopeTodoItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ScopeTodoItemUpdateRequest(BaseModel):
    done: bool


class ScopeResolveQuestionRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)


class ScopeQueueReorderRequest(BaseModel):
    order: list[str] = Field(min_length=1, max_length=500)
    comment: str = Field(default="", max_length=4000)
    moved_key: str = Field(min_length=1, max_length=64)


async def _session_ref(
    request: Request,
    session_id: int,
    actor: CmsPrincipal,
) -> tuple[int, Optional[int]]:
    detail = await _get_cms_store(request).get_session(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    assert_record_access(actor, detail)
    return int(detail["chat_id"]), detail.get("topic_id")


async def _ensure_login_not_limited(redis_client: aioredis.Redis, username: str, ip: str) -> str:
    key = f"cms_login_fail:{username}:{ip}"
    attempts_raw = await redis_client.get(key)
    attempts = int(attempts_raw or 0)
    if attempts >= CMS_LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts")
    return key


async def _record_login_failure(redis_client: aioredis.Redis, key: str) -> None:
    attempts = await redis_client.incr(key)
    if attempts == 1:
        await redis_client.expire(key, CMS_LOGIN_WINDOW_SECONDS)


@cms_router.post("/cms/auth/login")
async def cms_login(body: LoginRequest, request: Request, response: Response) -> dict:
    redis_client = await _get_redis(request)
    ip = _client_ip(request)
    # Defence-in-depth: cap total login attempts from a single IP
    # regardless of username, so username enumeration from one source
    # cannot stay under the per-username quota indefinitely.
    await enforce_rate_limit(
        redis_client,
        key=f"rl:login:ip:{ip}",
        limit=CMS_LOGIN_IP_MAX_ATTEMPTS,
        window_seconds=CMS_LOGIN_IP_WINDOW_SECONDS,
        error_detail="Too many login attempts",
    )
    fail_key = await _ensure_login_not_limited(redis_client, body.username, ip)

    principal_record = await _get_cms_store(request).verify_admin_login(body.username, body.password)
    if not principal_record:
        await _record_login_failure(redis_client, fail_key)
        await _audit(request, "cms.login", body.username, "failed", {"reason": "invalid_credentials"})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    await redis_client.delete(fail_key)
    token = secrets.token_urlsafe(32)
    token_payload = build_cms_token_payload(
        admin_id=int(principal_record["id"]),
        username=principal_record["username"],
        ip=ip,
        token_version=int(principal_record.get("token_version", 1)),
        ttl_seconds=CMS_TOKEN_TTL,
    )
    await redis_client.setex(
        f"cms_token:{token}",
        CMS_TOKEN_TTL,
        json.dumps(token_payload),
    )
    response.set_cookie(
        CMS_COOKIE_NAME,
        token,
        max_age=CMS_TOKEN_TTL,
        httponly=True,
        secure=CMS_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    await _audit(request, "cms.login", principal_record["username"], "ok")
    return {"ok": True, "expires_in": CMS_TOKEN_TTL}


@cms_router.post("/cms/auth/logout")
async def cms_logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
    cookie_token: Optional[str] = Cookie(default=None, alias=CMS_COOKIE_NAME),
    actor: CmsPrincipal = AuthDep,
) -> dict:
    token = cookie_token or _extract_bearer(authorization)
    if token:
        redis_client = await _get_redis(request)
        await redis_client.delete(f"cms_token:{token}")
    response.delete_cookie(CMS_COOKIE_NAME, path="/")
    await _audit(request, "cms.logout", actor.username, "ok")
    return {"ok": True}


@cms_router.get("/cms/auth/me")
async def cms_me(actor: CmsPrincipal = AuthDep) -> dict:
    return {
        "id": actor.id,
        "username": actor.username,
        "display_name": actor.display_name,
        "is_superuser": actor.is_superuser,
        "permissions": sorted(actor.permissions),
        "roles": list(actor.roles),
        "pages": list(actor.pages),
        "teams": list(actor.teams),
        "team_ids": sorted(actor.team_ids),
        "theme_preference": actor.theme_preference,
    }


class PreferencesUpdateRequest(BaseModel):
    theme_preference: str = Field(pattern=r"^(dark|light|system)$")


@cms_router.patch("/cms/auth/me/preferences")
async def cms_update_preferences(
    body: PreferencesUpdateRequest,
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    """Persist UI preferences (currently: theme) for the authenticated CMS user."""
    theme = body.theme_preference
    if theme not in ALLOWED_THEME_PREFERENCES:
        raise HTTPException(status_code=400, detail="Invalid theme_preference")
    store = _get_cms_store(request)
    try:
        ok = await store.update_admin_theme_preference(actor.id, theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        # Account is missing or deactivated — treat as unauthorized rather than 404
        # to avoid leaking account state.
        raise HTTPException(status_code=401, detail="Account is no longer active")
    await _audit(
        request,
        "cms.preferences.update",
        actor.username,
        "ok",
        {"theme_preference": theme},
    )
    return {"ok": True, "theme_preference": theme}


@cms_router.get("/cms/overview")
async def cms_overview(
    request: Request,
    team_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_OVERVIEW_VIEW)),
) -> dict:
    scope = team_scope(actor)
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _get_cms_store(request).overview(team_id=team_id, **scope)


@cms_router.get("/cms/teams")
async def cms_list_teams(
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    items = await _get_cms_store(request).list_teams(**team_scope(actor))
    return {"items": items}


@cms_router.post("/cms/teams")
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


@cms_router.patch("/cms/teams/{team_id}")
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


@cms_router.get("/cms/sessions")
async def cms_sessions(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    active: Optional[bool] = None,
    chat_id: Optional[int] = None,
    topic_id: Optional[int] = None,
    team_id: Optional[int] = None,
    sort: Optional[str] = Query(default=None, pattern="^(team_then_updated)?$"),
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    return await _get_cms_store(request).list_sessions(
        limit=limit,
        cursor=cursor,
        q=q,
        active=active,
        chat_id=chat_id,
        topic_id=topic_id,
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )


@cms_router.get("/cms/sessions/{session_id}")
async def cms_session_detail(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    session = await _get_cms_store(request).get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    assert_record_access(actor, session)
    return session


@cms_router.patch("/cms/sessions/{session_id}")
async def cms_rename_session(
    session_id: int,
    body: SessionRenameRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Rename a CMS session. Title is optional human-readable name shown in
    place of the technical ``chat_id:topic_id`` key. Sending ``null`` or an
    empty string clears the custom title."""
    store = _get_cms_store(request)
    existing = await store.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")
    assert_record_access(actor, existing)
    result = await store.rename_session(session_id, body.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")
    _, _, new_title = result
    await _audit(
        request,
        "cms.session.rename",
        actor.username,
        "ok",
        {"session_id": session_id, "title": new_title},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "title": new_title,
    }


@cms_router.get("/cms/sessions/{session_id}/participants")
async def cms_session_participants(
    session_id: int,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    await _session_ref(request, session_id, actor)
    return await _get_cms_store(request).list_session_participants(session_id, limit, cursor)


@cms_router.get("/cms/sessions/{session_id}/tasks")
async def cms_session_tasks(
    session_id: int,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    bucket: Optional[str] = Query(None, pattern="^(tasks_queue|history|last_batch)$"),
    q: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    await _session_ref(request, session_id, actor)
    return await _get_cms_store(request).list_session_tasks(session_id, limit, cursor, bucket, q)


@cms_router.post("/cms/sessions/{session_id}/tasks")
async def cms_create_session_task(
    session_id: int,
    body: TaskCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = AddManualTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            summary=body.summary,
            jira_key=body.jira_key,
            url=body.url,
            story_points=body.story_points,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.create", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.create", actor.username, "ok", {"session_id": session_id, "task_id": result.task.task_id if result.task else None})
    return _mutation_payload(result, session_id)


@cms_router.patch("/cms/sessions/{session_id}/tasks/{task_id}")
async def cms_update_session_task(
    session_id: int,
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = UpdateTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            summary=body.summary,
            jira_key=body.jira_key,
            url=body.url,
            story_points=body.story_points,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.update", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.update", actor.username, "ok", {"session_id": session_id, "task_id": task_id})
    return _mutation_payload(result, session_id)


@cms_router.delete("/cms/sessions/{session_id}/tasks/{task_id}")
async def cms_delete_session_task(
    session_id: int,
    task_id: str,
    request: Request,
    expected_version: Optional[int] = Query(default=None, ge=0),
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = DeleteTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            expected_version=expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.delete", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.delete", actor.username, "ok", {"session_id": session_id, "task_id": task_id})
    return _mutation_payload(result, session_id)


@cms_router.post("/cms/sessions/{session_id}/tasks/{task_id}/move")
async def cms_move_session_task(
    session_id: int,
    task_id: str,
    body: TaskMoveRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = MoveTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            target_index=body.target_index,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.move", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(
        request,
        "cms.task.move",
        actor.username,
        "ok",
        {"session_id": session_id, "task_id": task_id, "target_index": body.target_index},
    )
    return _mutation_payload(result, session_id)


@cms_router.post("/cms/sessions/{session_id}/tasks/reorder")
async def cms_reorder_session_tasks(
    session_id: int,
    body: TaskReorderRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = ReorderTasksUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            ordered_task_ids=body.ordered_task_ids,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.reorder", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.reorder", actor.username, "ok", {"session_id": session_id, "count": len(body.ordered_task_ids)})
    return _mutation_payload(result, session_id)


@cms_router.post("/cms/sessions/{session_id}/tasks/jira-preview")
async def cms_preview_jira_tasks(
    session_id: int,
    body: JiraPreviewRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)
    return _jira_preview_payload(issues, _existing_jira_keys(session))


@cms_router.post("/cms/sessions/{session_id}/tasks/jira-import")
async def cms_import_jira_tasks(
    session_id: int,
    body: JiraImportRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    try:
        selected = {key.strip().upper() for key in body.selected_keys if key.strip()}
        issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)

        # Same best-effort description pre-fetch as the manager import path —
        # see app_api.app_import_jira_tasks for the rationale.
        keys_to_fetch = [
            str(issue.get("key") or "").strip().upper()
            for issue in issues
            if str(issue.get("key") or "").strip()
            and (not selected or str(issue.get("key") or "").strip().upper() in selected)
        ]
        fetched_payloads = (
            await asyncio.gather(
                *[
                    _fetch_jira_description(request.app.state.http_session, key)
                    for key in keys_to_fetch
                ]
            )
            if keys_to_fetch
            else []
        )
        descriptions = dict(zip(keys_to_fetch, fetched_payloads))
        # Same import-side log line as the manager path — see app_api.
        logger.info(
            "jira import description fetch (cms) chat=%s tried=%d filled_text=%d filled_adf=%d filled_html=%d",
            chat_id,
            len(keys_to_fetch),
            sum(1 for v in descriptions.values() if v.text),
            sum(1 for v in descriptions.values() if v.adf),
            sum(1 for v in descriptions.values() if v.html),
        )

        def mutate(session: Session) -> TaskMutationResult:
            if body.expected_version is not None and body.expected_version != session.tasks_version:
                raise TaskQueueError("Task queue was changed. Refresh and try again.", status_code=409)

            existing_keys = _existing_jira_keys(session)
            added: list[Task] = []
            seen: set[str] = set()
            for issue in issues:
                key = str(issue.get("key") or "").strip().upper()
                if not key or key in existing_keys or key in seen:
                    continue
                if selected and key not in selected:
                    continue
                fetched = descriptions.get(key)
                task = Task(
                    jira_key=key,
                    summary=issue.get("summary") or key,
                    url=issue.get("url"),
                    story_points=issue.get("story_points"),
                    jql=body.jql,
                    source="jira",
                    description=fetched.text if fetched else None,
                    description_adf=fetched.adf if fetched else None,
                    description_html=fetched.html if fetched else None,
                )
                session.tasks_queue.append(task)
                added.append(task)
                seen.add(key)
                existing_keys.add(key)

            if not added:
                raise TaskQueueError("No Jira tasks to import")

            session.batch_completed = False
            session.bump_tasks_version()
            return TaskMutationResult(session=session, task=added[-1], tasks=tuple(added))

        _, result = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    except TaskQueueError as exc:
        await _audit(request, "cms.task.jira_import", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)

    await _audit(request, "cms.task.jira_import", actor.username, "ok", {"session_id": session_id, "count": len(result.tasks)})
    return _mutation_payload(result, session_id)


# ---------------------------------------------------------------------------
# Session lifecycle: close (force-finish) and soft-delete
# ---------------------------------------------------------------------------


async def _broadcast_session_state(request: Request, session: Session) -> None:
    """Publish a fresh state snapshot so participants see CMS-driven changes.

    ``_publish_state`` is itself best-effort (swallows pub/sub failures) but we
    add a second guard here so a CMS-driven mutation surface never returns
    5xx because the broadcast attempt blew up in an unexpected way.
    """
    try:
        await _publish_state(request, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CMS broadcast failed: %s", exc)


async def _purge_redis_tokens_for_session(request: Request, chat_id: int, topic_id: Optional[int]) -> None:
    """Best-effort: drop live ``web:<token>`` keys belonging to the session.

    Database expiry is updated by ``cms_store.revoke_web_token`` per token, but
    that only flips the read-model. The actual short-lived Redis entries
    (which authorize ``GET /web/state/...``) are wiped here so participants
    lose access immediately when an admin removes the session.
    """
    redis_client = getattr(request.app.state, "web_redis", None)
    if not redis_client:
        return
    try:
        async for key in redis_client.scan_iter(match="web:*", count=200):
            raw = await redis_client.get(key)
            if not raw:
                continue
            try:
                info = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if int(info.get("chat_id", -1)) != chat_id:
                continue
            stored_topic = info.get("topic_id")
            if (stored_topic is None) != (topic_id is None):
                continue
            if stored_topic is not None and int(stored_topic) != int(topic_id or 0):
                continue
            await redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis web-token purge failed: %s", exc)


@cms_router.post("/cms/sessions/{session_id}/close")
async def cms_close_session(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Force-finish a session from the CMS. Idempotent — safe to call twice.

    Behaves identically to the manager-driven ``POST /app/sessions/{chat_id}/finish``
    — both delegate to ``CloseSessionUseCase``.
    """
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    repo = request.app.state.repository
    before = await _get_repo_session(repo, chat_id, topic_id)
    was_completed = before.batch_completed

    use_case = CloseSessionUseCase(repo)
    refreshed_session, completed = await use_case.execute(chat_id, topic_id)
    await _broadcast_session_state(request, refreshed_session)
    await _audit(
        request,
        "cms.session.close",
        actor.username,
        "ok",
        {"session_id": session_id, "completed_count": len(completed)},
    )
    await maybe_notify_session_finished(
        request,
        refreshed_session,
        was_completed=was_completed,
        actor=actor,
        close_method="CMS force-close",
    )
    return {
        "ok": True,
        "session_id": session_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "completed_count": len(completed),
        "batch_completed": True,
    }


@cms_router.delete("/cms/sessions/{session_id}")
async def cms_delete_session(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Soft-delete a session.

    The CMS read-model row is flagged ``deleted_at`` so it disappears from all
    listings together with its tasks/votes/participants/tokens. The live
    Redis state and any pending invite tokens are also dropped so the deleted
    session cannot be reopened by a stale browser tab.
    """
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    store = _get_cms_store(request)
    deleted_ref = await store.soft_delete_session(session_id)
    if deleted_ref is None:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")

    repo = request.app.state.repository
    try:
        if hasattr(repo, "delete_session_async"):
            await repo.delete_session_async(chat_id, topic_id)
        elif hasattr(repo, "delete_session"):
            await repo.delete_session(chat_id, topic_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live session delete failed (id=%s): %s", session_id, exc)

    await _purge_redis_tokens_for_session(request, chat_id, topic_id)

    await _audit(
        request,
        "cms.session.delete",
        actor.username,
        "ok",
        {"session_id": session_id, "chat_id": chat_id, "topic_id": topic_id},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "deleted": True,
    }


@cms_router.delete("/cms/tokens/{token_id}")
async def cms_revoke_token(
    token_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Revoke a single invite token immediately.

    Only the read-model token_hash is recorded in Postgres, so we can't
    construct the original ``web:<token>`` Redis key from the hash. Instead we
    scan Redis once and drop the matching ``web:`` key by comparing hashes.
    """
    store = _get_cms_store(request)
    token = await store.get_web_token(token_id)
    if not token or not token.get("is_active"):
        raise HTTPException(status_code=404, detail="Token not found or already expired")
    assert_record_access(actor, token)

    revoked_hash = await store.revoke_web_token(token_id)
    if not revoked_hash:
        raise HTTPException(status_code=404, detail="Token not found or already expired")

    redis_client = getattr(request.app.state, "web_redis", None)
    if redis_client:
        try:
            async for key in redis_client.scan_iter(match="web:*", count=200):
                token = key.removeprefix("web:")
                if compute_token_hash(token) == revoked_hash:
                    await redis_client.delete(key)
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis revoke for token id=%s failed: %s", token_id, exc)

    await _audit(
        request,
        "cms.token.revoke",
        actor.username,
        "ok",
        {"token_id": token_id},
    )
    return {"ok": True, "token_id": token_id, "revoked": True}


@cms_router.get("/cms/users")
async def cms_users(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    role: Optional[str] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_USERS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_users(limit=limit, cursor=cursor, q=q, role=role)


@cms_router.delete("/cms/users")
@cms_router.delete("/cms/users/{path_user_id}")
async def cms_hard_delete_user(
    body: ParticipantHardDeleteRequest,
    request: Request,
    path_user_id: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_WEB_PARTICIPANTS_DELETE)),
) -> dict:
    store = _get_cms_store(request)
    raw_user_id = body.user_id or path_user_id
    if raw_user_id is None:
        raise HTTPException(status_code=400, detail="Invalid participant id")
    try:
        user_id = int(raw_user_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid participant id") from exc

    team_ids = await store.get_user_session_team_ids(user_id)
    if team_ids:
        assert_user_sessions_access(actor, team_ids)

    try:
        deleted = await store.hard_delete_user(user_id, body.confirm_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Type the participant name exactly to confirm deletion") from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    return {
        "ok": True,
        "user_id": deleted["user_id"],
        "deleted": True,
        "votes_deleted": deleted["votes_deleted"],
        "session_participants_deleted": deleted["session_participants_deleted"],
        "web_participants_deleted": deleted["web_participants_deleted"],
        "actor": actor.username,
    }


@cms_router.get("/cms/votes")
async def cms_votes(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    user_id: Optional[int] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_VOTES_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_votes(
        limit=limit,
        cursor=cursor,
        session_id=session_id,
        task_id=task_id,
        user_id=user_id,
    )


@cms_router.get("/cms/tokens")
async def cms_tokens(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    active: Optional[bool] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_TOKENS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_web_tokens(limit=limit, cursor=cursor, active=active)


@cms_router.get("/cms/web-participants")
async def cms_web_participants(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    token_hash: Optional[str] = None,
    active: Optional[bool] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_WEB_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_web_participants(
        limit=limit,
        cursor=cursor,
        token_hash_filter=token_hash,
        active=active,
    )


@cms_router.get("/cms/events")
async def cms_events(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    actor: Optional[str] = Query(default=None, max_length=120),
    ts_from: Optional[datetime] = None,
    ts_to: Optional[datetime] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_EVENTS_VIEW)),
) -> dict:
    """Paged audit-events feed.

    ``actor`` filters by exact username (case sensitive). ``ts_from`` and
    ``ts_to`` are inclusive bounds and are applied before cursor pagination.
    """
    return await _get_cms_store(request).list_audit_events(
        limit=limit,
        cursor=cursor,
        action=action,
        status=status,
        actor=actor,
        ts_from=ts_from,
        ts_to=ts_to,
    )


@cms_router.get("/cms/access/permissions")
async def cms_access_permissions(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_permissions()}


@cms_router.get("/cms/access/pages")
async def cms_access_pages(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_pages()}


@cms_router.get("/cms/access/roles")
async def cms_access_roles(
    request: Request,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return {"items": await _get_cms_store(request).list_cms_roles()}


@cms_router.post("/cms/access/roles")
async def cms_access_create_role(
    body: RoleCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    try:
        role = await _get_cms_store(request).create_cms_role(
            key=body.key,
            name=body.name,
            description=body.description,
            permission_keys=body.permission_keys,
        )
    except Exception as exc:
        await _audit(request, "cms.access.role.create", actor.username, "failed", {"error": str(exc)})
        raise HTTPException(status_code=400, detail="Role could not be created") from exc
    await _audit(request, "cms.access.role.create", actor.username, "ok", {"role_id": role["id"]})
    return role


@cms_router.patch("/cms/access/roles/{role_id}")
async def cms_access_update_role(
    role_id: int,
    body: RoleUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    role = await _get_cms_store(request).update_cms_role(
        role_id=role_id,
        name=body.name,
        description=body.description,
        permission_keys=body.permission_keys,
    )
    if not role:
        raise HTTPException(status_code=404, detail="Role not found or system role is read-only")
    await _audit(request, "cms.access.role.update", actor.username, "ok", {"role_id": role_id})
    return role


@cms_router.get("/cms/access/admins")
async def cms_access_admins(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    active: Optional[bool] = None,
    role_id: Optional[int] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_ACCESS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_cms_admins(
        limit=limit,
        cursor=cursor,
        q=q,
        active=active,
        role_id=role_id,
    )


@cms_router.post("/cms/access/admins")
async def cms_access_create_admin(
    body: AdminCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    try:
        if not actor.is_superuser and body.team_ids:
            raise HTTPException(status_code=403, detail="Forbidden")
        admin = await _get_cms_store(request).create_cms_admin(
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            is_active=body.is_active,
            role_ids=body.role_ids,
            team_ids=body.team_ids if actor.is_superuser else [],
        )
    except Exception as exc:
        await _audit(request, "cms.access.admin.create", actor.username, "failed", {"error": str(exc)})
        raise HTTPException(status_code=400, detail="Admin could not be created") from exc
    await _audit(request, "cms.access.admin.create", actor.username, "ok", {"admin_id": admin["id"]})
    return admin


@cms_router.patch("/cms/access/admins/{admin_id}")
async def cms_access_update_admin(
    admin_id: int,
    body: AdminUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_ACCESS_MANAGE)),
) -> dict:
    if admin_id == actor.id and not body.is_active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own admin account")
    if not actor.is_superuser and body.team_ids:
        raise HTTPException(status_code=403, detail="Forbidden")
    admin = await _get_cms_store(request).update_cms_admin(
        admin_id=admin_id,
        display_name=body.display_name,
        is_active=body.is_active,
        role_ids=body.role_ids,
        password=body.password,
        team_ids=body.team_ids if actor.is_superuser else None,
        update_teams=actor.is_superuser,
    )
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")
    await _audit(request, "cms.access.admin.update", actor.username, "ok", {"admin_id": admin_id})
    return admin


# ---------------------------------------------------------------------------
# Sprint planner (velocity + capacity calculator with persistence).
# ---------------------------------------------------------------------------


@cms_router.get("/cms/sprint-plans")
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


@cms_router.post("/cms/sprint-plans")
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


@cms_router.get("/cms/sprint-plans/{plan_id}")
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


@cms_router.put("/cms/sprint-plans/{plan_id}")
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


@cms_router.delete("/cms/sprint-plans/{plan_id}")
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


SCOPE_JQL_MAX_RESULTS = max(1, int(os.getenv("SCOPE_JQL_MAX_RESULTS", "500")))


@dataclass
class _ScopeJqlFetchResult:
    jql: str
    issues: list[dict[str, Any]]
    failed: bool = False
    truncated: bool = False
    jira_role_fields_configured: dict[str, bool] | None = None


def _count_snapshot_issues(snapshot: dict[str, Any] | None) -> int:
    if not snapshot:
        return 0
    total = 0
    for section in snapshot.get("sections") or []:
        total += len(section.get("issues") or [])
    for bucket in ("plan_issues", "unplan_issues"):
        total += len(snapshot.get(bucket) or [])
    for queue_name in ("todo", "test"):
        queue = (snapshot.get("priority_queues") or {}).get(queue_name) or {}
        total += len(queue.get("issues") or [])
    return total


async def _fetch_scope_issues(
    jql: str,
    client: Any,
    *,
    force_refresh: bool = False,
    milestone_status_targets: list[str] | None = None,
    enrich_changelog: bool = False,
) -> _ScopeJqlFetchResult:
    cleaned = (jql or "").strip()
    if not cleaned:
        return _ScopeJqlFetchResult(jql="", issues=[])
    try:
        raw_payload = await client.parse_jira_scope_issues(
            cleaned,
            max_results=SCOPE_JQL_MAX_RESULTS,
            force_refresh=force_refresh,
            milestone_status_targets=milestone_status_targets,
            enrich_changelog=enrich_changelog,
        )
    except Exception as exc:
        logger.warning("scope jql fetch failed jql=%s error=%s", cleaned, exc)
        return _ScopeJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if raw_payload is None:
        return _ScopeJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if isinstance(raw_payload, dict):
        raw_issues = raw_payload.get("issues") or []
        configured = raw_payload.get("jira_role_fields_configured") or {}
    else:
        raw_issues = raw_payload
        configured = {}
    issues = [normalize_scope_issue(issue) for issue in raw_issues]
    return _ScopeJqlFetchResult(
        jql=cleaned,
        issues=issues,
        truncated=len(issues) >= SCOPE_JQL_MAX_RESULTS,
        jira_role_fields_configured=configured,
    )


async def _fetch_scope_sections(
    sections: list[dict[str, Any]],
    client: Any,
    *,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[_ScopeJqlFetchResult]]:
    fetched_sections: list[dict[str, Any]] = []
    outcomes: list[_ScopeJqlFetchResult] = []
    for section in sections:
        jql = str(section.get("jql") or "").strip()
        if not jql:
            fetched_sections.append({**section, "issues": []})
            continue
        base_outcome, pause_outcome = await asyncio.gather(
            _fetch_scope_issues(
                jql,
                client,
                force_refresh=force_refresh,
                enrich_changelog=True,
            ),
            _fetch_scope_issues(
                pause_supplement_jql(jql),
                client,
                force_refresh=force_refresh,
                enrich_changelog=True,
            ),
        )
        outcomes.extend([base_outcome, pause_outcome])
        fetched_sections.append(
            {**section, "issues": merge_scope_issues(base_outcome.issues, pause_outcome.issues)}
        )
    return fetched_sections, outcomes


def _scope_sections_from_request(body: ScopeBoardCreateRequest | ScopeBoardUpdateRequest) -> list[dict[str, Any]]:
    if body.scope_sections:
        return normalize_scope_sections([section.model_dump() for section in body.scope_sections])
    return normalize_scope_sections(None, plan_jql=body.plan_jql, unplan_jql=body.unplan_jql)


async def _resolve_scope_report_type(store: Any, team_id: Optional[int]) -> str:
    if team_id is None:
        return "monthly"
    team = await store.get_team(team_id)
    if not team:
        return "monthly"
    return infer_scope_report_type(team.get("slug"), team.get("name"))


def _ensure_release_scope_sections(scope_sections: list[dict[str, Any]]) -> None:
    release_jql = str((scope_sections[0] if scope_sections else {}).get("jql") or "").strip()
    if not release_jql:
        raise HTTPException(
            status_code=400,
            detail="Укажите JQL релиза, например: project = AIG2 AND fixVersion = 12076",
        )


def _primary_release_jql(scope_sections: list[dict[str, Any]]) -> str:
    if not scope_sections:
        return ""
    return str(scope_sections[0].get("jql") or "").strip()


async def _fetch_release_version_meta(
    client: Any,
    jql: str,
    issues: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    lookup = infer_release_version_lookup(jql, issues)
    version_id = lookup.get("version_id", "")
    version_name = lookup.get("version_name", "")
    project_key = lookup.get("project_key", "")
    if not version_id and not version_name:
        return None
    try:
        raw = await client.resolve_version(
            project_key=project_key,
            version_id=version_id,
            version_name=version_name,
            force_refresh=True,
        )
    except Exception as exc:
        logger.warning(
            "release version meta fetch failed project=%s version_id=%s version_name=%s error=%s",
            project_key,
            version_id,
            version_name,
            exc,
        )
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_version_meta(raw, project_key=project_key)


async def _fetch_release_version_meta_map(
    client: Any,
    jql_by_slot: dict[str, str],
    issues_by_slot: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> dict[str, dict[str, Any]]:
    issues_by_slot = issues_by_slot or {}
    tasks = {
        slot: _fetch_release_version_meta(client, jql, issues=issues_by_slot.get(slot))
        for slot, jql in jql_by_slot.items()
        if (jql or "").strip()
    }
    if not tasks:
        return {}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    meta_map: dict[str, dict[str, Any]] = {}
    for (slot, _), result in zip(tasks.items(), results, strict=True):
        if isinstance(result, dict):
            meta_map[slot] = result
    return meta_map


def _normalize_release_queries(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        jql = str(item.get("jql") or "").strip()
        if not jql:
            continue
        relation = str(item.get("type") or item.get("relation") or "future").strip().lower()
        if relation not in {"past", "future"}:
            relation = "future"
        item_id = str(item.get("id") or "").strip() or f"release-{index + 1}-{secrets.token_hex(3)}"
        while item_id in seen:
            item_id = f"{item_id}-{secrets.token_hex(2)}"
        seen.add(item_id)
        label = str(item.get("label") or "").strip()
        result.append(
            {
                "id": item_id[:120],
                "type": relation,
                "label": label[:200],
                "jql": jql[:4000],
            }
        )
    return result


def _scope_board_payload_from_request(
    body: ScopeBoardCreateRequest | ScopeBoardUpdateRequest,
    *,
    report_type: str,
) -> dict[str, Any]:
    scope_sections = _scope_sections_from_request(body)
    if report_type == "release":
        release_jql = _primary_release_jql(scope_sections)
        scope_sections = release_scope_sections(release_jql, label=scope_sections[0].get("name") if scope_sections else None)
        _ensure_release_scope_sections(scope_sections)
    plan_jql, unplan_jql = sync_legacy_jql_from_sections(scope_sections)
    return {
        "name": body.name,
        "month": body.month,
        "capacity_sp": body.capacity_sp,
        "capacity_sp_dev": body.capacity_sp_dev,
        "capacity_sp_test": body.capacity_sp_test,
        "workload_mode": normalise_workload_mode(body.workload_mode),
        "plan_jql": plan_jql,
        "unplan_jql": unplan_jql,
        "todo_jql": body.todo_jql,
        "test_jql": body.test_jql,
        "report_type": report_type,
        "previous_release_jql": body.previous_release_jql,
        "next_release_jql": body.next_release_jql,
        "custom_release_name": body.custom_release_name,
        "custom_release_jql": body.custom_release_jql,
        "release_queries": _normalize_release_queries(body.release_queries),
        "release_comment": body.release_comment,
        "previous_release_comment": body.previous_release_comment,
        "next_release_comment": body.next_release_comment,
        "custom_release_comment": body.custom_release_comment,
        "scope_sections": scope_sections,
        "plan_epic_key": normalize_plan_epic_key(body.plan_epic_key),
    }


def _scope_fetch_warnings(outcomes: list[_ScopeJqlFetchResult]) -> list[dict[str, Any]]:
    return [
        {"jql": outcome.jql, "truncated": True, "count": len(outcome.issues)}
        for outcome in outcomes
        if outcome.jql and outcome.truncated
    ]


def _scope_sections_from_board(board: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_scope_sections(
        board.get("scope_sections"),
        plan_jql=str(board.get("plan_jql") or ""),
        unplan_jql=str(board.get("unplan_jql") or ""),
    )


def _release_queries_from_board(board: dict[str, Any]) -> list[dict[str, str]]:
    return _normalize_release_queries(board.get("release_queries"))


async def _post_jira_issue_comment(issue_key: str, text: str) -> dict[str, Any]:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.add_issue_comment(issue_key, text)
    finally:
        await client.close()


async def _put_jira_issue_due_date(issue_key: str, due_date: str) -> bool:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.update_due_date(issue_key, due_date)
    finally:
        await client.close()


async def _put_jira_issue_significance(issue_key: str, significance: int) -> bool:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.update_significance(issue_key, significance)
    finally:
        await client.close()


async def _sync_queue_significance_to_jira(order: list[str]) -> None:
    positions = queue_significance_positions(order)
    failures: list[str] = []
    for issue_key, significance in positions.items():
        try:
            saved = await _put_jira_issue_significance(issue_key, significance)
        except Exception as exc:
            logger.warning(
                "scope queue significance Jira update failed key=%s significance=%s error=%s",
                issue_key,
                significance,
                exc,
            )
            failures.append(issue_key)
            continue
        if not saved:
            failures.append(issue_key)
    if failures:
        raise HTTPException(
            status_code=502,
            detail="Порядок сохранён, но значимость не записана в Jira",
        )


def _scope_snapshot_has_issue(snapshot: dict[str, Any], issue_key: str) -> bool:
    target = issue_key.upper()
    for section in snapshot.get("sections") or []:
        for issue in section.get("issues") or []:
            if str(issue.get("key") or "").upper() == target:
                return True
    for section in ("plan_issues", "unplan_issues"):
        for issue in snapshot.get(section) or []:
            if str(issue.get("key") or "").upper() == target:
                return True
    return False


def _scope_snapshot_with_due_date(snapshot: dict[str, Any], *, issue_key: str, due_date: str) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    target = issue_key.upper()

    for section in updated.get("sections") or []:
        for issue in section.get("issues") or []:
            if str(issue.get("key") or "").upper() == target:
                issue["due_date"] = due_date

    for section_name in ("plan_issues", "unplan_issues"):
        for issue in updated.get(section_name) or []:
            if str(issue.get("key") or "").upper() == target:
                issue["due_date"] = due_date

    queues = dict(updated.get("priority_queues") or {})
    for queue_name, queue in queues.items():
        next_queue = dict(queue or {})
        next_issues = []
        for issue in next_queue.get("issues") or []:
            next_issue = dict(issue)
            if str(next_issue.get("key") or "").upper() == target:
                next_issue["due_date"] = due_date
            next_issues.append(next_issue)
        next_queue["issues"] = next_issues
        queues[queue_name] = next_queue
    updated["priority_queues"] = queues
    return updated


def _scope_snapshot_has_queue_issue(snapshot: dict[str, Any], issue_key: str, queue_kind: str) -> bool:
    queues = snapshot.get("priority_queues") or {}
    queue = queues.get(queue_kind) or {}
    target = issue_key.upper()
    for issue in queue.get("issues") or []:
        if str(issue.get("key") or "").upper() == target:
            return True
    return False


def _scope_snapshot_with_comment(
    snapshot: dict[str, Any],
    *,
    issue_key: str,
    text: str,
    actor_name: str,
    commented_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot)
    target = issue_key.upper()
    for section_name in ("sections",):
        for section in updated.get(section_name) or []:
            for issue in section.get("issues") or []:
                if str(issue.get("key") or "").upper() != target:
                    continue
                issue["last_comment"] = text
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = commented_at
    for section in ("plan_issues", "unplan_issues"):
        for issue in updated.get(section) or []:
            if str(issue.get("key") or "").upper() != target:
                continue
            issue["last_comment"] = text
            issue["last_comment_author"] = actor_name
            issue["last_comment_at"] = commented_at
    sections = updated.get("sections") or []
    if sections:
        updated["report"] = compute_scope_report_from_sections(sections)
    else:
        updated["report"] = compute_scope_report(
            updated.get("plan_issues") or [],
            updated.get("unplan_issues") or [],
        )
    return updated


def _scope_report_comments(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = snapshot.get("report_comments")
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def _scope_snapshot_has_report_issue(snapshot: dict[str, Any], issue_key: str) -> bool:
    if _scope_snapshot_has_issue(snapshot, issue_key):
        return True
    target = issue_key.upper()
    report = snapshot.get("report") or {}
    report_sections: list[dict[str, Any]] = list(report.get("sections") or [])
    for legacy in (report.get("plan"), report.get("unplan")):
        if isinstance(legacy, dict):
            report_sections.append(legacy)
    for section in report_sections:
        for column in ("in_work", "in_test", "done"):
            for issue in section.get(column) or []:
                if str(issue.get("key") or "").upper() == target:
                    return True
    release_context = snapshot.get("release_context") or {}
    buckets: list[dict[str, Any]] = []
    for slot in ("current", "previous", "next", "custom"):
        bucket = release_context.get(slot)
        if isinstance(bucket, dict):
            buckets.append(bucket)
    buckets.extend([bucket for bucket in release_context.get("releases") or [] if isinstance(bucket, dict)])
    for bucket in buckets:
        for column in ("in_work", "in_test", "done", "issues", "open_questions"):
            for issue in bucket.get(column) or []:
                if str(issue.get("key") or "").upper() == target:
                    return True
    return False


def _scope_snapshot_with_report_comment(
    snapshot: dict[str, Any],
    *,
    issue_key: str,
    text: str,
    actor_name: str,
    commented_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    comments = _scope_report_comments(updated)
    target = issue_key.upper()
    canonical_key = next((key for key in comments if key.upper() == target), issue_key.upper())
    cleaned = text.strip()
    if cleaned:
        comments[canonical_key] = {"text": cleaned, "by": actor_name, "at": commented_at}
    else:
        comments.pop(canonical_key, None)
        for key in list(comments):
            if key.upper() == target:
                comments.pop(key, None)
    updated["report_comments"] = comments
    return updated


def _grooming_jira_comment(queue_label: str, comment: str, *, moved_from: Optional[int] = None, moved_to: Optional[int] = None) -> str:
    prefix = f"[Scope grooming — {queue_label}]"
    if moved_from is not None and moved_to is not None:
        return f"{prefix} Позиция {moved_from + 1} → {moved_to + 1}: {comment}"
    return f"{prefix} {comment}"


def _scope_question_id(value: str) -> str:
    return value.strip()


def _scope_snapshot_with_manual_question(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    manual = list(updated.get("manual_questions") or [])
    manual.append(
        {
            "id": f"manual-{secrets.token_hex(6)}",
            "summary": text,
            "created_by": actor_name,
            "created_at": created_at,
        }
    )
    updated["manual_questions"] = manual
    return updated


def _scope_snapshot_with_top_item(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    top_items = list(updated.get("top_items") or [])
    if len(top_items) >= 10:
        raise HTTPException(status_code=400, detail="Можно добавить не более 10 пунктов")
    top_items.append(
        {
            "id": f"top-{secrets.token_hex(6)}",
            "text": text,
            "created_by": actor_name,
            "created_at": created_at,
        }
    )
    updated["top_items"] = top_items
    return updated


def _scope_snapshot_without_top_item(snapshot: dict[str, Any], *, item_id: str) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    target = item_id.strip()
    top_items = [
        item
        for item in (updated.get("top_items") or [])
        if str(item.get("id") or "") != target
    ]
    if len(top_items) == len(updated.get("top_items") or []):
        raise HTTPException(status_code=404, detail="Top item not found in scope board snapshot")
    updated["top_items"] = top_items
    return updated


def _scope_snapshot_with_todo_item(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    todo_items = list(updated.get("todo_items") or [])
    if len(todo_items) >= 100:
        raise HTTPException(status_code=400, detail="Можно добавить не более 100 todo")
    todo_items.insert(
        0,
        {
            "id": f"todo-{secrets.token_hex(6)}",
            "text": text,
            "done": False,
            "created_by": actor_name,
            "created_at": created_at,
        },
    )
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_with_todo_done(
    snapshot: dict[str, Any],
    *,
    item_id: str,
    done: bool,
    actor_name: str,
    changed_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    target = item_id.strip()
    changed = False
    todo_items: list[dict[str, Any]] = []
    for item in updated.get("todo_items") or []:
        if str(item.get("id") or "") != target:
            todo_items.append(item)
            continue
        next_item = {**item, "done": done}
        if done:
            next_item["done_by"] = actor_name
            next_item["done_at"] = changed_at
        else:
            next_item.pop("done_by", None)
            next_item.pop("done_at", None)
        todo_items.append(next_item)
        changed = True
    if not changed:
        raise HTTPException(status_code=404, detail="Todo item not found in scope board snapshot")
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_without_todo_item(snapshot: dict[str, Any], *, item_id: str) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    target = item_id.strip()
    todo_items = [
        item
        for item in (updated.get("todo_items") or [])
        if str(item.get("id") or "") != target
    ]
    if len(todo_items) == len(updated.get("todo_items") or []):
        raise HTTPException(status_code=404, detail="Todo item not found in scope board snapshot")
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_with_resolved_question(
    snapshot: dict[str, Any],
    *,
    question_id: str,
    comment: str,
    actor_name: str,
    resolved_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot or {})
    target = _scope_question_id(question_id)
    manual = []
    resolved_source: Optional[dict[str, Any]] = None

    for question in updated.get("manual_questions") or []:
        if str(question.get("id") or "") == target:
            resolved_source = {**question, "kind": "manual"}
            continue
        manual.append(question)

    if resolved_source is None:
        for snapshot_section in updated.get("sections") or []:
            for issue in snapshot_section.get("issues") or []:
                if str(issue.get("key") or "").upper() != target.upper():
                    continue
                issue["last_comment"] = comment
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = resolved_at
                resolved_source = {
                    "id": issue.get("key"),
                    "key": issue.get("key"),
                    "summary": issue.get("summary"),
                    "url": issue.get("url"),
                    "status": issue.get("status"),
                    "priority": issue.get("priority"),
                    "assignee": issue.get("assignee"),
                    "bucket": snapshot_section.get("id"),
                    "section_id": snapshot_section.get("id"),
                    "section_name": snapshot_section.get("name"),
                    "section_kind": snapshot_section.get("kind"),
                    "kind": "jira",
                }
                break
            if resolved_source is not None:
                break

    if resolved_source is None:
        for section in ("plan_issues", "unplan_issues"):
            for issue in updated.get(section) or []:
                if str(issue.get("key") or "").upper() != target.upper():
                    continue
                issue["last_comment"] = comment
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = resolved_at
                resolved_source = {
                    "id": issue.get("key"),
                    "key": issue.get("key"),
                    "summary": issue.get("summary"),
                    "url": issue.get("url"),
                    "status": issue.get("status"),
                    "priority": issue.get("priority"),
                    "assignee": issue.get("assignee"),
                    "bucket": "plan" if section == "plan_issues" else "unplan",
                    "kind": "jira",
                }

    if resolved_source is None:
        raise HTTPException(status_code=404, detail="Question not found in scope board snapshot")

    updated["manual_questions"] = manual
    resolved = list(updated.get("resolved_questions") or [])
    resolved.append(
        {
            **resolved_source,
            "id": target,
            "comment": comment,
            "resolved_by": actor_name,
            "resolved_at": resolved_at,
        }
    )
    updated["resolved_questions"] = resolved[-100:]
    sections = updated.get("sections") or []
    if sections:
        updated["report"] = compute_scope_report_from_sections(sections)
    else:
        updated["report"] = compute_scope_report(
            updated.get("plan_issues") or [],
            updated.get("unplan_issues") or [],
        )
    return updated


# ---------------------------------------------------------------------------
# Monthly scope boards (plan / unplan buffer dashboard).
# ---------------------------------------------------------------------------


@cms_router.get("/cms/scope-boards")
async def cms_list_scope_boards(
    request: Request,
    team_id: Optional[int] = None,
    sort: Optional[str] = Query(default=None, pattern="^(team_then_updated)?$"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    items = await _get_cms_store(request).list_scope_boards(
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )
    return {"items": items}


@cms_router.post("/cms/scope-boards")
async def cms_create_scope_board(
    body: ScopeBoardCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    if resolved_team_id is None:
        raise HTTPException(
            status_code=400,
            detail="Выберите команду — отчёт без команды видят все админы",
        )
    store = _get_cms_store(request)
    report_type = await _resolve_scope_report_type(store, resolved_team_id)
    payload = _scope_board_payload_from_request(body, report_type=report_type)
    board = await store.create_scope_board(
        created_by=actor.id,
        team_id=resolved_team_id,
        **payload,
    )
    await _audit(
        request,
        "cms.scope_board.create",
        actor.username,
        "ok",
        {"board_id": board["id"], "name": board["name"]},
    )
    return board


@cms_router.get("/cms/scope-boards/{board_id}")
async def cms_get_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    board = await _get_cms_store(request).get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)
    return board


@cms_router.patch("/cms/scope-boards/{board_id}/layout")
@cms_router.patch("/cms/scope-boards/{board_id}/layout/")
async def cms_update_scope_board_layout(
    board_id: int,
    body: ScopeBoardLayoutRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    normalized = _normalize_scope_layout_order(body.layout_order)
    try:
        board = await _get_cms_store(request).update_scope_board_layout(board_id, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.layout_update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.patch("/cms/scope-boards/{board_id}/release-comments")
@cms_router.patch("/cms/scope-boards/{board_id}/release-comments/")
async def cms_update_scope_board_release_comments(
    board_id: int,
    body: ScopeBoardReleaseCommentsRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    board = await _get_cms_store(request).update_scope_board_release_comments(
        board_id,
        release_comment=body.release_comment,
        previous_release_comment=body.previous_release_comment,
        next_release_comment=body.next_release_comment,
        custom_release_comment=body.custom_release_comment,
    )
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.release_comments_update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.patch("/cms/scope-boards/{board_id}")
async def cms_update_scope_board(
    board_id: int,
    body: ScopeBoardUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)
    report_type = str(existing.get("report_type") or "monthly")
    payload = _scope_board_payload_from_request(body, report_type=report_type)
    board = await _get_cms_store(request).update_scope_board(
        board_id,
        **payload,
    )
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    snapshot = board.get("snapshot")
    if snapshot:
        refreshed_snapshot = refresh_scope_snapshot_metrics(
            snapshot,
            capacity_sp=board["capacity_sp"],
            month=board["month"],
            workload_mode=board.get("workload_mode"),
            capacity_sp_dev=board.get("capacity_sp_dev"),
            capacity_sp_test=board.get("capacity_sp_test"),
        )
        board = await _get_cms_store(request).save_scope_board_snapshot(board_id, refreshed_snapshot)
        if not board:
            raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/refresh")
async def cms_refresh_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_refresh:actor:{actor.username}",
        limit=int(os.getenv("SCOPE_REFRESH_RATE_MAX", "30")),
        window_seconds=int(os.getenv("SCOPE_REFRESH_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Слишком много обновлений из Jira — попробуйте позже",
    )
    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_refresh:board:{board_id}",
        limit=int(os.getenv("SCOPE_REFRESH_BOARD_RATE_MAX", "12")),
        window_seconds=int(os.getenv("SCOPE_REFRESH_BOARD_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Этот отчёт уже часто обновляли — подождите немного",
    )

    from app.adapters.jira_service_client import JiraServiceHttpClient

    previous_snapshot = existing.get("snapshot") or {}
    previous_issue_count = _count_snapshot_issues(previous_snapshot)
    scope_sections = _scope_sections_from_board(existing)
    refreshed_at = datetime.now(timezone.utc).isoformat()
    fetch_outcomes: list[_ScopeJqlFetchResult] = []
    release_outcomes: dict[str, _ScopeJqlFetchResult] = {}
    release_version_meta_map: dict[str, dict[str, Any]] = {}

    client = JiraServiceHttpClient()
    try:
        fetched_sections, section_outcomes = await _fetch_scope_sections(
            scope_sections,
            client,
            force_refresh=True,
        )
        fetch_outcomes.extend(section_outcomes)

        todo_outcome = _ScopeJqlFetchResult(jql="", issues=[])
        test_outcome = _ScopeJqlFetchResult(jql="", issues=[])
        queue_tasks: list[Any] = []
        if (existing.get("todo_jql") or "").strip():
            queue_tasks.append(
                _fetch_scope_issues(
                    existing.get("todo_jql") or "",
                    client,
                    force_refresh=True,
                    milestone_status_targets=priority_queue_milestone_targets("todo"),
                    enrich_changelog=True,
                )
            )
        if (existing.get("test_jql") or "").strip():
            queue_tasks.append(
                _fetch_scope_issues(
                    existing.get("test_jql") or "",
                    client,
                    force_refresh=True,
                    milestone_status_targets=priority_queue_milestone_targets("test"),
                    enrich_changelog=True,
                )
            )
        release_outcomes = {}
        release_queries = _release_queries_from_board(existing) if existing.get("report_type") == "release" else []
        if existing.get("report_type") == "release":
            release_tasks = {
                query["id"]: _fetch_scope_issues(query["jql"], client, force_refresh=True, enrich_changelog=True)
                for query in release_queries
                if (query.get("jql") or "").strip()
            }
            if release_tasks:
                release_results = await asyncio.gather(*release_tasks.values())
                for (key, _), outcome in zip(release_tasks.items(), release_results, strict=True):
                    release_outcomes[key] = outcome
                    fetch_outcomes.append(outcome)
        if existing.get("report_type") == "release":
            version_jql_by_slot = {
                "current": _primary_release_jql(scope_sections),
            }
            version_jql_by_slot.update({query["id"]: query["jql"] for query in release_queries})
            issues_by_slot = {
                "current": (fetched_sections[0].get("issues") if fetched_sections else []) or [],
            }
            issues_by_slot.update({slot: outcome.issues for slot, outcome in release_outcomes.items()})
            release_version_meta_map = await _fetch_release_version_meta_map(
                client,
                version_jql_by_slot,
                issues_by_slot,
            )
        if queue_tasks:
            queue_results = await asyncio.gather(*queue_tasks)
            index = 0
            if (existing.get("todo_jql") or "").strip():
                todo_outcome = queue_results[index]
                fetch_outcomes.append(todo_outcome)
                index += 1
            if (existing.get("test_jql") or "").strip():
                test_outcome = queue_results[index]
                fetch_outcomes.append(test_outcome)
    finally:
        await client.close()

    configured_outcomes = [outcome for outcome in fetch_outcomes if outcome.jql]
    if configured_outcomes and all(outcome.failed for outcome in configured_outcomes):
        raise HTTPException(
            status_code=503,
            detail="Jira недоступна — snapshot не изменён",
        )
    if previous_issue_count > 0 and any(outcome.failed for outcome in configured_outcomes):
        raise HTTPException(
            status_code=503,
            detail="Часть JQL не загрузилась из Jira — snapshot не изменён",
        )

    todo_issues = todo_outcome.issues
    test_issues = test_outcome.issues

    for section in fetched_sections:
        for issue in section.get("issues") or []:
            issue["scope_creep"] = is_scope_creep(str(issue.get("created") or "") or None, existing["month"])
    metrics = compute_scope_metrics_from_sections(
        existing["capacity_sp"],
        fetched_sections,
        existing["month"],
        workload_mode=existing.get("workload_mode"),
        capacity_sp_dev=existing.get("capacity_sp_dev"),
        capacity_sp_test=existing.get("capacity_sp_test"),
    )
    snapshot = build_scope_snapshot(
        sections=fetched_sections,
        metrics=metrics,
        refreshed_at=refreshed_at,
        previous_snapshot=previous_snapshot,
    )
    snapshot["jira_role_fields_configured"] = merge_jira_role_fields_configured(
        snapshot.get("jira_role_fields_configured"),
        *[outcome.jira_role_fields_configured for outcome in fetch_outcomes],
    )
    snapshot["manual_questions"] = previous_snapshot.get("manual_questions") or []
    snapshot["resolved_questions"] = previous_snapshot.get("resolved_questions") or []
    snapshot["top_items"] = previous_snapshot.get("top_items") or []
    snapshot["todo_items"] = previous_snapshot.get("todo_items") or []
    snapshot["report_comments"] = previous_snapshot.get("report_comments") or {}
    snapshot["jira_fetch_warnings"] = _scope_fetch_warnings(fetch_outcomes)
    prev_queues = previous_snapshot.get("priority_queues") or {}
    snapshot["priority_queues"] = {
        "todo": merge_priority_queue(
            todo_issues,
            prev_queues.get("todo"),
            queue_label=priority_queue_label("todo"),
            refreshed_at=refreshed_at,
        ),
        "test": merge_priority_queue(
            test_issues,
            prev_queues.get("test"),
            queue_label=priority_queue_label("test"),
            refreshed_at=refreshed_at,
        ),
    }
    if existing.get("report_type") == "release":
        current_issues = (fetched_sections[0].get("issues") if fetched_sections else []) or []
        snapshot["release_context"] = build_release_context(
            current_jql=_primary_release_jql(scope_sections),
            current_issues=current_issues,
            previous_jql=existing.get("previous_release_jql") or "",
            previous_issues=(release_outcomes.get("previous").issues if release_outcomes.get("previous") else []),
            next_jql=existing.get("next_release_jql") or "",
            next_issues=(release_outcomes.get("next").issues if release_outcomes.get("next") else []),
            custom_name=existing.get("custom_release_name") or "",
            custom_jql=existing.get("custom_release_jql") or "",
            custom_issues=(release_outcomes.get("custom").issues if release_outcomes.get("custom") else []),
            release_queries=release_queries,
            release_issues_by_slot={slot: outcome.issues for slot, outcome in release_outcomes.items()},
            version_meta_by_slot=release_version_meta_map,
        )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.refresh",
        actor.username,
        "ok",
        {"board_id": board_id, "intake_status": metrics["intake_status"]},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/issues/{issue_key}/comment")
async def cms_add_scope_issue_comment(
    board_id: int,
    issue_key: str,
    body: ScopeIssueCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_issue(snapshot, issue_key):
        raise HTTPException(status_code=404, detail="Issue not found in scope board snapshot")

    cleaned_text = body.text.strip()
    if not cleaned_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    commented_at = datetime.now(timezone.utc).isoformat()
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_comment(
        snapshot,
        issue_key=issue_key,
        text=cleaned_text,
        actor_name=actor_name,
        commented_at=commented_at,
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    try:
        await _post_jira_issue_comment(issue_key, cleaned_text)
    except Exception as exc:
        logger.warning("scope issue comment saved locally but Jira failed key=%s error=%s", issue_key, exc)
        raise HTTPException(
            status_code=502,
            detail="Snapshot сохранён, но комментарий в Jira не отправлен",
        ) from exc
    await _audit(
        request,
        "cms.scope_board.issue_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "issue_key": issue_key},
    )
    return board


@cms_router.put("/cms/scope-boards/{board_id}/issues/{issue_key}/report-comment")
async def cms_update_scope_report_comment(
    board_id: int,
    issue_key: str,
    body: ScopeReportCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_report_issue(snapshot, issue_key):
        raise HTTPException(status_code=404, detail="Issue not found in scope board report")

    commented_at = datetime.now(timezone.utc).isoformat()
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_report_comment(
        snapshot,
        issue_key=issue_key,
        text=body.text,
        actor_name=actor_name,
        commented_at=commented_at,
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.report_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "issue_key": issue_key},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/questions")
async def cms_add_scope_manual_question(
    board_id: int,
    body: ScopeManualQuestionRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_manual_question(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.question_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/top-items")
async def cms_add_scope_top_item(
    board_id: int,
    body: ScopeTopItemRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_top_item(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.top_item_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.delete("/cms/scope-boards/{board_id}/top-items/{item_id}")
async def cms_delete_scope_top_item(
    board_id: int,
    item_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    next_snapshot = _scope_snapshot_without_top_item(snapshot, item_id=item_id)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.top_item_delete",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/todo-items")
async def cms_add_scope_todo_item(
    board_id: int,
    body: ScopeTodoItemRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_todo_item(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@cms_router.patch("/cms/scope-boards/{board_id}/todo-items/{item_id}")
async def cms_update_scope_todo_item(
    board_id: int,
    item_id: str,
    body: ScopeTodoItemUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_todo_done(
        snapshot,
        item_id=item_id,
        done=body.done,
        actor_name=actor_name,
        changed_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_update",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id, "done": body.done},
    )
    return board


@cms_router.delete("/cms/scope-boards/{board_id}/todo-items/{item_id}")
async def cms_delete_scope_todo_item(
    board_id: int,
    item_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    next_snapshot = _scope_snapshot_without_todo_item(snapshot, item_id=item_id)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_delete",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/questions/{question_id}/resolve")
async def cms_resolve_scope_question(
    board_id: int,
    question_id: str,
    body: ScopeResolveQuestionRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    cleaned_comment = body.comment.strip()
    if not cleaned_comment:
        raise HTTPException(status_code=400, detail="Comment text is required")

    if _scope_snapshot_has_issue(snapshot, question_id):
        await _post_jira_issue_comment(question_id, cleaned_comment)

    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_resolved_question(
        snapshot,
        question_id=question_id,
        comment=cleaned_comment,
        actor_name=actor_name,
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.question_resolve",
        actor.username,
        "ok",
        {"board_id": board_id, "question_id": question_id},
    )
    return board


def _parse_priority_queue_kind(raw: str) -> str:
    kind = raw.strip().lower()
    if kind not in {"todo", "test"}:
        raise HTTPException(status_code=400, detail="Queue must be todo or test")
    return kind


@cms_router.post("/cms/scope-boards/{board_id}/queues/{queue_kind}/reorder")
async def cms_reorder_scope_priority_queue(
    board_id: int,
    queue_kind: str,
    body: ScopeQueueReorderRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    queues = dict(snapshot.get("priority_queues") or {})
    current_queue = queues.get(kind) or {"order": [], "issues": [], "history": []}
    cleaned_comment = body.comment.strip()

    actor_name = actor.display_name or actor.username
    changed_at = datetime.now(timezone.utc).isoformat()
    queue_label = priority_queue_label(kind)  # type: ignore[arg-type]
    try:
        next_queue = apply_priority_queue_reorder(
            current_queue,
            order=body.order,
            comment=cleaned_comment,
            actor_name=actor_name,
            changed_at=changed_at,
            queue_label=queue_label,
            moved_key=body.moved_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    moved_key = None
    moved_from = None
    moved_to = None
    for entry in next_queue.get("history") or []:
        if entry.get("type") == "reorder" and entry.get("at") == changed_at:
            moved_key = entry.get("issue_key")
            moved_from = entry.get("from_index")
            moved_to = entry.get("to_index")
            break
    jira_comment = None
    if moved_key and cleaned_comment:
        jira_comment = _grooming_jira_comment(
            queue_label,
            cleaned_comment,
            moved_from=moved_from if isinstance(moved_from, int) else None,
            moved_to=moved_to if isinstance(moved_to, int) else None,
        )

    next_snapshot = copy.deepcopy(snapshot)
    next_queues = dict(next_snapshot.get("priority_queues") or {})
    next_queues[kind] = next_queue
    next_snapshot["priority_queues"] = next_queues
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _sync_queue_significance_to_jira(list(next_queue.get("order") or []))
    if moved_key and jira_comment:
        try:
            await _post_jira_issue_comment(str(moved_key), jira_comment)
        except Exception as exc:
            logger.warning(
                "scope queue reorder saved locally but Jira failed key=%s error=%s",
                moved_key,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Порядок сохранён, но комментарий в Jira не отправлен",
            ) from exc
    await _audit(
        request,
        "cms.scope_board.queue_reorder",
        actor.username,
        "ok",
        {"board_id": board_id, "queue": kind, "issue_key": moved_key},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/queues/{queue_kind}/issues/{issue_key}/comment")
async def cms_add_scope_queue_issue_comment(
    board_id: int,
    queue_kind: str,
    issue_key: str,
    body: ScopeIssueCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_queue_issue(snapshot, issue_key, kind):
        raise HTTPException(status_code=404, detail="Issue not found in queue")

    cleaned_text = body.text.strip()
    if not cleaned_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    queue_label = priority_queue_label(kind)  # type: ignore[arg-type]
    actor_name = actor.display_name or actor.username
    changed_at = datetime.now(timezone.utc).isoformat()
    queues = dict(snapshot.get("priority_queues") or {})
    current_queue = queues.get(kind) or {"order": [], "issues": [], "history": []}
    try:
        next_queue = apply_priority_queue_comment(
            current_queue,
            issue_key=issue_key,
            comment=cleaned_text,
            actor_name=actor_name,
            changed_at=changed_at,
            queue_label=queue_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    next_snapshot = copy.deepcopy(snapshot)
    next_queues = dict(next_snapshot.get("priority_queues") or {})
    next_queues[kind] = next_queue
    next_snapshot["priority_queues"] = next_queues
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    try:
        await _post_jira_issue_comment(issue_key, _grooming_jira_comment(queue_label, cleaned_text))
    except Exception as exc:
        logger.warning("scope queue comment saved locally but Jira failed key=%s error=%s", issue_key, exc)
        raise HTTPException(
            status_code=502,
            detail="Комментарий сохранён в отчёте, но не отправлен в Jira",
        ) from exc
    await _audit(
        request,
        "cms.scope_board.queue_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "queue": kind, "issue_key": issue_key},
    )
    return board


@cms_router.put("/cms/scope-boards/{board_id}/queues/{queue_kind}/issues/{issue_key}/due-date")
async def cms_update_scope_queue_issue_due_date(
    board_id: int,
    queue_kind: str,
    issue_key: str,
    body: ScopeIssueDueDateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_queue_issue(snapshot, issue_key, kind):
        raise HTTPException(status_code=404, detail="Issue not found in queue")

    try:
        datetime.strptime(body.due_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid due date") from exc

    try:
        saved = await _put_jira_issue_due_date(issue_key, body.due_date)
    except Exception as exc:
        logger.warning("scope queue due date Jira update failed key=%s error=%s", issue_key, exc)
        raise HTTPException(status_code=502, detail="Срок исполнения не сохранён в Jira") from exc
    if not saved:
        raise HTTPException(status_code=502, detail="Срок исполнения не сохранён в Jira")

    next_snapshot = _scope_snapshot_with_due_date(snapshot, issue_key=issue_key, due_date=body.due_date)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.queue_due_date_update",
        actor.username,
        "ok",
        {"board_id": board_id, "queue": kind, "issue_key": issue_key, "due_date": body.due_date},
    )
    return board


@cms_router.post("/cms/scope-boards/{board_id}/analyze")
async def cms_analyze_scope_board(
    board_id: int,
    request: Request,
    async_mode: bool = Query(False, alias="async"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    board = await store.get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)
    snapshot = board.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=400, detail="Нет snapshot — сначала обновите board из Jira")

    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_ai:actor:{actor.username}",
        limit=int(os.getenv("SCOPE_AI_RATE_MAX", "20")),
        window_seconds=int(os.getenv("SCOPE_AI_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Слишком много AI-запросов, попробуйте позже",
    )

    from services.voting_service.ai_job_runners import run_scope_ai_job
    from services.voting_service.ai_jobs import find_cached_scope_summary, get_job, get_or_create_job, job_public_view, spawn_ai_job
    from services.voting_service.scope_ai_llm import LlmScopeError, generate_scope_analysis

    snapshot_refreshed_at = snapshot.get("refreshed_at") if isinstance(snapshot, dict) else None
    cached = find_cached_scope_summary(board, snapshot_refreshed_at)
    if cached:
        from services.voting_service.ai_job_runners import spawn_scope_ai_jira_export

        spawn_scope_ai_jira_export(
            request.app,
            board_id=board_id,
            board=board,
            summary=dict(cached),
            actor_username=actor.username,
        )
        await _audit(
            request,
            "cms.scope_board.analyze",
            actor.username,
            "ok",
            {"board_id": board_id, "health": cached.get("health"), "cached": True},
        )
        return {"ai_summary": cached, "board": board, "cached": True}

    if async_mode:
        redis = await _get_redis(request)
        job_id, is_new = await get_or_create_job(
            redis,
            kind="scope",
            resource_key=f"board:{board_id}",
            actor=actor.username,
        )
        if is_new:
            spawn_ai_job(run_scope_ai_job(request.app, job_id=job_id, board_id=board_id, actor_username=actor.username))
        job_record = await get_job(redis, job_id)
        return job_public_view(job_record or {"job_id": job_id, "status": "queued", "phase": "queued", "message": "В очереди"})

    http_session = getattr(request.app.state, "http_session", None)
    if http_session is None:
        raise HTTPException(status_code=503, detail="AI is not configured")
    try:
        summary = await generate_scope_analysis(http_session, board)
    except LlmScopeError as exc:
        await _audit(
            request,
            "cms.scope_board.analyze",
            actor.username,
            "error",
            {"board_id": board_id, "error": exc.message},
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    updated = await store.save_scope_board_ai_summary(
        board_id,
        summary,
        snapshot_refreshed_at=snapshot_refreshed_at,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Scope board not found")
    from services.voting_service.ai_job_runners import spawn_scope_ai_jira_export

    spawn_scope_ai_jira_export(
        request.app,
        board_id=board_id,
        board=updated,
        summary=dict(summary),
        actor_username=actor.username,
    )
    await _audit(
        request,
        "cms.scope_board.analyze",
        actor.username,
        "ok",
        {"board_id": board_id, "health": summary.get("health")},
    )
    return {"ai_summary": summary, "board": updated}


@cms_router.get("/cms/scope-boards/{board_id}/analyze/jobs/{job_id}")
async def cms_scope_analyze_job_status(
    board_id: int,
    job_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    board = await _get_cms_store(request).get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)

    from services.voting_service.ai_jobs import get_job_for_poll, job_public_view

    redis = await _get_redis(request)
    job = await get_job_for_poll(redis, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    if job.get("kind") != "scope" or job.get("resource_key") != f"board:{board_id}":
        raise HTTPException(status_code=404, detail="AI job not found")
    return job_public_view(job)


@cms_router.delete("/cms/scope-boards/{board_id}")
async def cms_delete_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)
    deleted = await _get_cms_store(request).delete_scope_board(board_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.delete",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return {"ok": True, "id": board_id}
