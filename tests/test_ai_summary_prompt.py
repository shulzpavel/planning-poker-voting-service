"""Regression tests for the Anthropic prompt used by AI summaries.

The system prompt encodes the team's Story Points rubric. The validator
(:func:`_validate_summary_payload`) already rejects malformed responses,
but if a refactor accidentally drops a rule from the prompt (e.g. the
``SP = max(SP dev, SP test)`` formula or the Fibonacci scale) the model
will silently start producing different — but still well-formed — output.
These tests pin the rules we care about so that does not happen.
"""

import pytest

from services.voting_service.ai_summary_llm import (
    STORY_POINT_SCALE,
    _parse_llm_json_payload,
    _system_prompt,
    _validate_summary_payload,
)


def test_prompt_includes_max_formula() -> None:
    prompt = _system_prompt()
    assert "max(sp_dev, sp_test)" in prompt or "max(SP dev, SP test)" in prompt


def test_prompt_lists_full_fibonacci_scale() -> None:
    prompt = _system_prompt()
    for value in STORY_POINT_SCALE:
        assert f" {value} SP" in prompt or f"{value}|" in prompt or f": {value}\n" in prompt, (
            f"prompt must reference SP scale value {value}, got: {prompt!r}"
        )


@pytest.mark.parametrize(
    "rule_substring",
    [
        "Story Points",  # the term itself
        "Фибоначчи",  # scale name
        "max(sp_dev, sp_test)",  # final formula
        "18 SP",  # decomposition trigger
        "Эпики",  # epics-not-estimated rule
        "spike",  # research-task escape hatch
        "ретро",  # what NOT to estimate
        "инженерную сложность",  # estimate task implementation complexity, not doc size
        "не длину документации",  # long Jira/Confluence body alone must not inflate SP
        "edge cases",  # implementation complexity signal
        "low",  # confidence enum
        "high",  # confidence enum
    ],
)
def test_prompt_contains_rubric_keywords(rule_substring: str) -> None:
    """Each rule the team relies on must be physically present in the prompt;
    fuzz-matching is intentional — exact wording can drift, but the key
    concepts must survive any refactor."""
    assert rule_substring in _system_prompt(), (
        f"prompt is missing rubric concept {rule_substring!r}"
    )


def test_prompt_demands_strict_json_object() -> None:
    """The validator rejects markdown fences and non-object payloads, so the
    prompt must explicitly forbid them."""
    prompt = _system_prompt()
    assert "JSON" in prompt
    assert "schema" in prompt.lower() or "схеме" in prompt
    assert "Большое описание само по себе не повышает SP" in prompt


def test_validator_still_accepts_well_formed_response() -> None:
    """Sanity check: the response shape the prompt asks for is the one the
    validator accepts. Catches drift between the prompt and the validator."""
    payload = _validate_summary_payload(
        {
            "description": "Описание задачи на пару предложений.",
            "methods": ["API", "БД", "Тесты"],
            "complexity": "Средняя сложность из-за двух интеграций.",
            "sp_dev": 5,
            "sp_test": 3,
            "sp_final": 5,
            "scale_label": "5 SP — средняя",
            "confidence": "medium",
            "assumptions": ["Требуется уточнение по правам доступа"],
        }
    )
    assert payload["sp_final"] == 5
    assert payload["estimation_model"] == "max(sp_dev, sp_test)"
    assert payload["confidence"] == "medium"


def test_parser_accepts_json_object_with_extra_model_text() -> None:
    """Claude occasionally wraps the object in a short sentence despite the
    prompt. The backend should recover the object instead of surfacing
    ``LLM returned invalid JSON`` to the facilitator."""
    payload = _parse_llm_json_payload(
        'Вот JSON:\n{"description": "Задача с URL {в тексте}", "methods": ["API"], '
        '"complexity": "Средняя", "sp_dev": 5, "sp_test": 3, "sp_final": 5, '
        '"scale_label": "5 SP", "confidence": "medium", "assumptions": []}\nГотово.'
    )
    assert payload["description"] == "Задача с URL {в тексте}"
    assert payload["sp_final"] == 5


def test_ensure_current_task_description_backfills_in_place() -> None:
    """``_ensure_current_task_description`` must mutate the *same* session
    instance the caller passed in (so post-mutate endpoints don't have
    to re-read the repo) and persist via ``save_session``. The helper
    now backfills both ``description`` (plain text) and
    ``description_adf`` (raw ADF) in a single pass."""
    import asyncio
    from types import SimpleNamespace

    from app.domain.session import Session
    from app.domain.task import Task
    from services.voting_service import _http_shared
    from services.voting_service._http_shared import JiraDescriptionFetch

    task = Task(jira_key="PROJ-7", summary="X")
    session = Session(chat_id=1, topic_id=None)
    session.tasks_queue = [task]
    session.current_task_index = 0

    saved: list[Session] = []

    class _Repo:
        async def save_session(self, s):
            saved.append(s)

    async def _fake_fetch(_http_session, _key):
        return JiraDescriptionFetch(
            text="Fetched body",
            adf={"type": "doc", "content": [{"type": "paragraph"}]},
            html="<p>Fetched body</p>",
        )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repository=_Repo(),
                http_session=object(),
            )
        )
    )

    saved_fetch = _http_shared._fetch_jira_description
    _http_shared._fetch_jira_description = _fake_fetch
    try:
        changed = asyncio.run(
            _http_shared._ensure_current_task_description(
                request, 1, None, session=session
            )
        )
    finally:
        _http_shared._fetch_jira_description = saved_fetch

    assert changed is True
    assert task.description == "Fetched body"
    assert task.description_adf == {"type": "doc", "content": [{"type": "paragraph"}]}
    assert task.description_html == "<p>Fetched body</p>"
    assert saved and saved[0] is session

    # Warm path: second call must be a no-op (all fields already set).
    changed_again = asyncio.run(
        _http_shared._ensure_current_task_description(
            request, 1, None, session=session
        )
    )
    assert changed_again is False
    assert len(saved) == 1  # no extra write


