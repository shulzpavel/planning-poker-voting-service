"""Regression tests for scope board AI prompt + validator."""

import pytest

from services.voting_service.scope_ai_llm import (
    LlmScopeError,
    _system_prompt,
    _validate_scope_payload,
    build_scope_analysis_context,
)


def test_system_prompt_pins_schema_keys():
    prompt = _system_prompt("sp")
    for fragment in (
        '"health"',
        '"whats_good"',
        '"whats_bad"',
        '"whats_critical"',
        '"report_assessment"',
        '"open_questions_assessment"',
        '"role_workload_assessment"',
        '"role_risks"',
        '"role_focus"',
        '"capacity_assessment"',
        '"buffer_status"',
        '"queue_insights"',
        '"focus_now"',
        "бизнес",
        "JSON",
        "Открытые вопросы",
        "нагрузку по ролям",
    ):
        assert fragment in prompt


def test_system_prompt_split_mode_mentions_dev_test_tracks():
    prompt = _system_prompt("sp_dev_test")
    assert "SP Dev" in prompt
    assert "SP Test" in prompt


def test_build_context_includes_metrics_queues_and_questions():
    context = build_scope_analysis_context({
        "name": "Июнь FLEX",
        "month": "2026-06",
        "snapshot": {
            "refreshed_at": "2026-06-13T12:00:00+00:00",
            "metrics": {
                "capacity_sp": 80,
                "plan_sp": 50,
                "unplan_sp": 10,
                "buffer_sp": 20,
                "overfill_sp": 0,
                "intake_status": "ok",
                "plan_count": 5,
                "unplan_count": 2,
                "unestimated_count": 1,
                "scope_creep_count": 1,
                "sections": [
                    {"name": "Plan", "kind": "planned", "count": 5, "story_points": 50, "by_status": {"Done": 2}},
                ],
                "unestimated_tasks": [{"key": "FLEX-1", "status": "In Progress", "summary": "No SP"}],
                "plan_by_role": {
                    "front": [{"developer": "Front Dev", "story_points": 10, "count": 2, "issues": []}],
                    "back": [],
                    "qa": [],
                },
                "plan_role_coverage": {
                    "front": {"attributed": 2, "total": 2, "confirmed": 2, "unattributed": 0},
                    "back": {"attributed": 0, "total": 0},
                    "qa": {"attributed": 1, "total": 2, "unattributed": 1},
                },
                "unplan_role_coverage": {
                    "front": {"attributed": 0, "total": 0},
                    "back": {"attributed": 1, "total": 2, "unattributed": 1},
                    "qa": {"attributed": 0, "total": 0},
                },
            },
            "sections": [
                {
                    "id": "plan",
                    "name": "Plan",
                    "kind": "planned",
                    "issues": [
                        {"key": "FLEX-10", "summary": "Feature", "story_points": 5, "status": "In Progress", "priority": "High"},
                    ],
                }
            ],
            "priority_queues": {
                "todo": {
                    "issues": [
                        {"key": "FLEX-20", "summary": "Next up", "story_points": 3, "status": "К выполнению", "grooming_comment": "Берём первой"},
                    ]
                },
                "test": {"issues": []},
            },
            "manual_questions": [{"id": "manual-abc123", "summary": "Нужен ли rollback?", "created_by": "PO"}],
            "report": {"open_questions": [{"key": "FLEX-99", "summary": "Blocked on API", "status": "Пауза"}]},
            "events": [{"type": "added", "message": "Добавлена задача", "key": "FLEX-10"}],
        },
    })
    assert "Июнь FLEX" in context
    assert "лимит месяца: 80 SP" in context
    assert "запас на новые задачи: 20 SP" in context
    assert "FLEX-20" in context
    assert "Берём первой" in context
    assert "FLEX-99" in context
    assert "Нужен ли rollback" in context
    assert "open_questions_manual: 1" in context
    assert "kind=manual" in context
    assert "Ручные вопросы PO" in context
    assert "Отчёт — задачи по колонкам" in context
    assert "Нагрузка по ролям" in context
    assert "Plan Front" in context
    assert "Front Dev" in context
    assert "Открытые вопросы" in context


def test_build_context_includes_dev_test_capacity_tracks():
    context = build_scope_analysis_context({
        "name": "Split board",
        "month": "2026-06",
        "workload_mode": "sp_dev_test",
        "snapshot": {
            "metrics": {
                "workload_mode": "sp_dev_test",
                "capacity_sp": 80,
                "capacity_sp_dev": 60,
                "capacity_sp_test": 30,
                "plan_dev_sp": 20,
                "unplan_dev_sp": 5,
                "buffer_dev_sp": 35,
                "overfill_dev_sp": 0,
                "plan_test_sp": 8,
                "unplan_test_sp": 2,
                "buffer_test_sp": 20,
                "overfill_test_sp": 0,
                "plan_sp": 28,
                "unplan_sp": 7,
                "buffer_sp": 20,
                "intake_status": "ok",
            },
        },
    })
    assert "workload_mode: sp_dev_test" in context
    assert "Разработка (SP Dev)" in context
    assert "Тестирование (SP Test)" in context
    assert "запас=35 SP" in context
    assert "запас=20 SP" in context


