"""Tests for scope board Jira fetch helpers in cms_api."""

from services.voting_service.cms_api import (
    SCOPE_JQL_MAX_RESULTS,
    _ScopeJqlFetchResult,
    _count_snapshot_issues,
    _scope_fetch_warnings,
    _scope_snapshot_with_todo_done,
    _scope_snapshot_with_todo_item,
    _scope_snapshot_without_todo_item,
)


def test_count_snapshot_issues_sums_sections_and_queues():
    snapshot = {
        "sections": [{"issues": [{"key": "A-1"}, {"key": "A-2"}]}],
        "plan_issues": [{"key": "P-1"}],
        "priority_queues": {
            "todo": {"issues": [{"key": "T-1"}]},
            "test": {"issues": []},
        },
    }
    assert _count_snapshot_issues(snapshot) == 4


def test_scope_fetch_warnings_only_for_truncated_jql():
    warnings = _scope_fetch_warnings(
        [
            _ScopeJqlFetchResult(jql="project = A", issues=[{}] * 10, truncated=False),
            _ScopeJqlFetchResult(jql="project = B", issues=[{}] * SCOPE_JQL_MAX_RESULTS, truncated=True),
        ]
    )
    assert warnings == [{"jql": "project = B", "truncated": True, "count": SCOPE_JQL_MAX_RESULTS}]


def test_scope_todo_items_are_added_toggled_and_removed():
    snapshot = {"metrics": {}, "todo_items": []}
    with_item = _scope_snapshot_with_todo_item(
        snapshot,
        text="Проверить отчёт",
        actor_name="Paul",
        created_at="2026-06-14T12:00:00+00:00",
    )
    item = with_item["todo_items"][0]
    assert item["text"] == "Проверить отчёт"
    assert item["done"] is False
    assert item["created_by"] == "Paul"

    done_snapshot = _scope_snapshot_with_todo_done(
        with_item,
        item_id=item["id"],
        done=True,
        actor_name="Paul",
        changed_at="2026-06-14T12:05:00+00:00",
    )
    assert done_snapshot["todo_items"][0]["done"] is True
    assert done_snapshot["todo_items"][0]["done_by"] == "Paul"

    removed_snapshot = _scope_snapshot_without_todo_item(done_snapshot, item_id=item["id"])
    assert removed_snapshot["todo_items"] == []