def test_ensure_current_task_description_replaces_confluence_link_stub() -> None:
    """Existing Jira descriptions that are just Confluence links should be
    upgraded once the resolved Jira+Confluence context is available."""
    import asyncio
    from types import SimpleNamespace

    from app.domain.session import Session
    from app.domain.task import Task
    from services.voting_service import _http_shared
    from services.voting_service._http_shared import JiraDescriptionFetch

    task = Task(
        jira_key="FLEX-2702",
        summary="X",
        description="Описание",
        description_adf={
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Описание"}]}],
        },
        description_html='<p><a href="https://media-life.atlassian.net/wiki/spaces/SAC/pages/1049231367">Описание</a></p>',
    )
    session = Session(chat_id=1, topic_id=None)
    session.tasks_queue = [task]
    session.current_task_index = 0
    saved: list[Session] = []

    class _Repo:
        async def save_session(self, s):
            saved.append(s)

    async def _fake_fetch(_http_session, _key):
        return JiraDescriptionFetch(
            text="Описание\n\nConfluence: Нотификации ОИ БР\nПолное описание из Confluence",
            adf=None,
            html="<p>Описание</p><hr /><h3>Confluence: Нотификации ОИ БР</h3><p>Полное описание из Confluence</p>",
        )

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repository=_Repo(), http_session=object())))
    saved_fetch = _http_shared._fetch_jira_description
    _http_shared._fetch_jira_description = _fake_fetch
    try:
        changed = asyncio.run(_http_shared._ensure_current_task_description(request, 1, None, session=session))
    finally:
        _http_shared._fetch_jira_description = saved_fetch

    assert changed is True
    assert "Полное описание из Confluence" in (task.description or "")
    assert "Confluence" in (task.description_html or "")
    assert saved and saved[0] is session


def test_task_description_round_trips_through_serialization() -> None:
    """Imported Jira description must survive ``to_dict``/``from_dict``
    so it persists across reloads of the session file. Both the plain
    text projection and the raw ADF payload must round-trip cleanly."""
    from app.domain.task import Task

    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}
        ],
    }
    task = Task(
        jira_key="PROJ-1",
        summary="Build",
        description="Spec body",
        description_adf=adf,
        description_html="<p>Spec body</p>",
    )
    loaded = Task.from_dict(task.to_dict())
    assert loaded.description == "Spec body"
    assert loaded.description_adf == adf
    assert loaded.description_html == "<p>Spec body</p>"

    # Legacy payloads (no description fields) must still load cleanly.
    legacy = Task.from_dict({"jira_key": "OLD-1", "summary": "Legacy"})
    assert legacy.description is None
    assert legacy.description_adf is None

    # Defensive: junk in ``description_adf`` (string, empty dict, wrong
    # type) must collapse to ``None`` so the renderer never has to
    # defend itself against malformed data.
    for bad in ["plain string", {}, {"foo": "bar"}, [], 42]:
        coerced = Task.from_dict(
            {"jira_key": "X-1", "summary": "x", "description_adf": bad}
        )
        assert coerced.description_adf is None, f"junk ADF {bad!r} must coerce to None"


def test_build_task_context_uses_stored_description_when_jira_context_missing() -> None:
    """Stored ``Task.description`` (from import) must reach the prompt
    when the live jira-service context fetch returned nothing — otherwise
    the AI loses the spec body and falls back to estimating from the
    title alone."""
    from app.domain.task import Task
    from services.voting_service.ai_summary_llm import _build_task_context

    task = Task(
        jira_key="PROJ-9",
        summary="Add bonus tab",
        description="Bonus tab spec captured from Jira at import time.",
    )
    context = _build_task_context(task, jira_context=None)
    assert "Bonus tab spec captured from Jira at import time." in context


def test_build_task_context_compacts_large_confluence_specs_preserving_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large linked Confluence pages should be compact but keep tail requirements."""
    from app.domain.task import Task
    from services.voting_service.ai_summary_llm import _build_task_context

    monkeypatch.delenv("ANTHROPIC_MAX_CONTEXT_CHARS", raising=False)
    long_description = "A" * 7000 + "\nTAIL_REQUIREMENT_FROM_CONFLUENCE"
    context = _build_task_context(
        Task(jira_key="PROJ-10", summary="Large Confluence spec", description=long_description),
        jira_context=None,
    )
    assert "TAIL_REQUIREMENT_FROM_CONFLUENCE" in context
    assert len(context) < 4000


def test_validator_corrects_sp_final_below_max() -> None:
    """The validator must enforce sp_final >= max(sp_dev, sp_test) regardless
    of what the model returns. Prompt rule + validator rule together — this
    is the belt-and-braces check."""
    payload = _validate_summary_payload(
        {
            "description": "Задача",
            "methods": ["API"],
            "complexity": "Средняя",
            "sp_dev": 8,
            "sp_test": 3,
            # Model returned a lower value than max — validator must lift it.
            "sp_final": 3,
            "confidence": "medium",
        }
    )
    assert payload["sp_final"] == 8