def test_build_context_includes_role_workload_coverage():
    context = build_scope_analysis_context({
        "name": "Test",
        "month": "2026-06",
        "snapshot": {
            "metrics": {
                "plan_role_coverage": {"back": {"attributed": 1, "total": 2, "unattributed": 1, "confirmed_jira": 1}},
                "plan_by_role": {"back": [{"developer": "Не атрибутировано", "story_points": 5, "count": 1, "issues": [{"key": "FLEX-99"}]}]},
            },
        },
    })
    assert "1/2" in context
    assert "FLEX-99" in context


def test_build_context_uses_jira_role_rules_for_ai():
    context = build_scope_analysis_context({
        "name": "Test",
        "month": "2026-06",
        "snapshot": {
            "metrics": {
                "plan_role_coverage": {
                    "front": {
                        "attributed": 0,
                        "total": 1,
                        "unattributed": 1,
                    },
                    "back": {"attributed": 1, "total": 1, "confirmed_jira": 1},
                },
                "plan_by_role": {
                    "back": [{"developer": "Back Dev", "story_points": 2, "count": 1, "issues": [{"key": "FLEX-1965"}]}],
                },
            },
            "sections": [
                {
                    "id": "plan",
                    "name": "Plan",
                    "kind": "planned",
                    "issues": [
                        {
                            "key": "FLEX-1965",
                            "summary": "Ретраи при отправке сообщений в AlanBase",
                            "story_points": 2,
                            "status": "В работе",
                            "jira_role_assignees": {"front": "", "back": "Back Dev", "qa": ""},
                        }
                    ],
                }
            ],
        },
    })

    assert "полям Jira" in context
    assert "Plan Front: 0/1" in context
    assert "Plan Back: 1/1" in context


def test_collect_open_questions_includes_manual_and_excludes_resolved():
    from services.voting_service.scope_ai_llm import _collect_open_questions

    items = _collect_open_questions({
        "manual_questions": [
            {"id": "manual-1", "summary": "Rollback?"},
            {"id": "manual-2", "summary": "Closed"},
        ],
        "resolved_questions": [{"id": "manual-2", "summary": "Closed", "comment": "ok"}],
        "report": {"open_questions": []},
        "sections": [],
    })
    assert len(items) == 1
    assert items[0]["kind"] == "manual"
    assert items[0]["summary"] == "Rollback?"


def test_check_open_questions_assessment_rejects_false_empty():
    from services.voting_service.scope_ai_llm import _check_open_questions_assessment, LlmScopeError

    with pytest.raises(LlmScopeError):
        _check_open_questions_assessment(
            "Открытых вопросов нет, блокеров тоже.",
            [{"kind": "manual", "id": "manual-1", "summary": "Rollback?"}],
        )


def test_check_open_questions_assessment_allows_valid():
    from services.voting_service.scope_ai_llm import _check_open_questions_assessment

    _check_open_questions_assessment(
        "1 ручной вопрос: нужен ли rollback?",
        [{"kind": "manual", "id": "manual-1", "summary": "Rollback?"}],
    )


def test_validator_accepts_minimal_valid_payload():
    out = _validate_scope_payload({
        "health": "yellow",
        "summary": "Буфер сжимается, intake под контролем.",
        "whats_good": ["Буфер 20 SP"],
        "whats_bad": ["1 задача без SP"],
        "whats_critical": ["FLEX-99 в паузе"],
        "report_assessment": "Plan: 3 in work, 1 in test, 2 done.",
        "open_questions_assessment": "1 пауза FLEX-99 блокирует API.",
        "role_workload_assessment": "Back Plan 1/2, одна задача без GitLab-атрибуции.",
        "role_risks": ["Перегруз Back на внеплане"],
        "role_focus": ["Сверить unattributed Back задачи"],
        "capacity_assessment": "20 SP запас при capacity 80.",
        "buffer_status": "tight",
        "delivery_snapshot": "5 в работе, 2 в тесте.",
        "blockers": [{"title": "Пауза FLEX-99", "severity": "high", "detail": "API", "issue_keys": ["FLEX-99"]}],
        "scope_risks": ["Scope creep"],
        "queue_insights": {"todo": "Очередь короткая", "test": "Пусто"},
        "recommendations": [{"text": "Закрыть FLEX-99", "impact": "high"}],
        "focus_now": ["Груминг todo"],
        "watch_list": ["buffer_sp"],
    })
    assert out["health"] == "yellow"
    assert out["whats_critical"] == ["FLEX-99 в паузе"]
    assert out["report_assessment"].startswith("Plan:")
    assert out["open_questions_assessment"].startswith("1 пауза")
    assert out["role_workload_assessment"].startswith("Back Plan")
    assert out["role_risks"][0].startswith("Перегруз")
    assert out["blockers"][0]["issue_keys"] == ["FLEX-99"]
    assert out["source"] == "anthropic"


def test_parse_scope_llm_json_prefill_and_fences():
    from services.voting_service.scope_ai_llm import _parse_scope_llm_json

    prefill_body = '"health": "green", "summary": "OK"}'
    parsed = _parse_scope_llm_json(prefill_body)
    assert parsed["health"] == "green"
    assert parsed["summary"] == "OK"

    fenced = '```json\n{"health": "yellow", "summary": "Fence"}\n```'
    assert _parse_scope_llm_json(fenced)["summary"] == "Fence"


def test_validator_requires_summary():
    with pytest.raises(LlmScopeError):
        _validate_scope_payload({"health": "green"})
