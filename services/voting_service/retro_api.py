"""Retrospective API — public (participant) + manager (CMS) endpoints.

Mirrors the planning-poker stack:

* Live state lives in Redis (``retro:{id}``) via ``RedisRetroRepository``
  with optimistic locking; mutations fan out over the pub/sub channel
  ``retro_events:{id}`` to every connected WebSocket.
* Participants join anonymously via a public ``web_retro:{token}`` link
  (corporate email + role), exactly like the voting flow.
* Managers configure/facilitate through cookie-authenticated CMS routes
  guarded by ``cms.retro.view`` and audited.

Cards are anonymous to *everyone* (the author is stored server-side only,
for the durable snapshot). Vote anonymity is preserved by broadcasting
only aggregate ``vote_count`` — each client tracks its own dots locally
and reconciles them from its own join/state/vote responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.domain.retro import (
    DEFAULT_SECTION_SECONDS,
    DEFAULT_VOTES_PER_PERSON,
    PHASE_DISCUSSING,
    PHASE_DONE,
    PHASE_VOTING,
    Retrospective,
    RetroActionItem,
    RetroCard,
    RetroError,
    RetroGroup,
    RetroSection,
    RetrospectiveFactory,
)
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    _get_redis,
    require_permission,
)
from services.voting_service.cms_rbac import PERM_RETRO_ANALYZE, PERM_RETRO_MANAGE, PERM_RETRO_VIEW
from services.voting_service.cms_team_access import (
    assert_record_access,
    resolve_create_team_id,
    team_scope,
)
from services.voting_service.participant_identity import (
    stable_user_id_from_email,
    validate_participant_email,
    validate_participant_role,
)
from services.voting_service.rate_limit import client_ip, enforce_rate_limit
from services.voting_service.ws_manager import redis_pubsub_listener

logger = logging.getLogger(__name__)

DEFAULT_RETRO_SECTIONS = (
    ("went_well", "Что прошло хорошо"),
    ("pain_points", "Что мешало"),
    ("improvements", "Что улучшим"),
    ("experiments", "Идеи и эксперименты"),
)

retro_router = APIRouter()

RETRO_TOKEN_TTL = 8 * 3600
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

RETRO_JOIN_RATE_MAX = int(os.getenv("RETRO_JOIN_RATE_MAX", "30"))
RETRO_JOIN_RATE_WINDOW = int(os.getenv("RETRO_JOIN_RATE_WINDOW_SECONDS", "60"))
RETRO_CARD_RATE_MAX = int(os.getenv("RETRO_CARD_RATE_MAX", "60"))
RETRO_CARD_RATE_WINDOW = int(os.getenv("RETRO_CARD_RATE_WINDOW_SECONDS", "60"))
RETRO_VOTE_RATE_MAX = int(os.getenv("RETRO_VOTE_RATE_MAX", "120"))
RETRO_VOTE_RATE_WINDOW = int(os.getenv("RETRO_VOTE_RATE_WINDOW_SECONDS", "60"))
RETRO_INVITE_RATE_MAX = int(os.getenv("RETRO_INVITE_RATE_MAX", "20"))
RETRO_INVITE_RATE_WINDOW = int(os.getenv("RETRO_INVITE_RATE_WINDOW_SECONDS", "3600"))

SECTION_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RetroSectionInput(BaseModel):
    section_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=80)


class RetroConfigInput(BaseModel):
    sections: list[RetroSectionInput] = Field(default_factory=list, max_length=20)
    votes_per_person: int = Field(default=DEFAULT_VOTES_PER_PERSON, ge=1, le=50)
    default_section_seconds: int = Field(default=DEFAULT_SECTION_SECONDS, ge=0, le=7200)


class RetroCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    config: RetroConfigInput = Field(default_factory=RetroConfigInput)
    team_id: Optional[int] = None


class RetroUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    config: RetroConfigInput = Field(default_factory=RetroConfigInput)


class RetroOpenSectionRequest(BaseModel):
    section_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    seconds: Optional[int] = Field(default=None, ge=0, le=7200)


class RetroPhaseRequest(BaseModel):
    target: Literal["voting", "discussing"]


class RetroActionItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    assignee: Optional[str] = Field(default=None, max_length=120)


class RetroJoinRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="backend", max_length=32)


class RetroCardRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=80)
    section_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=1000)


class RetroVoteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=80)
    card_id: Optional[str] = Field(default=None, min_length=1, max_length=80)
    target_type: Literal["card", "group"] = "card"
    target_id: Optional[str] = Field(default=None, min_length=1, max_length=80)


class RetroGroupRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    card_ids: list[str] = Field(min_length=2, max_length=50)


class RetroGroupRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retro_channel(retro_id: int) -> str:
    return f"retro_events:{retro_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_retro_repo(request: Request):
    repo = getattr(request.app.state, "retro_repository", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Retro storage is not configured")
    return repo


def _slugify_section(title: str, index: int) -> str:
    base = "".join(ch if ch.isalnum() else "-" for ch in title.strip().lower()).strip("-")
    return (base or f"section-{index + 1}")[:64]


def _clean_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Укажите название ретро")
    return title


def _retro_from_config(retro_id: int, title: str, config: dict) -> Retrospective:
    """Build a fresh live Retrospective from a stored CMS config payload."""
    sections: list[RetroSection] = []
    seen: set[str] = set()
    for index, raw in enumerate(config.get("sections") or []):
        sid = str(raw.get("section_id") or _slugify_section(str(raw.get("title", "")), index))
        if not SECTION_ID_RE.match(sid):
            sid = _slugify_section(str(raw.get("title", "")), index)
        base_sid = sid[:64]
        counter = 2
        while sid in seen:
            suffix = f"-{counter}"
            sid = f"{base_sid[:64 - len(suffix)]}{suffix}"
            counter += 1
        seen.add(sid)
        sections.append(RetroSection(section_id=sid, title=str(raw.get("title", "")).strip() or sid))
    if not sections:
        sections = [RetroSection(section_id=section_id, title=title) for section_id, title in DEFAULT_RETRO_SECTIONS]
    return Retrospective(
        retro_id=retro_id,
        title=title,
        sections=sections,
        votes_per_person=int(config.get("votes_per_person", DEFAULT_VOTES_PER_PERSON)),
        default_section_seconds=int(config.get("default_section_seconds", DEFAULT_SECTION_SECONDS)),
    )


def _anonymized_retro_snapshot(retro: Retrospective) -> dict:
    """Durable retro snapshot safe to return from CMS APIs.

    The live aggregate keeps authors and exact voter ids in Redis so domain
    rules can enforce budgets. The durable Postgres copy must not expose
    those identities because retro cards are promised to be anonymous.
    """
    return {
        "retro_id": retro.retro_id,
        "title": retro.title,
        "sections": [s.to_dict() for s in retro.sections],
        "votes_per_person": retro.votes_per_person,
        "default_section_seconds": retro.default_section_seconds,
        "phase": retro.phase,
        "active_section_id": retro.active_section_id,
        "section_deadline": retro.section_deadline,
        "visited_section_ids": list(retro.visited_section_ids),
        "participants_count": len(retro.participants),
        "cards": [
            {
                "card_id": card.card_id,
                "section_id": card.section_id,
                "text": card.text,
                "created_at": card.created_at,
                "group_id": card.group_id,
                "vote_count": len(card.votes) if card.group_id is None else 0,
            }
            for card in retro.cards
        ],
        "groups": [
            {
                "group_id": group.group_id,
                "section_id": group.section_id,
                "title": group.title,
                "card_ids": list(group.card_ids),
                "created_at": group.created_at,
                "vote_count": len(group.votes),
            }
            for group in retro.groups
        ],
        "action_items": [item.to_dict() for item in retro.action_items],
        "ai_summary": retro.ai_summary,
        "version": retro.version,
    }


def _redact_retro_row(row: dict) -> dict:
    """Return a CMS row without author/voter identities in historical snapshots."""
    safe = dict(row)
    snapshot = safe.get("snapshot")
    if isinstance(snapshot, dict) and "cards" in snapshot:
        cards = []
        for card in snapshot.get("cards") or []:
            if not isinstance(card, dict):
                continue
            cards.append({
                "card_id": card.get("card_id"),
                "section_id": card.get("section_id"),
                "text": card.get("text"),
                "created_at": card.get("created_at"),
                "group_id": card.get("group_id"),
                "vote_count": card.get("vote_count", len(card.get("votes", []) or [])),
            })
        groups = []
        for group in snapshot.get("groups") or []:
            if not isinstance(group, dict):
                continue
            groups.append({
                "group_id": group.get("group_id"),
                "section_id": group.get("section_id"),
                "title": group.get("title"),
                "card_ids": list(group.get("card_ids") or []),
                "created_at": group.get("created_at"),
                "vote_count": group.get("vote_count", len(group.get("votes", []) or [])),
            })
        safe["snapshot"] = {
            key: value
            for key, value in snapshot.items()
            if key not in {"participants"}
        }
        safe["snapshot"]["cards"] = cards
        safe["snapshot"]["groups"] = groups
        safe["snapshot"]["participants_count"] = snapshot.get(
            "participants_count",
            len(snapshot.get("participants", {}) or {}),
        )
    return safe


def _retro_from_anonymized_snapshot(data: dict, retro_id: int) -> Retrospective:
    """Rebuild enough of a Retrospective from the safe snapshot for AI analysis."""
    retro = Retrospective(
        retro_id=retro_id,
        title=str(data.get("title", "")),
        sections=[RetroSection.from_dict(s) for s in data.get("sections", [])],
        votes_per_person=int(data.get("votes_per_person", DEFAULT_VOTES_PER_PERSON)),
        default_section_seconds=int(data.get("default_section_seconds", DEFAULT_SECTION_SECONDS)),
        phase=str(data.get("phase", PHASE_DONE)),
        active_section_id=data.get("active_section_id"),
        section_deadline=data.get("section_deadline"),
        visited_section_ids=[str(section_id) for section_id in data.get("visited_section_ids", [])],
        ai_summary=data.get("ai_summary"),
        version=int(data.get("version", 0)),
    )
    for card_data in data.get("cards", []):
        card = RetroCard(
            card_id=str(card_data.get("card_id", "")),
            section_id=str(card_data.get("section_id", "")),
            text=str(card_data.get("text", "")),
            author_id=0,
            author_name="",
            created_at=str(card_data.get("created_at", "")),
            group_id=(card_data.get("group_id") or None),
        )
        card.votes = set(range(int(card_data.get("vote_count", 0))))
        retro.cards.append(card)
    for group_data in data.get("groups", []):
        try:
            group = RetroGroup(
                group_id=str(group_data.get("group_id", "")),
                section_id=str(group_data.get("section_id", "")),
                title=str(group_data.get("title", "")),
                card_ids=[str(card_id) for card_id in group_data.get("card_ids", [])],
                created_at=str(group_data.get("created_at", "")),
            )
            group.votes = set(range(int(group_data.get("vote_count", 0))))
            retro.groups.append(group)
        except (TypeError, ValueError, KeyError):
            continue
    for item_data in data.get("action_items", []):
        try:
            retro.action_items.append(RetroActionItem.from_dict(item_data))
        except (TypeError, ValueError, KeyError):
            continue
    return retro


def _build_retro_state(retro: Retrospective, viewer_id: Optional[int] = None) -> dict:
    """Anonymous live projection shared by participants and the manager cockpit.

    ``viewer_id`` only affects the ``my_*`` hints (which cards *I* voted,
    how many dots I have left). Authors are never exposed. Broadcasts pass
    ``viewer_id=None`` and clients keep their own dots locally.
    """
    grouped_card_ids = {card_id for group in retro.groups for card_id in group.card_ids}
    ordered = list(retro.cards)
    if retro.phase in (PHASE_VOTING, PHASE_DISCUSSING, PHASE_DONE):
        ordered.sort(key=lambda c: (-(len(c.votes) if c.group_id is None else 0), c.created_at))
    else:
        ordered.sort(key=lambda c: c.created_at)

    cards = [
        {
            "card_id": c.card_id,
            "section_id": c.section_id,
            "text": c.text,
            "group_id": c.group_id,
            "is_grouped": c.card_id in grouped_card_ids,
            "vote_count": len(c.votes) if c.card_id not in grouped_card_ids else 0,
        }
        for c in ordered
    ]

    groups = [
        {
            "group_id": group.group_id,
            "section_id": group.section_id,
            "title": group.title,
            "card_ids": list(group.card_ids),
            "vote_count": len(group.votes),
        }
        for group in retro.groups
    ]
    if retro.phase in (PHASE_VOTING, PHASE_DISCUSSING, PHASE_DONE):
        groups.sort(key=lambda g: (-int(g["vote_count"]), str(g["title"])))

    my_votes: list[str] = []
    if viewer_id is not None:
        my_votes = [
            c.card_id
            for c in retro.cards
            if c.card_id not in grouped_card_ids and viewer_id in c.votes
        ]
        my_votes.extend(g.group_id for g in retro.groups if viewer_id in g.votes)

    return {
        "retro_id": retro.retro_id,
        "title": retro.title,
        "phase": retro.phase,
        "active_section_id": retro.active_section_id,
        "section_deadline": retro.section_deadline,
        "visited_section_ids": list(retro.visited_section_ids),
        "votes_per_person": retro.votes_per_person,
        "default_section_seconds": retro.default_section_seconds,
        "sections": [s.to_dict() for s in retro.sections],
        "cards": cards,
        "groups": groups,
        "action_items": [a.to_dict() for a in retro.action_items],
        "participants_count": len(retro.participants),
        "ai_summary": retro.ai_summary if retro.phase == PHASE_DONE else None,
        "my_votes": my_votes,
        "my_votes_used": len(my_votes),
        "my_votes_remaining": max(0, retro.votes_per_person - len(my_votes)),
        "version": retro.version,
    }


async def _publish_retro(redis_client: aioredis.Redis, retro: Retrospective) -> None:
    """Best-effort broadcast of the anonymous state to all WS listeners."""
    try:
        await redis_client.publish(
            _retro_channel(retro.retro_id),
            json.dumps({"type": "retro_state", "state": _build_retro_state(retro)}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("retro publish failed id=%s err=%r", retro.retro_id, exc)


async def _resolve_retro_token(redis_client: aioredis.Redis, token: str) -> int:
    data = await redis_client.get(f"web_retro:{token}")
    if not data:
        raise HTTPException(status_code=404, detail="Retro token not found or expired")
    try:
        return int(json.loads(data)["retro_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Retro token not found or expired") from exc


async def _load_participant(redis_client: aioredis.Redis, token: str, participant_id: str) -> dict:
    raw = await redis_client.get(f"retro_participant:{token}:{participant_id}")
    if not raw:
        raise HTTPException(status_code=403, detail="Participant not found or session expired")
    try:
        payload = json.loads(raw)
        payload["user_id"] = int(payload["user_id"])
        return payload
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Participant not found or session expired") from exc


# ---------------------------------------------------------------------------
# Public (participant) endpoints
# ---------------------------------------------------------------------------


@retro_router.post("/retro/join")
async def retro_join(body: RetroJoinRequest, request: Request) -> dict:
    """Join a retro by its public token (corporate email + team role)."""
    try:
        display_name = validate_participant_email(body.name)
        team_role = validate_participant_role(body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    redis_client = await _get_redis(request)
    await enforce_rate_limit(
        redis_client,
        key=f"rl:retro_join:ip:{client_ip(request)}",
        limit=RETRO_JOIN_RATE_MAX,
        window_seconds=RETRO_JOIN_RATE_WINDOW,
        error_detail="Too many join attempts",
    )
    retro_id = await _resolve_retro_token(redis_client, body.token)
    await enforce_rate_limit(
        redis_client,
        key=f"rl:retro_join:token:{body.token}",
        limit=RETRO_JOIN_RATE_MAX,
        window_seconds=RETRO_JOIN_RATE_WINDOW,
        error_detail="Too many join attempts",
    )

    participant_id = str(uuid.uuid4())
    user_id = stable_user_id_from_email(display_name)

    await redis_client.setex(
        f"retro_participant:{body.token}:{participant_id}",
        RETRO_TOKEN_TTL,
        json.dumps({"name": display_name, "user_id": user_id, "role": team_role}),
    )

    repo = _get_retro_repo(request)
    try:
        retro, added = await repo.mutate_retro(
            retro_id, lambda r: r.add_participant(user_id, display_name, team_role)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Retro is not active") from None

    if added:
        await _publish_retro(redis_client, retro)

    return {"participant_id": participant_id, "state": _build_retro_state(retro, user_id)}


@retro_router.get("/retro/state/{token}")
async def retro_state(token: str, request: Request, participant_id: Optional[str] = None) -> dict:
    redis_client = await _get_redis(request)
    retro_id = await _resolve_retro_token(redis_client, token)
    repo = _get_retro_repo(request)
    retro = await repo.get_retro(retro_id)
    if retro is None:
        raise HTTPException(status_code=404, detail="Retro is not active")

    viewer_id: Optional[int] = None
    if participant_id:
        try:
            viewer_id = (await _load_participant(redis_client, token, participant_id))["user_id"]
        except HTTPException:
            viewer_id = None
    return _build_retro_state(retro, viewer_id)


@retro_router.post("/retro/card")
async def retro_add_card(body: RetroCardRequest, request: Request) -> dict:
    redis_client = await _get_redis(request)
    retro_id = await _resolve_retro_token(redis_client, body.token)
    participant = await _load_participant(redis_client, body.token, body.participant_id)
    user_id = participant["user_id"]
    await enforce_rate_limit(
        redis_client,
        key=f"rl:retro_card:token:{body.token}:user:{user_id}",
        limit=RETRO_CARD_RATE_MAX,
        window_seconds=RETRO_CARD_RATE_WINDOW,
        error_detail="Too many cards",
    )

    card_id = str(uuid.uuid4())

    def _mutate(r: Retrospective):
        return r.add_card(
            card_id=card_id,
            section_id=body.section_id,
            text=body.text,
            author_id=user_id,
            author_name=participant.get("name", ""),
            created_at=_now_iso(),
        )

    try:
        retro, _ = await repo_mutate(request, retro_id, _mutate)
    except RetroError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    await _publish_retro(redis_client, retro)
    return _build_retro_state(retro, user_id)


@retro_router.post("/retro/vote")
async def retro_vote(body: RetroVoteRequest, request: Request) -> dict:
    redis_client = await _get_redis(request)
    retro_id = await _resolve_retro_token(redis_client, body.token)
    participant = await _load_participant(redis_client, body.token, body.participant_id)
    user_id = participant["user_id"]
    target_id = body.target_id or body.card_id
    if not target_id:
        raise HTTPException(status_code=400, detail="Voting target is required")
    await enforce_rate_limit(
        redis_client,
        key=f"rl:retro_vote:token:{body.token}:user:{user_id}",
        limit=RETRO_VOTE_RATE_MAX,
        window_seconds=RETRO_VOTE_RATE_WINDOW,
        error_detail="Too many votes",
    )

    try:
        retro, _ = await repo_mutate(
            request, retro_id, lambda r: r.toggle_vote(target_id, user_id, body.target_type)
        )
    except RetroError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    await _publish_retro(redis_client, retro)
    return _build_retro_state(retro, user_id)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@retro_router.websocket("/retro-ws/{token}")
async def retro_websocket(websocket: WebSocket, token: str) -> None:
    app_state = websocket.scope["app"].state
    redis_client = getattr(app_state, "web_redis", None)
    repo = getattr(app_state, "retro_repository", None)
    if redis_client is None or repo is None:
        await websocket.close(code=4503)
        return

    token_data = await redis_client.get(f"web_retro:{token}")
    if not token_data:
        await websocket.close(code=4004)
        return
    try:
        retro_id = int(json.loads(token_data)["retro_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        await websocket.close(code=4004)
        return

    await websocket.accept()

    try:
        retro = await repo.get_retro(retro_id)
        state = _build_retro_state(retro) if retro else {"phase": "lobby", "cards": []}
        await websocket.send_text(json.dumps({"type": "retro_state", "state": state}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("retro WS initial state failed: %s", exc)
        await websocket.close(code=1011)
        return

    channel = _retro_channel(retro_id)
    listen_task = asyncio.create_task(redis_pubsub_listener(redis_client, token, channel, websocket))
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive(), timeout=20)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("retro WS receive error: %s", exc)
    finally:
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Shared mutate helper (maps missing live state to 404)
# ---------------------------------------------------------------------------


async def repo_mutate(request: Request, retro_id: int, mutator):
    repo = _get_retro_repo(request)
    try:
        return await repo.mutate_retro(retro_id, mutator)
    except KeyError:
        raise HTTPException(status_code=409, detail="Retro session is not started") from None


# ---------------------------------------------------------------------------
# Manager (CMS) endpoints
# ---------------------------------------------------------------------------


@retro_router.get("/cms/retros")
async def cms_list_retros(
    request: Request,
    team_id: Optional[int] = None,
    sort: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    items = await _get_cms_store(request).list_retros(
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )
    return {"items": [_redact_retro_row(item) for item in items]}


@retro_router.post("/cms/retros")
async def cms_create_retro(
    body: RetroCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    retro = await _get_cms_store(request).create_retro(
        title=_clean_title(body.title),
        config=body.config.model_dump(),
        created_by=actor.id,
        team_id=resolved_team_id,
    )
    await _audit(request, "cms.retro.create", actor.username, "ok", {"retro_id": retro["id"]})
    return retro


async def _require_retro_access(request: Request, retro_id: int, actor: CmsPrincipal) -> dict:
    row = await _get_cms_store(request).get_retro(retro_id)
    if not row:
        raise HTTPException(status_code=404, detail="Retro not found")
    assert_record_access(actor, row)
    return row


@retro_router.get("/cms/retros/{retro_id}")
async def cms_get_retro(
    retro_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_VIEW)),
) -> dict:
    row = await _require_retro_access(request, retro_id, actor)
    row = _redact_retro_row(row)
    # Attach live state when the session has been started.
    live = await _get_retro_repo(request).get_retro(retro_id)
    row["live"] = _build_retro_state(live) if live else None
    return row


@retro_router.put("/cms/retros/{retro_id}")
async def cms_update_retro(
    retro_id: int,
    body: RetroUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    store = _get_cms_store(request)
    existing = await _require_retro_access(request, retro_id, actor)
    if existing["status"] not in ("draft",):
        raise HTTPException(status_code=409, detail="Нельзя менять конфигурацию запущенного ретро")
    retro = await store.update_retro_config(retro_id, _clean_title(body.title), body.config.model_dump())
    await _audit(request, "cms.retro.update", actor.username, "ok", {"retro_id": retro_id})
    return retro


@retro_router.delete("/cms/retros/{retro_id}")
async def cms_delete_retro(
    retro_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    await _require_retro_access(request, retro_id, actor)
    deleted = await _get_cms_store(request).delete_retro(retro_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Retro not found")
    await _get_retro_repo(request).delete_retro(retro_id)
    await _audit(request, "cms.retro.delete", actor.username, "ok", {"retro_id": retro_id})
    return {"ok": True, "id": retro_id}


@retro_router.post("/cms/retros/{retro_id}/invite")
async def cms_retro_invite(
    retro_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    """Bootstrap live state from config and mint a public participant link."""
    store = _get_cms_store(request)
    row = await _require_retro_access(request, retro_id, actor)

    repo = _get_retro_repo(request)
    default = _retro_from_config(retro_id, row["title"], row.get("config") or {})
    retro = await repo.ensure_retro(retro_id, default)

    redis_client = await _get_redis(request)
    await enforce_rate_limit(
        redis_client,
        key=f"rl:retro_invite:actor:{actor.username}:retro:{retro_id}",
        limit=RETRO_INVITE_RATE_MAX,
        window_seconds=RETRO_INVITE_RATE_WINDOW,
        error_detail="Too many retro invite requests",
    )
    token = str(uuid.uuid4())
    await redis_client.setex(f"web_retro:{token}", RETRO_TOKEN_TTL, json.dumps({"retro_id": retro_id}))

    if row["status"] == "draft":
        await store.update_retro_status(retro_id, "live")

    await _audit(request, "cms.retro.invite", actor.username, "ok", {"retro_id": retro_id})
    return {"token": token, "url": f"/r/{token}", "state": _build_retro_state(retro)}


@retro_router.post("/cms/retros/{retro_id}/open-section")
async def cms_retro_open_section(
    retro_id: int,
    body: RetroOpenSectionRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    def _mutate(r: Retrospective):
        seconds = body.seconds if body.seconds is not None else r.default_section_seconds
        deadline = None
        if seconds and seconds > 0:
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        r.open_section(body.section_id, deadline)

    retro = await _manager_mutate(request, retro_id, _mutate, actor)
    await _audit(request, "cms.retro.section.open", actor.username, "ok",
                 {"retro_id": retro_id, "section_id": body.section_id})
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/close-section")
async def cms_retro_close_section(
    retro_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    retro = await _manager_mutate(request, retro_id, lambda r: r.close_section(), actor)
    await _audit(request, "cms.retro.section.close", actor.username, "ok", {"retro_id": retro_id})
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/phase")
async def cms_retro_phase(
    retro_id: int,
    body: RetroPhaseRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    target = body.target.strip().lower()
    if target == PHASE_VOTING:
        mutate = lambda r: r.start_voting()
    elif target == PHASE_DISCUSSING:
        mutate = lambda r: r.start_discussion()
    else:
        raise HTTPException(status_code=400, detail="Unknown phase target")
    retro = await _manager_mutate(request, retro_id, mutate, actor)
    await _audit(request, "cms.retro.phase", actor.username, "ok", {"retro_id": retro_id, "target": target})
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/groups")
async def cms_retro_create_group(
    retro_id: int,
    body: RetroGroupRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    group_id = str(uuid.uuid4())

    def _mutate(r: Retrospective):
        return r.create_group(
            group_id=group_id,
            title=body.title,
            card_ids=body.card_ids,
            created_at=_now_iso(),
        )

    retro = await _manager_mutate(request, retro_id, _mutate, actor)
    await _audit(
        request,
        "cms.retro.group.create",
        actor.username,
        "ok",
        {"retro_id": retro_id, "group_id": group_id, "cards": len(body.card_ids)},
    )
    return _build_retro_state(retro)


@retro_router.patch("/cms/retros/{retro_id}/groups/{group_id}")
async def cms_retro_rename_group(
    retro_id: int,
    group_id: str,
    body: RetroGroupRenameRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    retro = await _manager_mutate(request, retro_id, lambda r: r.rename_group(group_id, body.title), actor)
    await _audit(
        request,
        "cms.retro.group.rename",
        actor.username,
        "ok",
        {"retro_id": retro_id, "group_id": group_id},
    )
    return _build_retro_state(retro)


@retro_router.delete("/cms/retros/{retro_id}/groups/{group_id}")
async def cms_retro_ungroup(
    retro_id: int,
    group_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    retro = await _manager_mutate(request, retro_id, lambda r: r.ungroup(group_id), actor)
    await _audit(
        request,
        "cms.retro.group.delete",
        actor.username,
        "ok",
        {"retro_id": retro_id, "group_id": group_id},
    )
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/action-items")
async def cms_retro_add_action_item(
    retro_id: int,
    body: RetroActionItemRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    item_id = str(uuid.uuid4())

    def _mutate(r: Retrospective):
        r.add_action_item(item_id=item_id, text=body.text, assignee=body.assignee, created_at=_now_iso())

    retro = await _manager_mutate(request, retro_id, _mutate, actor)
    await _audit(request, "cms.retro.action_item.add", actor.username, "ok", {"retro_id": retro_id})
    return _build_retro_state(retro)


@retro_router.delete("/cms/retros/{retro_id}/action-items/{item_id}")
async def cms_retro_remove_action_item(
    retro_id: int,
    item_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    retro = await _manager_mutate(request, retro_id, lambda r: r.remove_action_item(item_id), actor)
    await _audit(request, "cms.retro.action_item.remove", actor.username, "ok", {"retro_id": retro_id})
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/finalize")
async def cms_retro_finalize(
    retro_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_MANAGE)),
) -> dict:
    retro = await _manager_mutate(request, retro_id, lambda r: r.finalize(), actor)
    snapshot = _anonymized_retro_snapshot(retro)
    await _get_cms_store(request).save_retro_snapshot(retro_id, snapshot, status="done")
    await _audit(request, "cms.retro.finalize", actor.username, "ok", {"retro_id": retro_id})
    return _build_retro_state(retro)


@retro_router.post("/cms/retros/{retro_id}/analyze")
async def cms_retro_analyze(
    retro_id: int,
    request: Request,
    async_mode: bool = Query(False, alias="async"),
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_ANALYZE)),
) -> dict:
    store = _get_cms_store(request)
    await _require_retro_access(request, retro_id, actor)
    repo = _get_retro_repo(request)
    retro = await repo.get_retro(retro_id)
    if retro is None:
        row = await store.get_retro(retro_id)
        if row and row.get("snapshot"):
            retro = _retro_from_anonymized_snapshot(row["snapshot"], retro_id)
    if retro is None:
        raise HTTPException(status_code=404, detail="Retro not found")
    if retro.phase != PHASE_DONE:
        raise HTTPException(status_code=409, detail="Сначала завершите ретро")
    if not retro.cards:
        raise HTTPException(status_code=400, detail="Нет карточек для анализа")

    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:retro_ai:actor:{actor.username}",
        limit=int(os.getenv("RETRO_AI_RATE_MAX", "20")),
        window_seconds=int(os.getenv("RETRO_AI_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Слишком много AI-запросов, попробуйте позже",
    )

    if retro.ai_summary:
        await _audit(request, "cms.retro.analyze", actor.username, "ok", {"retro_id": retro_id, "cached": True})
        return {"ai_summary": retro.ai_summary, "cached": True}

    from services.voting_service.ai_job_runners import run_retro_ai_job
    from services.voting_service.ai_jobs import get_job, get_or_create_job, job_public_view, spawn_ai_job
    from services.voting_service.retro_ai_llm import LlmRetroError, generate_retro_analysis

    if async_mode:
        redis = await _get_redis(request)
        job_id, is_new = await get_or_create_job(
            redis,
            kind="retro",
            resource_key=f"retro:{retro_id}",
            actor=actor.username,
        )
        if is_new:
            spawn_ai_job(run_retro_ai_job(request.app, job_id=job_id, retro_id=retro_id, actor_username=actor.username))
        job_record = await get_job(redis, job_id)
        return job_public_view(job_record or {"job_id": job_id, "status": "queued", "phase": "queued", "message": "В очереди"})

    http_session = getattr(request.app.state, "http_session", None)
    if http_session is None:
        raise HTTPException(status_code=503, detail="AI is not configured")
    try:
        summary = await generate_retro_analysis(http_session, retro)
    except LlmRetroError as exc:
        await _audit(request, "cms.retro.analyze", actor.username, "error", {"retro_id": retro_id, "error": exc.message})
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    await store.save_retro_ai_summary(retro_id, summary)
    try:
        retro, _ = await repo.mutate_retro(retro_id, lambda r: _set_ai(r, summary))
        await _publish_retro(await _get_redis(request), retro)
    except KeyError:
        pass
    await _audit(request, "cms.retro.analyze", actor.username, "ok", {"retro_id": retro_id})
    return {"ai_summary": summary}


@retro_router.get("/cms/retros/{retro_id}/analyze/jobs/{job_id}")
async def cms_retro_analyze_job_status(
    retro_id: int,
    job_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_RETRO_ANALYZE)),
) -> dict:
    await _require_retro_access(request, retro_id, actor)

    from services.voting_service.ai_jobs import get_job, job_public_view

    redis = await _get_redis(request)
    job = await get_job(redis, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    if job.get("kind") != "retro" or job.get("resource_key") != f"retro:{retro_id}":
        raise HTTPException(status_code=404, detail="AI job not found")
    return job_public_view(job)


def _set_ai(retro: Retrospective, summary: dict) -> None:
    retro.ai_summary = summary
    retro.bump_version()


async def _manager_mutate(
    request: Request,
    retro_id: int,
    mutator,
    actor: CmsPrincipal,
) -> Retrospective:
    """Run a manager mutation, broadcast, and map domain errors to HTTP."""
    await _require_retro_access(request, retro_id, actor)
    try:
        retro, _ = await repo_mutate(request, retro_id, mutator)
    except RetroError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    await _publish_retro(await _get_redis(request), retro)
    return retro
