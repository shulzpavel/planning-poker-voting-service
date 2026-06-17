"""Regression tests for the retro AI prompt + validator.

Pins the JSON schema the system prompt promises and the strictness of the
validator, so future prompt edits can't silently break the UI contract.
No network: only the pure prompt/validator helpers are exercised.
"""

import pytest

from app.domain.retro import Retrospective, RetroSection
from services.voting_service.retro_ai_llm import (
    LlmRetroError,
    _build_retro_context,
    _system_prompt,
    _validate_retro_payload,
)


def test_system_prompt_pins_schema_keys():
    prompt = _system_prompt()
    for fragment in (
        '"mood"',
        '"summary"',
        '"problems"',
        '"recommendations"',
        '"patterns"',
        '"suggested_action_items"',
        "JSON",
        "недоверенным пользовательским вводом",
    ):
        assert fragment in prompt


def test_build_context_includes_cards_and_votes():
    retro = Retrospective(
        retro_id=1,
        title="Sprint 42",
        sections=[RetroSection(section_id="proc", title="По процессам")],
    )
    retro.open_section("proc", deadline=None)
    card = retro.add_card(
        card_id="c1",
        section_id="proc",
        text="Долгие код-ревью",
        author_id=-1,
        author_name="a@x.test",
        created_at="2026-01-01T00:00:00",
    )
    retro.start_voting()
    retro.toggle_vote("c1", user_id=-2)

    context = _build_retro_context(retro)
    assert "Sprint 42" in context
    assert "По процессам" in context
    assert "Долгие код-ревью" in context
    assert "1 голосов" in context
    # Author must never leak into the AI context.
    assert "a@x.test" not in context
    assert card.author_name == "a@x.test"


def test_validator_accepts_minimal_valid_payload():
    out = _validate_retro_payload({
        "mood": "high",
        "summary": "Команда в целом довольна.",
        "highlights": ["Хорошая коммуникация"],
        "problems": [{"title": "Долгие ревью", "severity": "high", "detail": "PR висят днями"}],
        "patterns": ["Узкое место на ревью"],
        "recommendations": [{"text": "Ввести SLA на ревью 4 часа", "impact": "high"}],
        "risks": ["Срыв сроков"],
        "suggested_action_items": ["Договориться про SLA на ревью"],
    })
    assert out["mood"] == "high"
    assert out["problems"][0]["severity"] == "high"
    assert out["recommendations"][0]["impact"] == "high"
    assert out["source"] == "anthropic"
    assert "generated_at" in out


def test_validator_normalizes_bad_enums_and_string_recommendations():
    out = _validate_retro_payload({
        "summary": "ok",
        "mood": "garbage",
        "problems": [{"title": "X", "severity": "nope"}],
        "recommendations": ["просто строка без impact"],
    })
    assert out["mood"] == "neutral"
    assert out["problems"][0]["severity"] == "medium"
    assert out["recommendations"][0] == {"text": "просто строка без impact", "impact": "medium"}


def test_validator_requires_summary():
    with pytest.raises(LlmRetroError):
        _validate_retro_payload({"problems": [{"title": "x"}], "recommendations": [{"text": "y"}]})


def test_validator_defaults_empty_problems_and_recommendations_for_positive_retros():
    out = _validate_retro_payload({"summary": "s", "problems": [], "recommendations": []})
    assert out["problems"][0]["severity"] == "low"
    assert out["recommendations"][0]["impact"] == "low"
