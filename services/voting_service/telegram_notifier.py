"""Telegram alerts for production events (session finish, etc.).

Uses the same ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` env vars as the
CI/deploy pipeline notifications.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())


def html_escape(value: object) -> str:
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_iso_timestamp(value: str) -> Optional[datetime]:
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_duration(started_at: Optional[str], finished_at: Optional[str]) -> str:
    """Human-readable duration between two ISO timestamps."""
    if not started_at or not finished_at:
        return "—"
    start = _parse_iso_timestamp(started_at)
    end = _parse_iso_timestamp(finished_at)
    if start is None or end is None:
        return "—"
    delta = end - start
    if delta.total_seconds() < 0:
        return "—"
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}ч {minutes}м"
    if minutes > 0:
        return f"{minutes}м"
    return "<1м"


def report_filename(title: str) -> str:
    safe_title = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in (title or "session")
    )
    safe_title = "_".join(part for part in safe_title.split("_") if part) or "session"
    return f"REPORT_{safe_title}.md"


def build_session_finish_caption(
    *,
    title: str,
    finished_by: str,
    team_name: str,
    duration: str,
    close_method: str,
    summary: dict[str, Any],
    report_url: Optional[str] = None,
) -> str:
    stats = summary.get("stats") or {}
    total_completed = stats.get("total_completed", 0)
    consensus_count = stats.get("consensus_count", 0)
    participant_count = len(summary.get("participants") or [])

    lines = [
        "✅ <b>Сессия завершена</b>",
        "",
        f"<b>Название:</b> {html_escape(title)}",
        f"<b>Завершил:</b> {html_escape(finished_by)}",
        f"<b>Способ:</b> {html_escape(close_method)}",
        f"<b>Команда:</b> {html_escape(team_name)}",
        f"<b>Длительность:</b> {html_escape(duration)}",
        "",
        f"<b>Задач:</b> {total_completed}",
        f"<b>Total SP:</b> {stats.get('total_story_points', 0)}",
        f"<b>Участников:</b> {participant_count}",
        f"<b>Consensus:</b> {consensus_count} / {total_completed}",
    ]
    if report_url:
        lines.append(f'<a href="{html_escape(report_url)}">Открыть отчёт</a>')
    return "\n".join(lines)


async def send_session_finish_document(
    http_session: Optional[aiohttp.ClientSession],
    *,
    caption: str,
    filename: str,
    content: bytes,
) -> None:
    """Best-effort Telegram document upload. Never raises to callers."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("Telegram not configured; skipping session finish alert")
        return
    if http_session is None:
        logger.warning("http_session missing; skipping session finish alert")
        return

    url = f"{TELEGRAM_API_BASE.format(token=token)}/sendDocument"
    form = aiohttp.FormData()
    form.add_field("chat_id", chat_id)
    form.add_field("caption", caption)
    form.add_field("parse_mode", "HTML")
    form.add_field("disable_web_page_preview", "true")
    form.add_field(
        "document",
        content,
        filename=filename,
        content_type="text/markdown",
    )

    try:
        async with http_session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status >= 400:
                body = await response.text()
                logger.warning(
                    "Telegram sendDocument failed status=%s body=%s",
                    response.status,
                    body[:500],
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram sendDocument error: %r", exc)
