"""Tests for AI summary Jira ADF export."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.voting_service.ai_summary_jira_export import (
    build_ai_summary_comment_adf,
    compute_summary_hash,
    export_ai_summary_to_jira,
    should_skip_jira_export,
)

_SAMPLE_SUMMARY: dict[str, Any] = {
    "description": "Добавить экспорт оценки в Jira.",
    "methods": ["API", "БД", "Тесты"],
    "complexity": "Средняя сложность из-за интеграции с Jira ADF.",
    "sp_dev": 5,
    "sp_test": 3,
    "sp_final": 5,
    "scale_label": "5 SP — средняя",
    "confidence": "medium",
    "assumptions": ["Нужны права на комментарии в Jira"],
    "generated_at": "2026-06-15T12:00:00Z",
}


def test_build_adf_contains_key_sections() -> None:
    adf = build_ai_summary_comment_adf(
        _SAMPLE_SUMMARY,
        issue_key="PROJ-42",
        task_summary="Экспорт AI summary",
    )
    assert adf["type"] == "doc"
    content = adf["content"]
    headings = [
        block["content"][0]["text"]
        for block in content
        if block.get("type") == "heading"
    ]
    assert "AI-оценка · Planning Poker" in headings
    assert "Описание" in headings
    assert "Сложность" in headings
    assert "Методы / зоны внимания" in headings
    assert "Допущения" in headings
    assert any(block.get("type") == "panel" for block in content)


def test_summary_hash_is_stable() -> None:
    first = compute_summary_hash(_SAMPLE_SUMMARY)
    second = compute_summary_hash(dict(_SAMPLE_SUMMARY))
    assert first == second
    changed = dict(_SAMPLE_SUMMARY)
    changed["sp_final"] = 8
    assert compute_summary_hash(changed) != first


def test_should_skip_when_hash_matches_ok_export() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "ok",
        "hash": compute_summary_hash(summary),
        "comment_id": "10001",
    }
    assert should_skip_jira_export(summary) is True


def test_should_not_skip_when_hash_changed() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "ok",
        "hash": "stale-hash",
        "comment_id": "10001",
    }
    assert should_skip_jira_export(summary) is False


def test_should_not_skip_when_previous_export_failed() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "error",
        "hash": compute_summary_hash(summary),
        "error": "timeout",
    }
    assert should_skip_jira_export(summary) is False


@pytest.mark.asyncio
async def test_export_posts_new_comment() -> None:
    client = AsyncMock()
    client.add_issue_comment_adf.return_value = {"success": True, "comment_id": "12345"}

    result = await export_ai_summary_to_jira(
        client,
        issue_key="PROJ-42",
        summary=_SAMPLE_SUMMARY,
        task_summary="Экспорт AI summary",
    )

    assert result["status"] == "ok"
    assert result["comment_id"] == "12345"
    assert result["hash"] == compute_summary_hash(_SAMPLE_SUMMARY)
    client.add_issue_comment_adf.assert_awaited_once()
    client.update_issue_comment_adf.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_updates_existing_comment_when_hash_changed() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "ok",
        "hash": "old-hash",
        "comment_id": "999",
    }
    client = AsyncMock()
    client.update_issue_comment_adf.return_value = {"success": True, "comment_id": "999"}

    result = await export_ai_summary_to_jira(
        client,
        issue_key="PROJ-42",
        summary=summary,
    )

    assert result["status"] == "ok"
    assert result["comment_id"] == "999"
    client.update_issue_comment_adf.assert_awaited_once()
    client.add_issue_comment_adf.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_skips_when_already_exported() -> None:
    summary = dict(_SAMPLE_SUMMARY)
    summary["jira_export"] = {
        "status": "ok",
        "hash": compute_summary_hash(summary),
        "comment_id": "10001",
        "exported_at": "2026-06-15T12:00:00Z",
    }
    client = AsyncMock()

    result = await export_ai_summary_to_jira(client, issue_key="PROJ-42", summary=summary)

    assert result["comment_id"] == "10001"
    client.add_issue_comment_adf.assert_not_awaited()
    client.update_issue_comment_adf.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_records_error_without_raising() -> None:
    client = AsyncMock()
    client.add_issue_comment_adf.side_effect = RuntimeError("Jira down")

    result = await export_ai_summary_to_jira(client, issue_key="PROJ-42", summary=_SAMPLE_SUMMARY)

    assert result["status"] == "error"
    assert "Jira down" in str(result.get("error"))
