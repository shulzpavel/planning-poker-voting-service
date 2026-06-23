"""Best-effort Telegram digest after standup AI summary is saved."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp

from services.voting_service.telegram_notifier import html_escape, send_telegram_message

logger = logging.getLogger(__name__)


def standup_detail_url(standup_id: int) -> Optional[str]:
    base = os.getenv("WEB_UI_URL", "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/cms/standups/{standup_id}"


def build_standup_digest_message(*, standup: dict[str, Any], ai_summary: dict[str, Any]) -> str:
    team = str(standup.get("team_name") or "Команда").strip()
    meeting_date = str(standup.get("meeting_date") or "").strip()
    summary = str(ai_summary.get("summary") or "").strip()
    lines = [
        "📋 <b>Дейлик</b>",
        f"<b>{html_escape(team)}</b> · {html_escape(meeting_date)}",
        "",
        html_escape(summary),
    ]

    changed = [item for item in (ai_summary.get("changed") or []) if isinstance(item, str) and item.strip()]
    if changed:
        lines.extend(["", "<b>📈 Изменилось</b>"])
        lines.extend(f"• {html_escape(item)}" for item in changed[:5])

    unchanged = [item for item in (ai_summary.get("unchanged") or []) if isinstance(item, str) and item.strip()]
    if unchanged:
        lines.extend(["", "<b>⏸ Без изменений</b>"])
        lines.extend(f"• {html_escape(item)}" for item in unchanged[:5])

    watch = [item for item in (ai_summary.get("watch") or []) if isinstance(item, str) and item.strip()]
    if watch:
        lines.extend(["", "<b>👀 На что смотреть</b>"])
        lines.extend(f"• {html_escape(item)}" for item in watch[:5])

    done = [item for item in (ai_summary.get("done") or []) if isinstance(item, str) and item.strip()]
    if done:
        lines.extend(["", "<b>✅ Сделано</b>"])
        lines.extend(f"• {html_escape(item)}" for item in done[:6])

    in_progress = ai_summary.get("in_progress")
    if isinstance(in_progress, list) and in_progress:
        lines.extend(["", "<b>🔄 В работе</b>"])
        for row in in_progress[:8]:
            if not isinstance(row, dict):
                continue
            person = str(row.get("person") or "Участник").strip()
            tasks = [task for task in (row.get("tasks") or []) if isinstance(task, str) and task.strip()]
            if not tasks:
                continue
            lines.append(f"• <b>{html_escape(person)}</b>: {html_escape('; '.join(tasks[:3]))}")

    blockers = ai_summary.get("blockers")
    if isinstance(blockers, list) and blockers:
        lines.extend(["", "<b>🚧 Блокеры</b>"])
        for row in blockers[:5]:
            if not isinstance(row, dict):
                continue
            person = str(row.get("person") or "").strip()
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            prefix = f"{html_escape(person)}: " if person else ""
            lines.append(f"• {prefix}{html_escape(text)}")

    risks = [item for item in (ai_summary.get("risks") or []) if isinstance(item, str) and item.strip()]
    if risks:
        lines.extend(["", "<b>⚠️ Риски</b>"])
        lines.extend(f"• {html_escape(item)}" for item in risks[:4])

    focus = [item for item in (ai_summary.get("focus") or []) if isinstance(item, str) and item.strip()]
    if focus:
        lines.extend(["", "<b>👀 Фокус</b>"])
        lines.extend(f"• {html_escape(item)}" for item in focus[:4])

    url = standup_detail_url(int(standup["id"]))
    if url:
        lines.extend(["", f'<a href="{html_escape(url)}">Открыть в CMS</a>'])
    return "\n".join(lines)


async def maybe_notify_standup_published(
    http_session: Optional[aiohttp.ClientSession],
    *,
    standup: dict[str, Any],
    ai_summary: dict[str, Any],
) -> dict[str, Any]:
    """Send digest to Telegram once. Returns summary with ``telegram_sent_at`` when sent."""
    if ai_summary.get("telegram_sent_at"):
        return ai_summary
    message = build_standup_digest_message(standup=standup, ai_summary=ai_summary)
    sent = await send_telegram_message(http_session, text=message)
    if not sent:
        return ai_summary
    from datetime import datetime, timezone

    updated = dict(ai_summary)
    updated["telegram_sent_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("standup Telegram digest sent standup_id=%s", standup.get("id"))
    return updated
