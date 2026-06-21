"""Main web app API for facilitated Planning Poker sessions."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.domain.estimation import (
    build_flat_results,
    clear_task_votes,
    estimation_mode_payload,
    get_mode_config,
    MAX_STORY_POINTS,
    is_split_mode,
    normalise_estimation_mode,
    resolve_track_for_participant,
    VALID_ESTIMATION_MODES,
)
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.close_session import CloseSessionUseCase
from app.usecases.manage_tasks import (
    AddManualTaskUseCase,
    AddManualTasksUseCase,
    DeleteTaskUseCase,
    MoveTaskUseCase,
    ReorderTasksUseCase,
    TaskMutationResult,
    TaskQueueError,
    ReopenCompletedTaskUseCase,
    UpdateTaskUseCase,
)
from services.voting_service._http_shared import (
    CmsPrincipal,
    JiraImportRequest,
    JiraPreviewRequest,
    TaskCreateRequest,
    _ensure_current_task_description,
    _fetch_jira_description,
    TaskInput,
    TaskMoveRequest,
    TaskReorderRequest,
    TaskUpdateRequest,
    _audit,
    _existing_jira_keys,
    _get_repo_session,
    _jira_preview,
    _jira_preview_payload,
    _mutate_repo_session,
    _mutation_payload,
    _publish_state,
    _raise_task_error,
    require_permission,
)
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT
from services.voting_service.cms_rbac import PERM_APP_SESSIONS_MANAGE
from services.voting_service.cms_team_access import assert_record_access, resolve_create_team_id
from services.voting_service.ai_summary_llm import (
    LlmSummaryError,
    fetch_jira_issue_context,
    generate_ai_summary_llm,
)
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service.session_finish_notify import maybe_notify_session_finished
from services.voting_service.web_api import WEB_TOKEN_TTL, _build_web_session_state


# Per-actor (authenticated CMS user) quotas for the manager surface.
# Both invite refresh and AI summary generation are cheap-ish for one user
# but expensive at fleet scale (Anthropic billing for the second), so we
# cap them by username rather than IP.
APP_INVITE_RATE_LIMIT_MAX = int(os.getenv("APP_INVITE_RATE_LIMIT_MAX", "30"))
APP_INVITE_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("APP_INVITE_RATE_LIMIT_WINDOW_SECONDS", "60"))
AI_SUMMARY_RATE_LIMIT_MAX = int(os.getenv("AI_SUMMARY_RATE_LIMIT_MAX", "20"))
AI_SUMMARY_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AI_SUMMARY_RATE_LIMIT_WINDOW_SECONDS", "3600"))

app_router = APIRouter()


# ``_publish_state`` historically lived in this module; tests import it from
# here (see ``tests/test_review_hardening.py``). Keep the public name visible
# via re-export so that import path keeps working.
__all__ = ["app_router", "_publish_state"]

DEMO_CHAT_ID = -42_424_242
DEMO_TITLE = "Demo planning session"
DEMO_JQL = "project = DEMO ORDER BY priority DESC"


class AppSessionCreateRequest(BaseModel):
    title: str = Field(default="Planning Poker", min_length=1, max_length=120)
    team_id: Optional[int] = None
    estimation_mode: Optional[str] = None


class AppSessionStartRequest(BaseModel):
    estimation_mode: Optional[str] = None


class AppSessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class FinalEstimateRequest(BaseModel):
    value: Optional[int] = Field(default=None, ge=0, le=MAX_STORY_POINTS)
    tracks: Optional[dict[str, int]] = None


class ReopenCompletedRequest(BaseModel):
    expected_version: Optional[int] = Field(default=None, ge=0)


class AiTaskSummary(BaseModel):
    description: str
    methods: list[str]
    complexity: str
    sp_dev: Optional[int] = None
    sp_test: Optional[int] = None
    sp_final: Optional[int] = None
    scale_label: Optional[str] = None
    confidence: Optional[str] = None
    assumptions: list[str] = Field(default_factory=list)
    estimation_model: Optional[str] = None
    generated_at: str
    source: str = "anthropic"


def _manager_dep(actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE))) -> CmsPrincipal:
    return actor


async def _require_manager_session_access(
    request: Request,
    chat_id: int,
    topic_id: Optional[int],
    actor: CmsPrincipal,
) -> None:
    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store is None:
        return
    row = await cms_store.get_session_by_chat(chat_id, topic_id)
    if row:
        assert_record_access(actor, row)


async def _require_manager_session(
    request: Request,
    chat_id: int,
    actor: CmsPrincipal = Depends(_manager_dep),
    topic_id: Optional[int] = None,
) -> CmsPrincipal:
    await _require_manager_session_access(request, chat_id, topic_id, actor)
    return actor


def _public_url(path: str) -> str:
    base = os.getenv("WEB_UI_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def _new_app_chat_id() -> int:
    return -int(secrets.randbelow(8_000_000_000_000) + 1_000_000_000_000)


def _demo_enabled() -> bool:
    return os.getenv("ENABLE_DEMO_SESSION", "true").lower() in {"1", "true", "yes", "on"}


async def _stored_session_row(
    request: Request,
    chat_id: int,
    topic_id: Optional[int],
) -> Optional[dict]:
    """Best-effort lookup of the CMS read-model row for a live session."""
    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store is None:
        return None
    try:
        return await cms_store.get_session_by_chat(chat_id, topic_id)
    except AttributeError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stored_session_row lookup failed chat_id=%s topic_id=%s err=%r",
            chat_id,
            topic_id,
            exc,
        )
        return None


async def _stored_session_title(
    request: Request,
    chat_id: int,
    topic_id: Optional[int],
) -> Optional[str]:
    """Best-effort lookup of the human-readable session title from the CMS
    read model. Returns ``None`` when the store is unavailable, the row
    doesn't exist yet, or the stored title is empty."""
    row = await _stored_session_row(request, chat_id, topic_id)
    if not row:
        return None
    title = (row.get("title") or "").strip()
    return title or None


def _resolve_session_title(
    requested_title: Optional[str],
    stored_title: Optional[str],
    *,
    default: str = "Planning Poker",
) -> str:
    """Pick the best title to surface to the manager: an explicit query
    parameter wins (unless it is the legacy default), otherwise we use the
    stored title, finally falling back to the generic default."""
    requested = (requested_title or "").strip()
    if requested and requested != default:
        return requested
    if stored_title:
        return stored_title
    return requested or default


async def _create_invite_token(
    request: Request,
    chat_id: int,
    topic_id: Optional[int],
    title: str,
    *,
    team_id: Optional[int] = None,
) -> tuple[str, str]:
    token = secrets.token_urlsafe(18)
    payload = json.dumps({"chat_id": chat_id, "topic_id": topic_id, "title": title})
    redis_client = request.app.state.web_redis
    await redis_client.setex(f"web:{token}", WEB_TOKEN_TTL, payload)

    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store:
        await cms_store.record_web_token(token, chat_id, topic_id, WEB_TOKEN_TTL)
        # Persist the manager-supplied title onto the read-model row so the
        # CMS can show a friendly name instead of the technical chat key.
        # We only overwrite empty titles, so manual renames in CMS survive
        # invite regeneration.
        try:
            await cms_store.set_session_title_by_chat(
                chat_id,
                topic_id,
                title,
                team_id=team_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "set_session_title failed chat_id=%s topic_id=%s err=%r",
                chat_id,
                topic_id,
                exc,
            )

    path = f"/s/{token}"
    return token, _public_url(path)


