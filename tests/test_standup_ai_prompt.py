"""Regression tests for standup AI prompt + validator."""

import pytest

from services.voting_service.standup_ai_llm import (
    LlmStandupError,
    _build_standup_context,
    _parse_and_validate,
    _system_prompt,
    _validate_standup_payload,
)
from services.voting_service.ai_summary_llm import parse_llm_json_object


def test_system_prompt_pins_schema_keys() -> None:
    prompt = _system_prompt()
    for fragment in (
        '"summary"',
        '"changed"',
        '"unchanged"',
        '"watch"',
        '"done"',
        '"in_progress"',
        '"blockers"',
        '"risks"',
        '"focus"',
        "JSON",
        "PREVIOUS_STANDUP",
        "CURRENT_STANDUP",
        "недоверенный ввод",
    ):
        assert fragment in prompt


def test_build_context_includes_previous_and_current_sections() -> None:
    standup = {
        "team_name": "Alpha",
        "meeting_date": "2026-06-24",
        "payload": {"participants": [], "notes": ""},
    }
    previous = {
        "team_name": "Alpha",
        "meeting_date": "2026-06-23",
        "payload": {"participants": [], "notes": "вчера"},
    }
    context = _build_standup_context(standup, previous)
    assert "# PREVIOUS_STANDUP" in context
    assert "# CURRENT_STANDUP" in context
    assert "2026-06-23" in context
    assert "2026-06-24" in context


def test_build_context_includes_tracks_and_participants() -> None:
    standup = {
        "team_name": "Alpha",
        "meeting_date": "2026-06-23",
        "payload": {
            "facilitator": "Lead",
            "notes": "",
            "participants": [
                {
                    "name": "Alice",
                    "role": "front",
                    "present": True,
                    "items": [
                        {
                            "track": "yesterday",
                            "task_title": "Закрыла FLEX-1",
                            "jira_key": "FLEX-1",
                            "status": "done",
                            "comment": "",
                        },
                        {
                            "track": "blocker",
                            "task_title": "Ждём DevOps",
                            "jira_key": "",
                            "status": "blocked",
                            "comment": "нет доступа",
                        },
                    ],
                }
            ],
        },
    }
    context = _build_standup_context(standup)
    assert "Alpha" in context
    assert "Alice" in context
    assert "Что сделано" in context
    assert "Блокер" in context
    assert "FLEX-1" in context


def test_validator_accepts_minimal_valid_payload() -> None:
    out = _validate_standup_payload({
        "summary": "Команда движется по плану.",
        "changed": ["Закрыли FLEX-1"],
        "unchanged": ["FLEX-2 всё ещё в работе"],
        "watch": ["Спросить у QA статус"],
        "done": ["Закрыли FLEX-1"],
        "in_progress": [{"person": "Bob", "tasks": ["FLEX-2"]}],
        "blockers": [{"person": "Alice", "text": "Нет доступа", "severity": "high"}],
        "risks": ["Срыв срока"],
        "focus": ["Проверить блокер"],
    })
    assert out["summary"].startswith("Команда")
    assert out["changed"][0].startswith("Закрыли")
    assert out["watch"][0].startswith("Спросить")
    assert out["blockers"][0]["severity"] == "high"
    assert out["source"] == "anthropic"
    assert "generated_at" in out


def test_validator_requires_summary() -> None:
    with pytest.raises(LlmStandupError):
        _validate_standup_payload({"done": ["x"]})


def test_parse_and_validate_accepts_fenced_json() -> None:
    out = _parse_and_validate(
        '```json\n{"summary": "ok", "changed": [], "unchanged": [], "watch": ["x"], '
        '"done": [], "in_progress": [], "blockers": [], "risks": [], "focus": []}\n```'
    )
    assert out["summary"] == "ok"
    assert out["watch"] == ["x"]


def test_parse_llm_json_object_prefills_standup_body() -> None:
    payload = parse_llm_json_object('"summary": "ok", "changed": [], "unchanged": [], "watch": []}')
    assert payload["summary"] == "ok"
