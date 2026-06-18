"""Tests for scope AI summary Jira ADF export."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.voting_service.scope_ai_jira_export import (
    build_scope_ai_summary_comment_adf,
    compute_scope_summary_hash,
    export_scope_ai_summary_to_jira,
    normalize_plan_epic_key,
    should_skip_scope_jira_export,
)

_SAMPLE_SUMMARY: dict[str, Any] = {
    "health": "yellow",
    "summary": "Месяц идёт с рисками — нужно закрыть открытые вопросы.",
    "buffer_status": "tight",
    "whats_critical": ["Перегруз тестирования"],
    "whats_bad": ["Много внеплана"],
    "recommendations": [{"text": "Согласовать intake", "impact": "high"}],
    "focus_now": ["Обсудить FLEX-100"],
    "blockers": [{"title": "Пауза по API", "severity": "high", "detail": "Ждём решения", "issue_keys": ["FLEX-9"]}],
    "capacity_assessment": "Запас почти исчерпан.",
    "report_assessment": "План в работе.",
    "generated_at": "2026-06-18T10:00:00Z",
}


def test_normalize_plan_epic_key() -> None:
    assert normalize_plan_epic_key(" flex-42 ") == "FLEX-42"
    assert normalize_plan_epic_key("not-a-key") == ""
    assert normalize_plan_epic_key("") == ""


def test_build_scope_adf_contains_key_sections() -> None:
    adf = build_scope_ai_summary_comment_adf(
        _SAMPLE_SUMMARY,
        issue_key="FLEX-1",
        board_name="Test board",
        board_month="2026-06",
    )
    assert adf["type"] == "doc"
    headings = [
        block["content"][0]["text"]
        for block in adf["content"]
        if block.get("type") == "heading"
    ]
    assert "AI-сводка · Отчёт месяца" in headings
    assert "Главный вывод" in headings
    assert "Что сделать на этой неделе" in headings
    assert "Обсудить на ближайшей встрече" in headings


def test_should_skip_scope_jira_export_when_hash_matches() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "ok",
        "hash": compute_scope_summary_hash(summary),
        "comment_id": "10001",
    }
    assert should_skip_scope_jira_export(summary) is True


@pytest.mark.asyncio
async def test_export_scope_posts_new_comment() -> None:
    client = AsyncMock()
    client.add_issue_comment_adf.return_value = {"comment_id": "20001"}
    summary = dict(_SAMPLE_SUMMARY)

    result = await export_scope_ai_summary_to_jira(
        client,
        issue_key="FLEX-1",
        summary=summary,
        board_name="Board",
        board_month="2026-06",
    )

    assert result["status"] == "ok"
    assert result["comment_id"] == "20001"
    client.add_issue_comment_adf.assert_awaited_once()
