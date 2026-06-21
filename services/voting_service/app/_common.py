"""Shared app API router, models, and helpers."""

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

