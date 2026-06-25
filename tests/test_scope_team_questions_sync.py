"""Regression tests for team-level release open questions sync."""

from __future__ import annotations

from typing import Any

import pytest

from planning_poker_common.scope.team_questions import (
    manual_question_with_release_meta,
    normalize_team_scope_questions,
)
from services.voting_service.cms.scope import (
    _apply_release_team_questions,
    _sync_release_team_questions,
)


class _TeamQuestionStore:
    def __init__(self, team_questions: dict[str, Any] | None = None) -> None:
        self.team_questions = normalize_team_scope_questions(team_questions)
        self.saved: list[dict[str, Any]] = []

    async def get_team_scope_questions(self, team_id: int) -> dict[str, Any]:
        assert team_id == 42
        return self.team_questions

    async def save_team_scope_questions(self, team_id: int, questions: dict[str, Any]) -> None:
        assert team_id == 42
        self.team_questions = normalize_team_scope_questions(questions)
        self.saved.append(self.team_questions)

    async def backfill_team_scope_questions_from_boards(self, team_id: int) -> dict[str, Any]:
        return normalize_team_scope_questions({})


def _release_board(*, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": 7,
        "team_id": 42,
        "team": {"slug": "igaming-ios", "name": "iGaming iOS"},
        "snapshot": snapshot,
    }


@pytest.mark.asyncio
async def test_apply_release_team_questions_merges_team_registry_into_stale_snapshot():
    store = _TeamQuestionStore(
        {
            "manual_questions": [
                manual_question_with_release_meta(
                    text="Question A",
                    actor_name="PO",
                    question_id="manual-a",
                    release_name="iOS 2.5",
                ),
                manual_question_with_release_meta(
                    text="Question B",
                    actor_name="PO",
                    question_id="manual-b",
                    release_name="iOS 2.5",
                ),
            ],
        }
    )
    board = _release_board(
        snapshot={
            "plan_issues": [],
            "unplan_issues": [],
            "metrics": {},
            "manual_questions": [
                manual_question_with_release_meta(
                    text="Question A",
                    actor_name="PO",
                    question_id="manual-a",
                    release_name="iOS 2.5",
                )
            ],
        }
    )

    merged = await _apply_release_team_questions(store, board)
    manual_ids = {item["id"] for item in merged["snapshot"]["manual_questions"]}
    assert manual_ids == {"manual-a", "manual-b"}


@pytest.mark.asyncio
async def test_sync_release_team_questions_unions_with_existing_team_registry():
    store = _TeamQuestionStore(
        {
            "manual_questions": [
                manual_question_with_release_meta(
                    text="Question A",
                    actor_name="PO",
                    question_id="manual-a",
                    release_name="iOS 2.5",
                ),
                manual_question_with_release_meta(
                    text="Question B",
                    actor_name="PO",
                    question_id="manual-b",
                    release_name="iOS 2.5",
                ),
            ],
        }
    )
    board = _release_board(snapshot={})
    stale_snapshot = {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "manual_questions": [
            manual_question_with_release_meta(
                text="Question A",
                actor_name="PO",
                question_id="manual-a",
                release_name="iOS 2.5",
            ),
            manual_question_with_release_meta(
                text="Question C",
                actor_name="PO",
                question_id="manual-c",
                release_name="iOS 2.6",
            ),
        ],
    }

    await _sync_release_team_questions(store, board, stale_snapshot)

    manual_ids = {item["id"] for item in store.team_questions["manual_questions"]}
    assert manual_ids == {"manual-a", "manual-b", "manual-c"}
