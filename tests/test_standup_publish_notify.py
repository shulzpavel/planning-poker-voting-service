"""Tests for standup Telegram digest formatting."""

from unittest.mock import AsyncMock

import pytest

from services.voting_service.standup_publish_notify import (
    build_standup_digest_message,
    maybe_notify_standup_published,
)


def test_build_standup_digest_message_includes_sections() -> None:
    text = build_standup_digest_message(
        standup={"id": 7, "team_name": "Alpha", "meeting_date": "2026-06-23"},
        ai_summary={
            "summary": "Команда закрыла одну задачу.",
            "changed": ["FLEX-1 закрыта"],
            "unchanged": ["FLEX-2 без движения"],
            "watch": ["Проверить QA"],
            "done": ["FLEX-1"],
            "in_progress": [{"person": "Bob", "tasks": ["FLEX-2"]}],
            "blockers": [{"person": "Alice", "text": "Нет доступа", "severity": "high"}],
            "risks": ["Срок"],
            "focus": ["Разблокировать DevOps"],
        },
    )
    assert "Дейлик" in text
    assert "Alpha" in text
    assert "Изменилось" in text
    assert "На что смотреть" in text
    assert "Сделано" in text
    assert "Блокеры" in text
    assert "FLEX-1" in text


@pytest.mark.asyncio
async def test_maybe_notify_skips_when_already_sent(monkeypatch) -> None:
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "services.voting_service.standup_publish_notify.send_telegram_message",
        send,
    )
    summary = {"summary": "ok", "telegram_sent_at": "2026-06-23T10:00:00+00:00"}
    out = await maybe_notify_standup_published(
        AsyncMock(),
        standup={"id": 1, "team_name": "Alpha", "meeting_date": "2026-06-23"},
        ai_summary=summary,
    )
    assert out["telegram_sent_at"]
    send.assert_not_called()