def _serialize_completed_task(session: Session, task: Task, *, bucket_index: Optional[int] = None) -> dict:
    """Render a played task with full vote breakdown for manager-facing views.

    Used by manager state (HistoryStrip), Finish summary and CSV export. The
    vote breakdown is *not* exposed to participants — see ``_build_web_session_state``.
    """
    votes = _completed_vote_rows(session, task)

    distribution: dict[str, int] = {}
    for row in votes:
        value = row["value"]
        distribution[value] = distribution.get(value, 0) + 1

    unique_numeric: set[str] = {row["value"] for row in votes if row["value"] not in {"?", "coffee", "skip"}}
    consensus = len(unique_numeric) == 1 if unique_numeric else False

    return {
        "task_id": task.task_id,
        "jira_key": task.jira_key,
        "summary": task.summary,
        "url": task.url,
        "story_points": task.story_points,
        "story_points_by_track": dict(task.story_points_by_track) if task.story_points_by_track else None,
        "source": task.source,
        "completed_at": task.completed_at,
        "bucket_index": bucket_index,
        "ai_summary": task.ai_summary,
        "votes": votes,
        "distribution": distribution,
        "voter_count": len(votes),
        "consensus": consensus,
    }


def _completed_vote_rows(session: Session, task: Task) -> list[dict]:
    """Votes with participant role/track metadata for reports and exports."""
    if not is_split_mode(session.estimation_mode):
        return [
            {
                "name": session.participants[uid].name if uid in session.participants else "—",
                "value": value,
                "role": (
                    session.participants[uid].team_role
                    if uid in session.participants
                    else None
                ),
                "track": None,
                "track_label": None,
            }
            for uid, value in task.votes.items()
        ]

    config = get_mode_config(session.estimation_mode)
    track_label_by_key = {track.key: track.label for track in config.tracks}
    rows: list[dict] = []
    for track_key, track_votes in task.track_votes.items():
        for uid, value in track_votes.items():
            participant = session.participants.get(uid)
            rows.append(
                {
                    "name": participant.name if participant else "—",
                    "value": value,
                    "role": participant.team_role if participant else None,
                    "track": track_key,
                    "track_label": track_label_by_key.get(track_key, track_key),
                }
            )
    return rows


def _participant_report_rows(session: Session) -> list[dict]:
    config = get_mode_config(session.estimation_mode)
    track_label_by_key = {track.key: track.label for track in config.tracks}
    rows: list[dict] = []
    for uid, participant in session.participants.items():
        if not session.can_vote(uid):
            continue
        track_key = None
        if is_split_mode(session.estimation_mode):
            track_key = resolve_track_for_participant(session, uid)
        rows.append(
            {
                "name": participant.name,
                "role": participant.team_role,
                "track": track_key,
                "track_label": track_label_by_key.get(track_key) if track_key else None,
            }
        )
    return sorted(rows, key=lambda item: (str(item.get("track_label") or ""), str(item["name"]).casefold()))


def _final_estimate_label(entry: dict) -> str:
    by_track = entry.get("story_points_by_track")
    if isinstance(by_track, dict) and by_track:
        return ", ".join(f"{track}: {value}" for track, value in by_track.items())
    return str(entry["story_points"]) if entry["story_points"] is not None else "—"


def _completed_tasks_in_batch(session: Session):
    """Raw (un-serialized) sequence of tasks already played in the active batch.

    Three layered cases:
    1. Explicit Finish was called → ``last_batch`` keeps the finished work.
    2. Managers can add more tasks after Finish; those live in
       ``tasks_queue`` until the next Finish call, so reports include the
       already-played queue slice on top of ``last_batch``.
    3. The cursor was advanced past the last task by auto-next, but Finish
       was not (yet) explicitly invoked. ``tasks_queue`` still holds the
       played tasks with their votes intact.
    """
    completed = list(session.last_batch)
    if session.batch_completed:
        # Auto-next-on-last clears the active cursor before Finish migrates
        # newly added tasks into last_batch.
        return completed + list(session.tasks_queue)
    return completed + list(session.tasks_queue[: session.current_task_index])


def _completed_in_batch(session: Session) -> list[dict]:
    """Serialised, full list — kept for callers that genuinely need everything
    (e.g. CSV export). Prefer ``_paginate_completed_in_batch`` for UI traffic."""
    return [
        _serialize_completed_task(session, task, bucket_index=idx)
        for idx, task in enumerate(_completed_tasks_in_batch(session))
    ]


COMPLETED_DEFAULT_LIMIT = 20
COMPLETED_MAX_LIMIT = 200


def _parse_int_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def _paginate_completed_in_batch(
    session: Session,
    *,
    limit: int,
    cursor: Optional[str],
) -> dict:
    """Return a cursor-paginated slice of the already-played tasks in the
    active batch. ``cursor`` is the integer offset from the start (oldest-first).
    Newest tasks are at the end."""
    limit = max(1, min(limit, COMPLETED_MAX_LIMIT))
    offset = _parse_int_cursor(cursor)
    all_tasks = _completed_tasks_in_batch(session)
    total = len(all_tasks)
    slice_ = all_tasks[offset: offset + limit]
    next_offset = offset + len(slice_)
    items = [
        _serialize_completed_task(session, task, bucket_index=offset + idx)
        for idx, task in enumerate(slice_)
    ]
    return {
        "items": items,
        "next_cursor": str(next_offset) if next_offset < total else None,
        "limit": limit,
        "total": total,
    }


def _current_task_votes(session: Session) -> list[dict]:
    """Manager-only: real votes (with participant names) before reveal."""
    task = session.current_task
    if not task:
        return []
    return build_flat_results(session, task)


def _manager_session_payload(
    session: Session,
    *,
    title: str = "Planning Poker",
    invite_url: Optional[str] = None,
    token: Optional[str] = None,
    completed_limit: Optional[int] = None,
    team_id: Optional[int] = None,
    team: Optional[dict] = None,
) -> dict:
    """Manager-facing snapshot of the session.

    ``completed_limit`` is opt-in cursor pagination for ``completed_tasks``:
    when set, only the OLDEST ``completed_limit`` played tasks are inlined,
    plus a ``completed_next_cursor`` callers can pass to
    ``/sessions/{chat_id}/completed`` to fetch the rest. When ``None``,
    callers receive the full (legacy) list — kept for backward compatibility
    with older clients that read ``completed_tasks`` directly.
    """
    if completed_limit is None:
        completed = _completed_in_batch(session)
        completed_total = len(completed)
        completed_next_cursor: Optional[str] = None
    else:
        page = _paginate_completed_in_batch(session, limit=completed_limit, cursor=None)
        completed = page["items"]
        completed_total = page["total"]
        completed_next_cursor = page["next_cursor"]

    return {
        "chat_id": session.chat_id,
        "topic_id": session.topic_id,
        "title": title,
        "token": token,
        "invite_url": invite_url,
        "tasks_version": session.tasks_version,
        "tasks_queue_count": len(session.tasks_queue),
        "current_task_id": session.current_task_id,
        "current_batch_started_at": session.current_batch_started_at,
        "state": _build_web_session_state(session),
        # Manager-only enrichment: votes & completed history.
        "current_task_votes": _current_task_votes(session),
        "completed_tasks": completed,
        "completed_count": completed_total,
        "completed_next_cursor": completed_next_cursor,
        "team_id": team_id,
        "team": team,
        **estimation_mode_payload(session.estimation_mode),
    }


