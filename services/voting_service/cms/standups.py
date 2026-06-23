"""CMS daily standups and team roster endpoints."""

from __future__ import annotations

import os
import re
import uuid
from datetime import date
from typing import Any, Literal, Optional
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from services.voting_service.cms_rbac import PERM_STANDUPS_MANAGE, PERM_STANDUPS_VIEW
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT
from services.voting_service.cms_team_access import assert_record_access, resolve_create_team_id, team_scope
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    require_permission,
)

router = APIRouter()

_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

StandupRole = Literal["front", "back", "qa", "other"]
StandupTrack = Literal["yesterday", "today", "blocker"]
StandupItemStatus = Literal["in_progress", "done", "blocked", "waiting"]
StandupStatus = Literal["draft", "published"]


class StandupRosterMember(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: StandupRole = "other"
    active: bool = True


class StandupWorkItem(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    task_title: str = Field(default="", max_length=300)
    jira_key: str = Field(default="", max_length=40)
    track: StandupTrack
    due_date: Optional[str] = Field(default=None, max_length=10)
    status: Optional[StandupItemStatus] = None
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def normalize_work_item(self) -> "StandupWorkItem":
        jira_key = self.jira_key.strip().upper()
        task_title = self.task_title.strip()
        if jira_key:
            self.jira_key = jira_key
        if not task_title and jira_key:
            self.task_title = jira_key
        if not task_title and not jira_key:
            raise ValueError("task_title or jira_key is required")
        return self


class StandupParticipant(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: StandupRole = "other"
    present: bool = True
    items: list[StandupWorkItem] = Field(default_factory=list, max_length=50)


class StandupPayload(BaseModel):
    facilitator: str = Field(default="", max_length=120)
    duration_minutes: Optional[int] = Field(default=None, ge=0, le=600)
    notes: str = Field(default="", max_length=4000)
    participants: list[StandupParticipant] = Field(default_factory=list, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def strip_empty_items(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        participants = data.get("participants")
        if not isinstance(participants, list):
            return data
        normalized_participants = []
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            items = participant.get("items")
            kept_items = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    has_title = bool(str(item.get("task_title") or "").strip())
                    has_jira = bool(str(item.get("jira_key") or "").strip())
                    if has_title or has_jira:
                        kept_items.append(item)
            normalized_participants.append({**participant, "items": kept_items})
        return {**data, "participants": normalized_participants}

    @field_validator("participants")
    @classmethod
    def validate_items(cls, participants: list[StandupParticipant]) -> list[StandupParticipant]:
        for participant in participants:
            for item in participant.items:
                if item.track == "blocker" and not item.comment.strip():
                    raise ValueError(f"Blocker for {participant.name} requires a comment")
        return participants


class StandupRosterUpdateRequest(BaseModel):
    members: list[StandupRosterMember] = Field(default_factory=list, max_length=100)


class StandupCreateRequest(BaseModel):
    team_id: Optional[int] = None
    meeting_date: str = Field(min_length=10, max_length=10)
    payload: Optional[StandupPayload] = None


class StandupUpdateRequest(BaseModel):
    payload: StandupPayload
    status: Optional[StandupStatus] = None


def _parse_meeting_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid meeting_date, expected YYYY-MM-DD") from exc


def _payload_from_roster(members: list[dict]) -> dict:
    participants = []
    for member in members:
        if not member.get("active", True):
            continue
        participants.append(
            {
                "id": member.get("id") or str(uuid.uuid4()),
                "name": str(member.get("name") or "").strip(),
                "role": member.get("role") or "other",
                "present": True,
                "items": [],
            }
        )
    return {"facilitator": "", "duration_minutes": None, "notes": "", "participants": participants}


def _can_manage(actor: CmsPrincipal) -> bool:
    return actor.is_superuser or PERM_STANDUPS_MANAGE in actor.permissions


def _normalize_jira_key(value: str) -> str:
    key = (value or "").strip().upper()
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        key = key.replace(dash, "-")
    return key if _JIRA_KEY_RE.fullmatch(key) else ""


async def _fetch_jira_issue_summary(http_session: aiohttp.ClientSession, issue_key: str) -> Optional[dict[str, Any]]:
    key = _normalize_jira_key(issue_key)
    if not key:
        return None
    base_url = os.getenv("JIRA_SERVICE_URL", "http://jira-service:8001").rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(os.getenv("JIRA_LOOKUP_TIMEOUT_SECONDS", "10")))
    url = f"{base_url}/api/v1/issue/{quote(key)}"
    try:
        async with http_session.get(url, timeout=timeout) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                body_snippet = (await response.text())[:200]
                raise HTTPException(status_code=502, detail=f"Jira lookup failed: {body_snippet}")
            data = await response.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaced as 502 for CMS UI.
        raise HTTPException(status_code=502, detail=f"Jira lookup unavailable: {exc}") from exc
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or key).strip() or key
    return {
        "key": str(data.get("key") or key).strip().upper() or key,
        "summary": summary,
        "url": str(data.get("url") or "").strip(),
    }


class StandupLocalDueHintResponse(BaseModel):
    jira_key: str
    due_date: Optional[str] = None
    meeting_date: Optional[str] = None


class StandupJiraLookupResponse(BaseModel):
    key: str
    summary: str
    url: str = ""


def _merge_roster_into_payload(payload: dict, members: list[dict]) -> dict:
    """Add active roster members missing from ``payload.participants`` (by id)."""
    participants = list(payload.get("participants") or [])
    existing_ids = {str(participant.get("id") or "") for participant in participants if isinstance(participant, dict)}
    for member in members:
        if not member.get("active", True):
            continue
        member_id = str(member.get("id") or "").strip()
        if not member_id or member_id in existing_ids:
            continue
        participants.append(
            {
                "id": member_id,
                "name": str(member.get("name") or "").strip(),
                "role": member.get("role") or "other",
                "present": True,
                "items": [],
            }
        )
        existing_ids.add(member_id)
    return {**payload, "participants": participants}


def _assert_standup_visible(actor: CmsPrincipal, standup: dict) -> None:
    assert_record_access(actor, standup)
    if standup.get("status") != "published" and not _can_manage(actor):
        raise HTTPException(status_code=404, detail="Standup not found")


@router.get("/cms/standup-rosters/{team_id}")
async def cms_get_standup_roster(
    team_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_VIEW)),
) -> dict:
    assert_record_access(actor, {"team_id": team_id})
    roster = await _get_cms_store(request).get_standup_roster(team_id)
    if roster is None:
        return {"team_id": team_id, "members": [], "updated_by": None, "created_at": None, "updated_at": None}
    return roster


@router.put("/cms/standup-rosters/{team_id}")
async def cms_upsert_standup_roster(
    team_id: int,
    body: StandupRosterUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    assert_record_access(actor, {"team_id": team_id})
    roster = await _get_cms_store(request).upsert_standup_roster(
        team_id,
        [member.model_dump() for member in body.members],
        actor.id,
    )
    await _audit(
        request,
        "cms.standup_roster.update",
        actor.username,
        "ok",
        {"team_id": team_id, "member_count": len(body.members)},
    )
    return roster


@router.get("/cms/standups")
async def cms_list_standups(
    request: Request,
    team_id: Optional[int] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    sort: Optional[str] = Query(default=None, pattern="^(team_then_date)?$"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        assert_record_access(actor, {"team_id": team_id})
    scope = team_scope(actor)
    return await _get_cms_store(request).list_standups(
        limit=limit,
        cursor=cursor,
        team_id=team_id,
        date_from=_parse_meeting_date(from_date) if from_date else None,
        date_to=_parse_meeting_date(to_date) if to_date else None,
        published_only=not _can_manage(actor),
        sort_team=sort == "team_then_date" and actor.is_superuser,
        **scope,
    )


@router.get("/cms/standups/local-due-hints/{issue_key}", response_model=StandupLocalDueHintResponse)
async def cms_lookup_standup_local_due_hint(
    issue_key: str,
    request: Request,
    team_id: int = Query(...),
    before: Optional[str] = Query(default=None, description="Exclude standups on/after this meeting_date"),
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_VIEW)),
) -> StandupLocalDueHintResponse:
    assert_record_access(actor, {"team_id": team_id})
    key = _normalize_jira_key(issue_key)
    if not key:
        raise HTTPException(status_code=400, detail="Invalid Jira issue key")
    before_date = _parse_meeting_date(before) if before else None
    prior = await _get_cms_store(request).find_last_standup_jira_due_date(
        team_id=team_id,
        jira_key=key,
        before_meeting_date=before_date,
    )
    if not prior:
        return StandupLocalDueHintResponse(jira_key=key)
    return StandupLocalDueHintResponse(
        jira_key=key,
        due_date=prior.get("due_date"),
        meeting_date=prior.get("meeting_date"),
    )


@router.get("/cms/standups/jira-issues/{issue_key}", response_model=StandupJiraLookupResponse)
async def cms_lookup_standup_jira_issue(
    issue_key: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> StandupJiraLookupResponse:
    _ = actor
    http_session = getattr(request.app.state, "http_session", None)
    if http_session is None:
        raise HTTPException(status_code=503, detail="Jira lookup unavailable")
    issue = await _fetch_jira_issue_summary(http_session, issue_key)
    if issue is None:
        raise HTTPException(status_code=404, detail=f"Issue {issue_key} not found")
    return StandupJiraLookupResponse(
        key=str(issue["key"]),
        summary=str(issue["summary"]),
        url=str(issue.get("url") or ""),
    )


@router.post("/cms/standups")
async def cms_create_standup(
    body: StandupCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    meeting_date = _parse_meeting_date(body.meeting_date)
    store = _get_cms_store(request)
    existing = await store.get_standup_for_team_date(resolved_team_id, meeting_date)
    if existing:
        raise HTTPException(status_code=409, detail="Standup already exists for this team and date")
    if body.payload is not None:
        payload = body.payload.model_dump()
    else:
        roster = await store.get_standup_roster(resolved_team_id)
        members = roster["members"] if roster else []
        payload = _payload_from_roster(members)
        if not payload["participants"]:
            raise HTTPException(status_code=400, detail="Team standup roster is empty")
    standup = await store.create_standup(
        team_id=resolved_team_id,
        meeting_date=meeting_date,
        payload=payload,
        created_by=actor.id,
    )
    await _audit(
        request,
        "cms.standup.create",
        actor.username,
        "ok",
        {"standup_id": standup["id"], "team_id": resolved_team_id, "meeting_date": body.meeting_date},
    )
    return standup


@router.get("/cms/standups/{standup_id}")
async def cms_get_standup(
    standup_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_VIEW)),
) -> dict:
    standup = await _get_cms_store(request).get_standup(standup_id)
    if not standup:
        raise HTTPException(status_code=404, detail="Standup not found")
    _assert_standup_visible(actor, standup)
    return standup


@router.patch("/cms/standups/{standup_id}")
async def cms_update_standup(
    standup_id: int,
    body: StandupUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    existing = await _get_cms_store(request).get_standup(standup_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Standup not found")
    assert_record_access(actor, existing)
    status = body.status
    published_by = actor.id if status == "published" else None
    standup = await _get_cms_store(request).update_standup(
        standup_id,
        payload=body.payload.model_dump(),
        status=status,
        published_by=published_by,
    )
    if not standup:
        raise HTTPException(status_code=404, detail="Standup not found")
    await _audit(
        request,
        "cms.standup.update",
        actor.username,
        "ok",
        {"standup_id": standup_id, "status": standup.get("status")},
    )
    return standup


@router.post("/cms/standups/{standup_id}/sync-roster")
async def cms_sync_standup_roster(
    standup_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    store = _get_cms_store(request)
    existing = await store.get_standup(standup_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Standup not found")
    assert_record_access(actor, existing)
    if existing.get("status") == "published":
        raise HTTPException(status_code=409, detail="Cannot sync roster on a published standup")
    team_id = existing.get("team_id")
    if team_id is None:
        raise HTTPException(status_code=400, detail="Standup has no team")
    roster = await store.get_standup_roster(int(team_id))
    members = roster["members"] if roster else []
    merged = _merge_roster_into_payload(existing["payload"], members)
    standup = await store.update_standup(standup_id, payload=merged)
    if not standup:
        raise HTTPException(status_code=404, detail="Standup not found")
    await _audit(
        request,
        "cms.standup.sync_roster",
        actor.username,
        "ok",
        {"standup_id": standup_id, "team_id": team_id},
    )
    return standup


@router.post("/cms/standups/{standup_id}/publish")
async def cms_publish_standup(
    standup_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    existing = await _get_cms_store(request).get_standup(standup_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Standup not found")
    assert_record_access(actor, existing)
    standup = await _get_cms_store(request).publish_standup(standup_id, published_by=actor.id)
    if not standup:
        raise HTTPException(status_code=404, detail="Standup not found")
    await _audit(
        request,
        "cms.standup.publish",
        actor.username,
        "ok",
        {"standup_id": standup_id},
    )
    return standup


@router.delete("/cms/standups/{standup_id}")
async def cms_delete_standup(
    standup_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_STANDUPS_MANAGE)),
) -> dict:
    existing = await _get_cms_store(request).get_standup(standup_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Standup not found")
    assert_record_access(actor, existing)
    deleted = await _get_cms_store(request).delete_standup(standup_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Standup not found")
    await _audit(
        request,
        "cms.standup.delete",
        actor.username,
        "ok",
        {"standup_id": standup_id},
    )
    return {"ok": True, "id": standup_id}
