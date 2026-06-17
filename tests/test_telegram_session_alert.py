"""Tests for Telegram session-finish alerts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.session import Session
from app.domain.task import Task
from services.voting_service._http_shared import CmsPrincipal
from services.voting_service.session_finish_notify import maybe_notify_session_finished
from services.voting_service.telegram_notifier import (
    build_session_finish_caption,
    format_duration,
    report_filename,
    send_session_finish_document,
    telegram_configured,
)


def _summary_fixture() -> dict:
    return {
        "title": "Sprint 42",
        "started_at": "2026-06-10T10:00:00+00:00",
        "finished_at": "2026-06-10T11:30:00+00:00",
        "participants": ["Alice", "Bob"],
        "stats": {
            "total_completed": 5,
            "total_story_points": 21,
            "consensus_count": 3,
            "with_estimate": 5,
            "votes_cast": 10,
        },
        "completed_tasks": [],
    }


def test_format_duration_hours_and_minutes() -> None:
    assert format_duration("2026-06-10T10:00:00+00:00", "2026-06-10T11:30:00+00:00") == "1ч 30м"


def test_format_duration_missing_values() -> None:
    assert format_duration(None, "2026-06-10T11:30:00+00:00") == "—"


def test_report_filename_sanitizes_title() -> None:
    assert report_filename("iGaming RIP · Sprint 42") == "REPORT_iGaming_RIP_Sprint_42.md"


def test_build_session_finish_caption_includes_core_fields() -> None:
    caption = build_session_finish_caption(
        title="Sprint 42",
        finished_by="Павел",
        team_name="iGaming RIP",
        duration="1ч 30м",
        close_method="Finish",
        summary=_summary_fixture(),
        report_url="https://planning.example.com/cms/sessions/101/report",
    )
    assert "Сессия завершена" in caption
    assert "Sprint 42" in caption
    assert "Павел" in caption
    assert "iGaming RIP" in caption
    assert "1ч 30м" in caption
    assert "Total SP" in caption
    assert "Открыть отчёт" in caption


@pytest.mark.asyncio
async def test_send_session_finish_document_skips_without_env(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram_configured() is False

    session = AsyncMock()
    await send_session_finish_document(
        session,
        caption="test",
        filename="report.md",
        content=b"# report",
    )
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_notify_skips_when_session_was_already_completed() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    actor = CmsPrincipal(
        id=1,
        username="admin",
        display_name="Admin",
        is_superuser=True,
        permissions=frozenset(),
        roles=(),
        pages=(),
        team_ids=frozenset(),
        teams=(),
    )
    session = Session(chat_id=10, topic_id=None)
    session.batch_completed = True

    with patch(
        "services.voting_service.session_finish_notify.send_session_finish_document",
        new_callable=AsyncMock,
    ) as send_mock:
        await maybe_notify_session_finished(
            request,
            session,
            was_completed=True,
            actor=actor,
            close_method="Finish",
        )
        send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_notify_sends_when_session_newly_completed(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("WEB_UI_URL", "https://planning.example.com")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(http_session=AsyncMock())),
    )
    actor = CmsPrincipal(
        id=1,
        username="admin",
        display_name="Павел",
        is_superuser=True,
        permissions=frozenset(),
        roles=(),
        pages=(),
        team_ids=frozenset(),
        teams=(),
    )
    session = Session(chat_id=101, topic_id=None)
    session.batch_completed = True
    session.last_batch = [Task(jira_key="BB-1", summary="Login", story_points=5)]
    session.last_batch[0].completed_at = "2026-06-10T11:30:00+00:00"
    session.last_batch_started_at = "2026-06-10T10:00:00+00:00"

    stored_row = {
        "title": "Sprint 42",
        "team": {"id": 1, "name": "iGaming RIP", "slug": "igaming-rip"},
    }

    with patch(
        "services.voting_service.app_api._stored_session_row",
        new_callable=AsyncMock,
        return_value=stored_row,
    ), patch(
        "services.voting_service.session_finish_notify.send_session_finish_document",
        new_callable=AsyncMock,
    ) as send_mock:
        await maybe_notify_session_finished(
            request,
            session,
            was_completed=False,
            actor=actor,
            close_method="CMS force-close",
        )

    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["filename"].endswith(".md")
    assert "Sprint 42" in kwargs["caption"]
    assert "Павел" in kwargs["caption"]
    assert "CMS force-close" in kwargs["caption"]
    assert "iGaming RIP" in kwargs["caption"]
    assert b"Planning Poker" in kwargs["content"]
