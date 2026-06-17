"""Orchestrate Telegram alerts when a planning session is finished."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Request

from app.domain.session import Session
from services.voting_service._http_shared import CmsPrincipal
from services.voting_service.telegram_notifier import (
    build_session_finish_caption,
    format_duration,
    report_filename,
    send_session_finish_document,
)

logger = logging.getLogger(__name__)


def _actor_label(actor: CmsPrincipal) -> str:
    display = (actor.display_name or "").strip()
    if display:
        return display
    return actor.username


def _team_name_from_row(stored_row: Optional[dict]) -> str:
    if not stored_row:
        return "Без команды"
    team = stored_row.get("team")
    if isinstance(team, dict):
        name = (team.get("name") or "").strip()
        if name:
            return name
    return "Без команды"


def _report_url(chat_id: int) -> Optional[str]:
    base = (os.getenv("WEB_UI_URL") or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/cms/sessions/{chat_id}/report"


async def maybe_notify_session_finished(
    request: Request,
    session: Session,
    *,
    was_completed: bool,
    actor: CmsPrincipal,
    close_method: str,
) -> None:
    """Send a Telegram alert with Markdown report when a session newly completes.

    Idempotent: skips when the session was already completed before this close.
    Best-effort: Telegram failures are logged and never propagate.
    """
    if was_completed or not session.batch_completed:
        return

    # Lazy import avoids a circular dependency with app_api at module load.
    from services.voting_service.app_api import (
        _markdown_report,
        _resolve_session_title,
        _stored_session_row,
        _summary_payload,
    )

    try:
        stored_row = await _stored_session_row(request, session.chat_id, session.topic_id)
        stored_title = (stored_row.get("title") or "").strip() if stored_row else None
        resolved_title = _resolve_session_title(None, stored_title)
        summary = _summary_payload(session, title=resolved_title)
        markdown = _markdown_report(summary).encode("utf-8")
        duration = format_duration(summary.get("started_at"), summary.get("finished_at"))
        caption = build_session_finish_caption(
            title=resolved_title,
            finished_by=_actor_label(actor),
            team_name=_team_name_from_row(stored_row),
            duration=duration,
            close_method=close_method,
            summary=summary,
            report_url=_report_url(session.chat_id),
        )
        http_session = getattr(request.app.state, "http_session", None)
        await send_session_finish_document(
            http_session,
            caption=caption,
            filename=report_filename(resolved_title),
            content=markdown,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session finish Telegram alert failed chat_id=%s topic_id=%s err=%r",
            session.chat_id,
            session.topic_id,
            exc,
        )