def _task_page(session: Session, limit: int, cursor: Optional[str], q: Optional[str]) -> dict:
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            offset = 0

    query = (q or "").strip().lower()
    tasks = session.tasks_queue
    if query:
        tasks = [
            task for task in tasks
            if query in task.summary.lower() or (task.jira_key and query in task.jira_key.lower())
        ]
    slice_ = tasks[offset: offset + limit]
    next_offset = offset + len(slice_)
    return {
        "items": [
            {
                "id": -1,
                "session_id": -1,
                "task_uid": task.task_id,
                "bucket": "tasks_queue",
                "bucket_index": session.tasks_queue.index(task),
                "jira_key": task.jira_key,
                "summary": task.summary,
                "url": task.url,
                "story_points": task.story_points,
                "source": task.source,
                "votes_count": len(task.votes or {}),
                "numeric_avg": None,
                "numeric_max": None,
                "completed_at": task.completed_at,
                "jql": task.jql,
                "created_at_text": task.created_at,
                "domain_updated_at": task.updated_at,
                "updated_at": task.updated_at,
            }
            for task in slice_
        ],
        "next_cursor": str(next_offset) if next_offset < len(tasks) else None,
        "limit": limit,
    }


@app_router.post("/app/sessions")
async def create_app_session(
    body: AppSessionCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(_manager_dep),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    repo = request.app.state.repository
    chat_id = _new_app_chat_id()
    topic_id = None
    session = await _get_repo_session(repo, chat_id, topic_id)
    if body.estimation_mode:
        mode = normalise_estimation_mode(body.estimation_mode)
        if mode not in VALID_ESTIMATION_MODES:
            raise HTTPException(status_code=400, detail="Invalid estimation mode")

        def set_mode(session: Session) -> None:
            session.estimation_mode = mode

        session, _ = await _mutate_repo_session(repo, chat_id, topic_id, set_mode)
    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store is not None:
        await cms_store.set_session_team_by_chat(chat_id, topic_id, resolved_team_id)
    token, invite_url = await _create_invite_token(
        request,
        chat_id,
        topic_id,
        body.title,
        team_id=resolved_team_id,
    )
    await _audit(
        request,
        "app.session.create",
        actor.username,
        "ok",
        {"chat_id": chat_id, "title": body.title, "team_id": resolved_team_id},
    )
    return _manager_session_payload(
        session,
        title=body.title,
        invite_url=invite_url,
        token=token,
        team_id=resolved_team_id,
    )


@app_router.post("/app/demo-session")
async def create_demo_session(request: Request, reset: bool = Query(default=False)) -> dict:
    """Create or reuse a real demo session for local/product testing."""
    if not _demo_enabled():
        raise HTTPException(status_code=404, detail="Demo session is disabled")

    from app.adapters.jira_service_client import JiraServiceHttpClient

    jira_client = JiraServiceHttpClient()
    try:
        demo_rows = await jira_client.parse_jira_request(DEMO_JQL, max_results=20)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Demo tasks unavailable: {exc}") from exc
    finally:
        await jira_client.close()

    if not demo_rows:
        raise HTTPException(status_code=503, detail="No demo tasks returned from jira-service")

    chat_id = DEMO_CHAT_ID
    topic_id = None
    repo = request.app.state.repository

    def mutate(session: Session) -> None:
        if reset:
            session.participants.clear()
            session.tasks_queue.clear()
            session.history.clear()
            session.last_batch.clear()

        # Demo tasks may already live in ``last_batch`` after a previous run.
        # Do not treat that as "already imported" — only skip keys already queued.
        if not session.tasks_queue:
            queued_keys = {task.jira_key for task in session.tasks_queue if task.jira_key}
            for item in demo_rows:
                key = item["key"]
                if key in queued_keys:
                    continue
                session.tasks_queue.append(
                    Task(
                        jira_key=key,
                        summary=item["summary"],
                        story_points=item.get("story_points") or None,
                        source="jira",
                        jql=DEMO_JQL,
                    )
                )
                queued_keys.add(key)

        if session.tasks_queue and (reset or session.batch_completed or not session.current_batch_started_at or not session.current_task):
            session.current_task_index = 0
            session.batch_completed = False
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.revealed_task_id = None
            if session.current_task:
                clear_task_votes(session.current_task, session.estimation_mode)

        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(repo, chat_id, topic_id, mutate)
    token, invite_url = await _create_invite_token(request, chat_id, topic_id, DEMO_TITLE)
    await _publish_state(request, session)
    await _audit(request, "app.demo_session", None, "ok", {"chat_id": chat_id, "reset": reset})
    return _manager_session_payload(session, title=DEMO_TITLE, invite_url=invite_url, token=token)


@app_router.get("/app/sessions/{chat_id}/state")
async def app_session_state(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = None,
    completed_limit: Optional[int] = Query(default=None, ge=1, le=COMPLETED_MAX_LIMIT),
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    # Backfill description for the current Jira task when it wasn't
    # captured at import time (sessions imported before the field
    # landed). No-op on the warm path — once the field is filled in,
    # the helper short-circuits without doing any I/O.
    await _ensure_current_task_description(request, chat_id, topic_id)
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_row = await _stored_session_row(request, chat_id, topic_id)
    stored_title = (stored_row.get("title") or "").strip() if stored_row else None
    resolved_title = _resolve_session_title(title, stored_title or None)
    return _manager_session_payload(
        session,
        title=resolved_title,
        completed_limit=completed_limit,
        team_id=stored_row.get("team_id") if stored_row else None,
        team=stored_row.get("team") if stored_row else None,
    )


@app_router.get("/app/sessions/{chat_id}/completed")
async def app_session_completed(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(default=COMPLETED_DEFAULT_LIMIT, ge=1, le=COMPLETED_MAX_LIMIT),
    cursor: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Paginated, oldest-first slice of tasks already played in the active
    batch. Use it to lazy-load Manager's HistoryStrip and the Finished-session
    report without pulling the entire batch in one payload."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _paginate_completed_in_batch(session, limit=limit, cursor=cursor)


@app_router.post("/app/sessions/{chat_id}/invite")
async def app_regenerate_invite(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Mint a fresh invite token for an existing manager session.

    Each refresh writes a new ``web:<token>`` key into Redis, so this is the
    public-token equivalent of /web/token but for the authenticated manager
    surface. Rate-limit by actor so a stuck UI loop can't fill Redis even
    behind an authenticated session.

    Web tokens live in Redis with an 8h TTL and may be evicted before the
    session itself is finished (volume reset, manager comes back the next day,
    etc.). Without this endpoint the manager would see a stale invite_url
    cached in localStorage and participants would hit "Session token not found
    or expired" on /s/<token>. The session itself stays the same chat_id.
    """
    await enforce_rate_limit(
        request.app.state.web_redis,
        key=f"rl:app_invite:actor:{actor.username}",
        limit=APP_INVITE_RATE_LIMIT_MAX,
        window_seconds=APP_INVITE_RATE_LIMIT_WINDOW_SECONDS,
        error_detail="Too many invite refresh requests",
    )
    # Touch the session so we know it exists in the repository before binding
    # a new token to its identity (also normalizes lazily-created sessions).
    await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    token, invite_url = await _create_invite_token(request, chat_id, topic_id, resolved_title)
    await _audit(
        request,
        "app.session.invite_regenerate",
        actor.username,
        "ok",
        {"chat_id": chat_id},
    )
    return {"token": token, "invite_url": invite_url}


@app_router.patch("/app/sessions/{chat_id}/title")
async def app_rename_session(
    chat_id: int,
    body: AppSessionRenameRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Rename an active manager session.

    The friendly title is stored on ``cms_sessions.title`` so the CMS surfaces
    the same name. Unlike create/invite-regenerate this endpoint *always*
    overwrites the stored title — the manager is the source of truth here.
    """
    await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    cms_store = getattr(request.app.state, "cms_store", None)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    if cms_store is not None:
        try:
            await cms_store.set_session_title_by_chat(
                chat_id, topic_id, new_title, only_if_empty=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rename_session failed chat_id=%s topic_id=%s err=%r",
                chat_id,
                topic_id,
                exc,
            )
            raise HTTPException(status_code=503, detail="Title store unavailable") from exc
    await _audit(
        request,
        "app.session.rename",
        actor.username,
        "ok",
        {"chat_id": chat_id, "title": new_title},
    )
    return {"chat_id": chat_id, "topic_id": topic_id, "title": new_title}


@app_router.get("/app/sessions/{chat_id}/tasks")
async def app_session_tasks(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _task_page(session, limit, cursor, q)


@app_router.post("/app/sessions/{chat_id}/tasks")
async def app_create_task(
    chat_id: int,
    body: TaskCreateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
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
        await _audit(request, "app.task.create", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.create", actor.username, "ok", {"chat_id": chat_id, "task_id": result.task.task_id if result.task else None})
    return _mutation_payload(result, -1)


@app_router.patch("/app/sessions/{chat_id}/tasks/{task_id}")
async def app_update_task(
    chat_id: int,
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
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
        await _audit(request, "app.task.update", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.update", actor.username, "ok", {"chat_id": chat_id, "task_id": task_id})
    return _mutation_payload(result, -1)


@app_router.delete("/app/sessions/{chat_id}/tasks/{task_id}")
async def app_delete_task(
    chat_id: int,
    task_id: str,
    request: Request,
    topic_id: Optional[int] = None,
    expected_version: Optional[int] = Query(default=None, ge=0),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = DeleteTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(chat_id=chat_id, topic_id=topic_id, task_id=task_id, expected_version=expected_version)
    except TaskQueueError as exc:
        await _audit(request, "app.task.delete", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.delete", actor.username, "ok", {"chat_id": chat_id, "task_id": task_id})
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/{task_id}/move")
async def app_move_task(
    chat_id: int,
    task_id: str,
    body: TaskMoveRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
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
        await _audit(request, "app.task.move", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/reorder")
async def app_reorder_tasks(
    chat_id: int,
    body: TaskReorderRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = ReorderTasksUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            ordered_task_ids=body.ordered_task_ids,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "app.task.reorder", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/jira-preview")
async def app_preview_jira_tasks(
    chat_id: int,
    body: JiraPreviewRequest,
    request: Request,
    topic_id: Optional[int] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)
    return _jira_preview_payload(issues, _existing_jira_keys(session))


@app_router.get("/app/debug/jira-description/{issue_key}")
async def app_debug_jira_description(
    issue_key: str,
    request: Request,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Diagnostic endpoint: ask jira-service for the description of a key.

    Lets operators verify in one curl whether the issue actually carries
    a non-empty description, without spinning up an import. Returns the
    raw length and a short preview so it's also safe to share in chat.
    Bounded by manager auth — no anonymous use.
    """
    fetched = await _fetch_jira_description(request.app.state.http_session, issue_key)
    if fetched.is_empty:
        return {"key": issue_key.strip().upper(), "found": False}
    text = fetched.text or ""
    # ``format`` makes the source obvious in one curl: "html" is Jira
    # renderedFields (what the voter UI prefers), "adf" is structured
    # ADF, "plain" is the flat string fallback.
    fmt = "html" if fetched.html else ("adf" if fetched.adf else "plain")
    adf_root_type = fetched.adf.get("type") if isinstance(fetched.adf, dict) else None
    adf_first_block_types: list[str] = []
    if isinstance(fetched.adf, dict):
        for child in (fetched.adf.get("content") or [])[:5]:
            if isinstance(child, dict) and child.get("type"):
                adf_first_block_types.append(str(child.get("type")))
    return {
        "key": issue_key.strip().upper(),
        "found": True,
        "format": fmt,
        "length": len(text),
        "newline_count": text.count("\n"),
        "preview": text[:300],
        "has_adf": bool(fetched.adf),
        "has_html": bool(fetched.html),
        "html_length": len(fetched.html or ""),
        "adf_root_type": adf_root_type,
        "adf_first_block_types": adf_first_block_types,
    }


@app_router.post("/app/sessions/{chat_id}/tasks/jira-import")
async def app_import_jira_tasks(
    chat_id: int,
    body: JiraImportRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    selected = {key.strip().upper() for key in body.selected_keys if key.strip()}
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)

    # Pre-fetch the issue body for every selected key so we can store it on
    # the Task at import time. Voters then see the full Jira spec inline on
    # the vote page and the AI summary prompt has a cheap fallback when
    # the per-request context fetch fails. Each call is best-effort and
    # de-duped by the jira-service in-memory cache; failures resolve to
    # ``None`` and never block the import.
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
    # One summary line per import call: how many keys we tried, how
    # many got plain text, and how many got rich ADF. If both numbers
    # are 0 the ratio gives operators an immediate "Jira returns empty
    # bodies" signal without grepping per-key warnings.
    logger.info(
        "jira import description fetch chat=%s tried=%d filled_text=%d filled_adf=%d filled_html=%d",
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
                story_points=issue.get("story_points") or None,
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

    try:
        session, result = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    except TaskQueueError as exc:
        await _audit(request, "app.task.jira_import", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, session)
    await _audit(request, "app.task.jira_import", actor.username, "ok", {"chat_id": chat_id, "count": len(result.tasks)})
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/start")
async def app_start_session(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    body: Optional[AppSessionStartRequest] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    start_body = body or AppSessionStartRequest()

    def mutate(session: Session) -> Optional[str]:
        if not session.tasks_queue:
            return "Add at least one task before starting."
        if start_body.estimation_mode:
            mode = normalise_estimation_mode(start_body.estimation_mode)
            if mode not in VALID_ESTIMATION_MODES:
                return "Invalid estimation mode"
            session.estimation_mode = mode
        session.normalize_current_task_index()
        session.batch_completed = False
        started = datetime.utcnow().isoformat()
        session.current_batch_started_at = started
        session.last_batch_started_at = started  # preserved through next/finish for summary
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
        session.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    # First task of the batch just became active — make sure its Jira
    # description is loaded before we publish/return, otherwise the
    # voter UI would briefly miss the spec block until the next WS
    # push. Helper mutates ``session`` in place.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.start", actor.username, "ok", {"chat_id": chat_id})
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/ai-summary")
async def app_generate_ai_summary(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    async_mode: bool = Query(False, alias="async"),
    refresh: bool = Query(False),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Generate a facilitator-facing AI hint for the current voting task via Anthropic.

    The summary is stored on the active ``Task`` and appears in manager state and
    participant WebSocket payloads. Strict mode: no heuristic fallback when LLM fails.

    Each call costs an Anthropic completion, so we cap them per CMS actor — a
    stuck UI loop must never burn the LLM budget unbounded.
    """
    await enforce_rate_limit(
        request.app.state.web_redis,
        key=f"rl:ai_summary:actor:{actor.username}",
        limit=AI_SUMMARY_RATE_LIMIT_MAX,
        window_seconds=AI_SUMMARY_RATE_LIMIT_WINDOW_SECONDS,
        error_detail="Too many AI summary requests",
    )
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    if not session.current_task or not session.current_batch_started_at:
        raise HTTPException(status_code=400, detail="Start voting before generating an AI summary.")

    task = session.current_task
    if task.ai_summary and not refresh:
        if task.jira_key and isinstance(task.ai_summary, dict):
            from services.voting_service.ai_job_runners import spawn_session_ai_jira_export
            from services.voting_service.ai_summary_jira_export import should_skip_jira_export

            if not should_skip_jira_export(task.ai_summary):
                spawn_session_ai_jira_export(
                    request.app,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    task_id=task.task_id,
                    issue_key=task.jira_key,
                    summary=dict(task.ai_summary),
                    actor_username=actor.username,
                )
        return _manager_session_payload(session)

    from services.voting_service.ai_job_runners import run_session_ai_summary_job
    from services.voting_service.ai_jobs import get_job, get_or_create_job, job_public_view, spawn_ai_job

    if async_mode:
        redis = request.app.state.web_redis
        resource_key = f"session:{chat_id}:{task.task_id}:refresh" if refresh else f"session:{chat_id}:{task.task_id}"
        job_id, is_new = await get_or_create_job(
            redis,
            kind="session_ai_summary",
            resource_key=resource_key,
            actor=actor.username,
        )
        if is_new:
            spawn_ai_job(
                run_session_ai_summary_job(
                    request.app,
                    job_id=job_id,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    task_id=task.task_id,
                    actor_username=actor.username,
                    force_refresh=refresh,
                )
            )
        job_record = await get_job(redis, job_id)
        return job_public_view(job_record or {"job_id": job_id, "status": "queued", "phase": "queued", "message": "В очереди"})

    http_session = request.app.state.http_session
    jira_context = None
    if task.jira_key:
        try:
            jira_context = await fetch_jira_issue_context(http_session, task.jira_key)
        except LlmSummaryError as exc:
            await _audit(
                request,
                "app.task.ai_summary.generate",
                actor.username,
                "failed",
                {"chat_id": chat_id, "task_id": task.task_id, "error": exc.message},
            )
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        summary = await generate_ai_summary_llm(http_session, task, jira_context)
    except LlmSummaryError as exc:
        await _audit(
            request,
            "app.task.ai_summary.generate",
            actor.username,
            "failed",
            {"chat_id": chat_id, "task_id": task.task_id, "error": exc.message},
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def mutate(active: Session) -> Optional[str]:
        if not active.current_task or active.current_task.task_id != task.task_id:
            return "Task changed before AI summary could be saved. Refresh and try again."
        active.current_task.ai_summary = summary
        active.current_task.touch()
        active.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    await _publish_state(request, session)
    await _audit(
        request,
        "app.task.ai_summary.generate",
        actor.username,
        "ok",
        {"chat_id": chat_id, "task_id": session.current_task_id, "source": summary.get("source")},
    )
    if task.jira_key:
        from services.voting_service.ai_job_runners import spawn_session_ai_jira_export

        spawn_session_ai_jira_export(
            request.app,
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task.task_id,
            issue_key=task.jira_key,
            summary=dict(summary),
            actor_username=actor.username,
        )
    return _manager_session_payload(session)


@app_router.get("/app/sessions/{chat_id}/ai-summary/jobs/{job_id}")
async def app_ai_summary_job_status(
    chat_id: int,
    job_id: str,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    if not session.current_task:
        raise HTTPException(status_code=400, detail="No active task")

    from services.voting_service.ai_jobs import get_job, job_public_view

    redis = request.app.state.web_redis
    job = await get_job(redis, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    if job.get("kind") != "session_ai_summary" or not str(job.get("resource_key", "")).startswith(f"session:{chat_id}:"):
        raise HTTPException(status_code=404, detail="AI job not found")
    return job_public_view(job)


@app_router.post("/app/sessions/{chat_id}/next")
async def app_next_task(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    before = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    was_completed = before.batch_completed

    def mutate(session: Session) -> None:
        if session.current_task:
            session.current_task_index += 1
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.batch_completed = False
        else:
            session.current_batch_started_at = None
            session.batch_completed = True
        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    # The active task just rolled over — backfill its description before
    # we broadcast so voters see the right spec block on the very first
    # post-advance render. In-place mutation; no second repo read.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.next", actor.username, "ok", {"chat_id": chat_id})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Last task completed",
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/skip")
async def app_skip_task(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    before = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    was_completed = before.batch_completed

    # Skip == advance to next task. Run the same mutation as `next` but only
    # record a single `skip` audit event so the audit log isn't polluted with
    # a paired `skip` + `next` for every skip click.
    def mutate(session: Session) -> None:
        if session.current_task:
            session.current_task_index += 1
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.batch_completed = False
        else:
            session.current_batch_started_at = None
            session.batch_completed = True
        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    # Same rationale as ``app_next_task`` — backfill before broadcasting
    # the new active task's state. In-place mutation; no second repo read.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.skip", actor.username, "ok", {"chat_id": chat_id})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Last task skipped",
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/completed/{task_id}/reopen")
async def app_reopen_completed_task(
    chat_id: int,
    task_id: str,
    body: ReopenCompletedRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = ReopenCompletedTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(
            request,
            "app.session.completed_reopen",
            actor.username,
            "failed",
            {"error": str(exc), "chat_id": chat_id, "task_id": task_id},
        )
        _raise_task_error(exc)
    await _ensure_current_task_description(request, chat_id, topic_id, session=result.session)
    await _publish_state(request, result.session)
    await _audit(
        request,
        "app.session.completed_reopen",
        actor.username,
        "ok",
        {"chat_id": chat_id, "task_id": task_id},
    )
    return _manager_session_payload(result.session)


@app_router.post("/app/sessions/{chat_id}/final-estimate")
async def app_set_final_estimate(
    chat_id: int,
    body: FinalEstimateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    def mutate(session: Session) -> Optional[str]:
        if not session.current_task:
            return "No active task."
        task = session.current_task
        if is_split_mode(session.estimation_mode):
            if not body.tracks:
                return "Final estimate requires per-track values for this estimation mode."
            config = get_mode_config(session.estimation_mode)
            allowed = {track.key for track in config.tracks}
            for track_key, value in body.tracks.items():
                if track_key not in allowed:
                    return f"Unknown track: {track_key}"
                if value < 0 or value > MAX_STORY_POINTS:
                    return f"Track value must be between 0 and {MAX_STORY_POINTS} SP"
            task.story_points_by_track = {key: int(value) for key, value in body.tracks.items()}
            task.story_points = None
        else:
            if body.value is None:
                return "Final estimate value is required."
            task.story_points = body.value
        task.touch()
        session.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    await _publish_state(request, session)
    await _audit(
        request,
        "app.session.final_estimate",
        actor.username,
        "ok",
        {
            "chat_id": chat_id,
            "value": body.value,
            "tracks": body.tracks,
        },
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/finish")
async def app_finish_session(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Finalize the session. Idempotent: safe to call after auto-next-on-last.

    Delegates to ``CloseSessionUseCase`` so the manager-driven finish and the
    CMS-driven force-close stay strictly in sync (they used to be two copies
    of the same mutator).
    """
    repo = request.app.state.repository
    before = await _get_repo_session(repo, chat_id, topic_id)
    was_completed = before.batch_completed

    use_case = CloseSessionUseCase(repo)
    session, completed = await use_case.execute(chat_id, topic_id)
    await _publish_state(request, session)
    await _audit(request, "app.session.finish", actor.username, "ok", {"chat_id": chat_id, "count": len(completed)})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Finish",
    )
    return _manager_session_payload(session)


class JiraStoryPointsSyncBody(BaseModel):
    skip_errors: bool = True


@app_router.post("/app/sessions/{chat_id}/jira-story-points/sync")
async def app_sync_jira_story_points(
    chat_id: int,
    body: JiraStoryPointsSyncBody,
    request: Request,
    topic_id: Optional[int] = Query(None),
    actor: CmsPrincipal = Depends(_require_manager_session),
):
    """Write final SP from the last finished batch into Jira (manager-initiated)."""
    session = await request.app.state.repository.get_session(chat_id, topic_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.last_batch:
        raise HTTPException(status_code=400, detail="Нет завершённого батча для синхронизации")

    from app.adapters.jira_service_client import JiraServiceHttpClient
    from app.usecases.update_jira_sp import UpdateJiraStoryPointsUseCase

    jira_client = JiraServiceHttpClient()
    try:
        use_case = UpdateJiraStoryPointsUseCase(
            jira_client,
            request.app.state.repository,
        )
        updated, failed, skipped = await use_case.execute(
            chat_id,
            topic_id,
            skip_errors=body.skip_errors,
        )
    finally:
        await jira_client.close()
    await _audit(
        request,
        "app.session.jira_sp_sync",
        actor.username,
        "ok" if not failed else "partial",
        {
            "chat_id": chat_id,
            "updated": updated,
            "failed": failed,
            "skipped_count": len(skipped),
        },
    )
    return {
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Session summary (used by the post-finish "results" page and CSV export)
# ---------------------------------------------------------------------------


def _summary_payload(
    session: Session,
    *,
    title: str,
    tasks_limit: Optional[int] = None,
) -> dict:
    """Build a detailed summary of the (current or just-finished) session.

    Works in both states:
    - phase == "complete": tasks read from ``session.last_batch``.
    - in-progress: completed slice = tasks_queue[:current_task_index].

    Aggregates (stats / participants) are ALWAYS computed across the full
    batch. ``tasks_limit`` lets callers (e.g. the Finished-page UI) inline
    only the first slice — they then page through the rest via
    ``/sessions/{chat_id}/summary/tasks``. CSV export does not pass the limit.
    """
    full_completed = _completed_in_batch(session)

    if tasks_limit is None:
        completed = full_completed
        completed_next_cursor: Optional[str] = None
    else:
        limit = max(1, min(tasks_limit, COMPLETED_MAX_LIMIT))
        completed = full_completed[:limit]
        completed_next_cursor = str(len(completed)) if len(completed) < len(full_completed) else None

    with_estimate = sum(
        1
        for entry in full_completed
        if entry["story_points"] is not None or entry.get("story_points_by_track")
    )
    consensus_count = sum(1 for entry in full_completed if entry["consensus"])
    total_voters = sum(entry["voter_count"] for entry in full_completed)
    total_story_points = sum(
        entry["story_points"]
        for entry in full_completed
        if entry["story_points"] is not None
    )
    total_story_points_by_track: dict[str, int] = {}
    for entry in full_completed:
        by_track = entry.get("story_points_by_track")
        if not isinstance(by_track, dict):
            continue
        for track_key, value in by_track.items():
            try:
                total_story_points_by_track[track_key] = total_story_points_by_track.get(track_key, 0) + int(value)
            except (TypeError, ValueError):
                continue

    # We persist a snapshot of the batch start time so finish/auto-next-on-last
    # don't erase it. Fall back to the current live timestamp for in-flight
    # sessions; final fallback is the first task's created_at for very old
    # sessions imported without timing data.
    started_at = (
        session.last_batch_started_at
        or session.current_batch_started_at
        or (session.last_batch[0].created_at if session.last_batch else None)
        or (session.tasks_queue[0].created_at if session.tasks_queue else None)
    )

    finished_at: Optional[str] = None
    if session.batch_completed and session.last_batch:
        finished_at = session.last_batch[0].completed_at

    # Stable participant roster across the session (manager + voters).
    participant_names = sorted(
        {participant.name for participant in session.participants.values() if participant.name},
        key=str.casefold,
    )
    participants_detailed = _participant_report_rows(session)
    estimation_payload = estimation_mode_payload(session.estimation_mode)

    return {
        "chat_id": session.chat_id,
        "topic_id": session.topic_id,
        "title": title,
        "phase": "complete" if session.batch_completed else ("in_progress" if full_completed else "fresh"),
        "started_at": started_at,
        "finished_at": finished_at,
        "tasks_queue_count": len(session.tasks_queue),
        **estimation_payload,
        "completed_tasks": completed,
        "completed_next_cursor": completed_next_cursor,
        "participants": participant_names,
        "participants_detailed": participants_detailed,
        "stats": {
            # Aggregates are always computed across the full batch so the UI
            # can show truthful totals before pulling every task into memory.
            "total_completed": len(full_completed),
            "with_estimate": with_estimate,
            "consensus_count": consensus_count,
            "votes_cast": total_voters,
            "total_story_points": total_story_points,
            "total_story_points_by_track": total_story_points_by_track,
        },
    }


@app_router.get("/app/sessions/{chat_id}/summary")
async def app_session_summary(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    tasks_limit: Optional[int] = Query(default=None, ge=1, le=COMPLETED_MAX_LIMIT),
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """JSON-summary for the Finished-session page. Pass ``tasks_limit`` to
    inline only the first slice of completed tasks; remaining pages are
    served by ``/summary/tasks``. Aggregate stats are always exact."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    return _summary_payload(session, title=resolved_title, tasks_limit=tasks_limit)


@app_router.get("/app/sessions/{chat_id}/summary/tasks")
async def app_session_summary_tasks(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(default=COMPLETED_DEFAULT_LIMIT, ge=1, le=COMPLETED_MAX_LIMIT),
    cursor: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Page through the completed-tasks list for the Finished-session report.

    Shape matches the existing CMS list contract (``items``,
    ``next_cursor``, ``limit``, ``total``) so the frontend can drop it into
    the shared ``useCmsList``-style hook."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _paginate_completed_in_batch(session, limit=limit, cursor=cursor)


def _format_distribution(distribution: dict[str, int]) -> str:
    """Render `{5: 3, 8: 1}` as `5×3, 8×1` (sorted by descending count)."""
    if not distribution:
        return ""
    pairs = sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{value}×{count}" for value, count in pairs)


def _format_track_totals(totals: dict[str, int]) -> str:
    if not totals:
        return "—"
    return ", ".join(f"{track}: {value}" for track, value in totals.items())


def _format_task_track_results(entry: dict) -> str:
    votes = entry.get("votes") or []
    if not votes:
        return "—"

    groups: dict[str, list[dict]] = {}
    for vote in votes:
        label = vote.get("track_label") or "Общая оценка"
        groups.setdefault(label, []).append(vote)

    parts: list[str] = []
    for track_label, track_votes in groups.items():
        track_key = next((vote.get("track") for vote in track_votes if vote.get("track")), None)
        final_value = None
        by_track = entry.get("story_points_by_track")
        if track_key and isinstance(by_track, dict):
            final_value = by_track.get(track_key)
        elif entry.get("story_points") is not None:
            final_value = entry.get("story_points")

        distribution: dict[str, int] = {}
        participant_bits: list[str] = []
        for vote in track_votes:
            raw_value = vote.get("value")
            # Explicit None/empty check: a vote of 0 is a legitimate value
            # and must not collapse into the "—" placeholder.
            value = "—" if raw_value is None or raw_value == "" else str(raw_value)
            distribution[value] = distribution.get(value, 0) + 1
            role = f" [{vote.get('role')}]" if vote.get("role") else ""
            participant_bits.append(f"{vote.get('name') or '—'}{role}: {value}")

        final_label = f"final {final_value} SP" if final_value is not None else "final —"
        parts.append(
            f"{track_label} ({final_label}; {_format_distribution(distribution) or '—'}; "
            + "; ".join(participant_bits)
            + ")"
        )

    return " | ".join(parts)


def _normalise_cell_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _join_summary_list(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(_normalise_cell_text(item) for item in value if _normalise_cell_text(item))


def _csv_ai_summary_fields(ai_summary: Optional[dict]) -> tuple[str, str, str, str, str, str, str, str, str]:
    """Flatten the persisted AI result for CSV cells.

    Keep this tied to the stored ``Task.ai_summary`` schema, not to the prompt
    text, so downloaded reports contain the actual model estimate.
    """
    if not ai_summary or not isinstance(ai_summary, dict):
        return "", "", "", "", "", "", "", "", ""

    description = _normalise_cell_text(ai_summary.get("description"))
    complexity = _normalise_cell_text(ai_summary.get("complexity"))
    methods = _normalise_cell_text(_join_summary_list(ai_summary.get("methods")))
    sp_dev = _normalise_cell_text(ai_summary.get("sp_dev"))
    sp_test = _normalise_cell_text(ai_summary.get("sp_test"))
    sp_final = _normalise_cell_text(ai_summary.get("sp_final"))
    confidence = _normalise_cell_text(ai_summary.get("confidence"))
    assumptions = _normalise_cell_text(_join_summary_list(ai_summary.get("assumptions")))
    estimation_model = _normalise_cell_text(ai_summary.get("estimation_model"))
    return description, complexity, methods, sp_dev, sp_test, sp_final, confidence, assumptions, estimation_model


def _download_filename(title: str, chat_id: int, extension: str) -> str:
    """ASCII fallback filename for Content-Disposition headers."""
    safe_title = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in (title or "session"))
    safe_title = "_".join(part for part in safe_title.split("_") if part) or "session"
    return f"REPORT_{safe_title}.{extension}"


def _content_disposition(title: str, chat_id: int, extension: str) -> str:
    filename = _download_filename(title, chat_id, extension)
    utf8_filename = f"REPORT_{title or 'session'}.{extension}"
    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(utf8_filename)}"


def _md_escape(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _md_link(label: object, url: object) -> str:
    clean_url = str(url or "").strip()
    clean_label = _md_escape(label)
    if not clean_url:
        return clean_label
    return f"[{clean_label or _md_escape(clean_url)}]({clean_url})"


def _markdown_report(summary: dict) -> str:
    stats = summary["stats"]
    lines = [
        f"# Planning Poker: {_md_escape(summary['title'])}",
        "",
        "## Summary",
        "",
        f"- **Estimation method:** {_md_escape(summary.get('estimation_mode_label') or 'SP')}",
        f"- **TOTAL SP:** {stats['total_story_points']}",
        f"- **Split SP totals:** {_md_escape(_format_track_totals(stats.get('total_story_points_by_track') or {}))}",
        f"- **Completed tasks:** {stats['total_completed']}",
        f"- **With final estimate:** {stats['with_estimate']} / {stats['total_completed']}",
        f"- **Consensus:** {stats['consensus_count']} / {stats['total_completed']}",
        f"- **Votes cast:** {stats['votes_cast']}",
        f"- **Started:** {_md_escape(summary['started_at'] or '—')}",
        f"- **Finished:** {_md_escape(summary['finished_at'] or '—')}",
        "",
        "## Participants",
        "",
        "| Participant | Role | Track |",
        "|---|---|---|",
    ]
    participants_detailed = summary.get("participants_detailed") or []
    if participants_detailed:
        for participant in participants_detailed:
            lines.append(
                f"| {_md_escape(participant.get('name'))} | "
                f"{_md_escape(participant.get('role') or '—')} | "
                f"{_md_escape(participant.get('track_label') or '—')} |"
            )
    else:
        lines.append("| — | — | — |")
    lines.extend([
        "",
        "## Results By Task",
        "",
    ])

    if not summary["completed_tasks"]:
        lines.extend(["No completed tasks.", ""])
        return "\n".join(lines).strip() + "\n"

    lines.extend([
        "| # | Task | Final SP | Results | Consensus | AI Description |",
        "|---:|---|---:|---|---|---|",
    ])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        task_label = entry["jira_key"] or entry["summary"]
        task = _md_link(task_label, entry.get("url"))
        if entry["jira_key"]:
            task = f"{task}<br />{_md_escape(entry['summary'])}"
        ai_description, _, _, _, _, ai_sp_final, _, _, _ = _csv_ai_summary_fields(entry.get("ai_summary"))
        ai_table_value = " — ".join(part for part in [ai_sp_final and f"{ai_sp_final} SP", ai_description] if part)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    task,
                    _md_escape(_final_estimate_label(entry)),
                    _md_escape(_format_task_track_results(entry)),
                    "yes" if entry["consensus"] else "no",
                    _md_escape(ai_table_value or "—"),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Vote Details", ""])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        title = entry["jira_key"] or entry["summary"]
        lines.extend([
            "---",
            "",
            f"### {idx}. {_md_escape(title)}",
            "",
            f"- **Final SP:** {_md_escape(_final_estimate_label(entry))}",
            f"- **Distribution:** {_md_escape(_format_distribution(entry['distribution']) or '—')}",
        ])
        if entry.get("url"):
            lines.append(f"- **Link:** {entry['url']}")
        if entry.get("ai_summary"):
            (
                ai_description,
                ai_complexity,
                ai_methods,
                ai_sp_dev,
                ai_sp_test,
                ai_sp_final,
                ai_confidence,
                ai_assumptions,
                ai_estimation_model,
            ) = _csv_ai_summary_fields(entry.get("ai_summary"))
            if ai_description:
                lines.append(f"- **AI description:** {_md_escape(ai_description)}")
            if ai_complexity:
                lines.append(f"- **AI complexity:** {_md_escape(ai_complexity)}")
            if ai_methods:
                lines.append(f"- **AI methods:** {_md_escape(ai_methods)}")
            if ai_sp_dev or ai_sp_test or ai_sp_final:
                lines.append(
                    "- **AI estimate:** "
                    + _md_escape(
                        ", ".join(
                            part
                            for part in [
                                ai_sp_dev and f"dev {ai_sp_dev} SP",
                                ai_sp_test and f"test {ai_sp_test} SP",
                                ai_sp_final and f"final {ai_sp_final} SP",
                            ]
                            if part
                        )
                    )
                )
            if ai_confidence:
                lines.append(f"- **AI confidence:** {_md_escape(ai_confidence)}")
            if ai_assumptions:
                lines.append(f"- **AI assumptions:** {_md_escape(ai_assumptions)}")
            if ai_estimation_model:
                lines.append(f"- **AI estimation model:** {_md_escape(ai_estimation_model)}")
        lines.extend(["", "| Track | Role | Participant | Vote |", "|---|---|---|---|"])
        if entry["votes"]:
            for vote in entry["votes"]:
                lines.append(
                    f"| {_md_escape(vote.get('track_label') or '—')} | "
                    f"{_md_escape(vote.get('role') or '—')} | "
                    f"{_md_escape(vote['name'])} | "
                    f"{_md_escape(vote['value'])} |"
                )
        else:
            lines.append("| — | — | — | — |")
        lines.extend(["", "---", ""])

    return "\n".join(lines).strip() + "\n"


def _csv_report(summary: dict) -> str:
    """Build an Excel/Sheets-friendly report with readable sections."""
    participant_names: list[str] = summary["participants"]
    stats = summary["stats"]
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["Planning Poker Report"])
    writer.writerow(["Title", summary["title"]])
    writer.writerow(["Chat ID", summary["chat_id"]])
    writer.writerow(["Topic ID", summary["topic_id"] if summary["topic_id"] is not None else "—"])
    writer.writerow(["Started", summary["started_at"] or "—"])
    writer.writerow(["Finished", summary["finished_at"] or "—"])
    writer.writerow(["Phase", summary["phase"]])
    writer.writerow(["Estimation Method", summary.get("estimation_mode_label") or "SP"])
    writer.writerow([])

    writer.writerow(["Summary"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["TOTAL SP", stats["total_story_points"]])
    writer.writerow(["Split SP totals", _format_track_totals(stats.get("total_story_points_by_track") or {})])
    writer.writerow(["Completed tasks", stats["total_completed"]])
    writer.writerow(["With final estimate", f"{stats['with_estimate']} / {stats['total_completed']}"])
    writer.writerow(["Consensus", f"{stats['consensus_count']} / {stats['total_completed']}"])
    writer.writerow(["Votes cast", stats["votes_cast"]])
    writer.writerow([])

    writer.writerow(["Participants"])
    participants_detailed = summary.get("participants_detailed") or []
    if participants_detailed:
        writer.writerow(["Name", "Role", "Track"])
        for participant in participants_detailed:
            writer.writerow([
                participant.get("name") or "—",
                participant.get("role") or "—",
                participant.get("track_label") or "—",
            ])
    elif participant_names:
        writer.writerow(["Name", "Role", "Track"])
        for name in participant_names:
            writer.writerow([name, "—", "—"])
    else:
        writer.writerow(["—"])
    writer.writerow([])

    writer.writerow(["Results By Task"])
    writer.writerow([
        "#",
        "Jira Key",
        "Task",
        "Final SP",
        "Estimation Method",
        "Results",
        "Consensus",
        "AI Description",
        "AI Complexity",
        "AI Methods",
        "AI SP Dev",
        "AI SP Test",
        "AI SP Final",
        "AI Confidence",
        "AI Assumptions",
        "AI Estimation Model",
        "URL",
        "Completed At",
    ])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        (
            ai_description,
            ai_complexity,
            ai_methods,
            ai_sp_dev,
            ai_sp_test,
            ai_sp_final,
            ai_confidence,
            ai_assumptions,
            ai_estimation_model,
        ) = _csv_ai_summary_fields(entry.get("ai_summary"))
        writer.writerow([
            idx,
            entry["jira_key"] or "",
            entry["summary"],
            _final_estimate_label(entry),
            summary.get("estimation_mode_label") or "SP",
            _format_task_track_results(entry),
            "yes" if entry["consensus"] else "no",
            ai_description or "—",
            ai_complexity or "—",
            ai_methods or "—",
            ai_sp_dev or "—",
            ai_sp_test or "—",
            ai_sp_final or "—",
            ai_confidence or "—",
            ai_assumptions or "—",
            ai_estimation_model or "—",
            entry["url"] or "",
            entry["completed_at"] or "",
        ])
    writer.writerow([])

    writer.writerow(["Vote Details"])
    writer.writerow(["Task #", "Jira Key", "Task", "Method", "Track", "Role", "Participant", "Vote"])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        if entry["votes"]:
            for vote in entry["votes"]:
                writer.writerow([
                    idx,
                    entry["jira_key"] or "",
                    entry["summary"],
                    summary.get("estimation_mode_label") or "SP",
                    vote.get("track_label") or "—",
                    vote.get("role") or "—",
                    vote["name"],
                    vote["value"],
                ])
        else:
            writer.writerow([idx, entry["jira_key"] or "", entry["summary"], summary.get("estimation_mode_label") or "SP", "—", "—", "—", "—"])

    return buf.getvalue()


@app_router.get("/app/sessions/{chat_id}/summary.csv")
async def app_session_summary_csv(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> StreamingResponse:
    """Export the session summary as a structured, human-readable CSV."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    summary = _summary_payload(session, title=resolved_title)
    csv_bytes = _csv_report(summary).encode("utf-8-sig")  # BOM so Excel detects UTF-8

    content_disposition = _content_disposition(summary["title"], chat_id, "csv")

    await _audit(
        request,
        "app.session.summary_export",
        actor.username,
        "ok",
        {"chat_id": chat_id, "format": "csv", "rows": len(summary["completed_tasks"])},
    )

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition},
    )


@app_router.get("/app/sessions/{chat_id}/summary.md")
async def app_session_summary_markdown(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> StreamingResponse:
    """Export a Confluence-friendly Markdown report for a planning session."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    summary = _summary_payload(session, title=resolved_title)
    markdown = _markdown_report(summary).encode("utf-8")

    await _audit(
        request,
        "app.session.summary_export",
        actor.username,
        "ok",
        {"chat_id": chat_id, "format": "md", "rows": len(summary["completed_tasks"])},
    )

    return StreamingResponse(
        iter([markdown]),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(summary["title"], chat_id, "md")},
    )
