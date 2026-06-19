from app.domain.scope_board import (
    apply_priority_queue_ranked_update,
    apply_priority_queue_reorder,
    clear_priority_queue_ranked,
    merge_priority_queue,
    queue_significance_positions,
)


def _issue(key: str, sp: int):
    return {"key": key, "summary": key, "story_points": sp}


def test_queue_significance_positions():
    assert queue_significance_positions(["A-1", "B-2"]) == {"A-1": 1, "B-2": 2}


def test_apply_priority_queue_reorder_sets_significance_for_ranked_issues():
    queue = {
        "ranked_order": ["P-1", "P-2"],
        "order": ["P-1", "P-2"],
        "issues": [_issue("P-1", 1), _issue("P-2", 2), _issue("P-3", 3)],
        "history": [],
    }
    updated = apply_priority_queue_reorder(
        queue,
        order=["P-2", "P-1"],
        comment="",
        actor_name="PO",
        changed_at="2026-06-12T11:00:00+00:00",
        queue_label="Задачи к тестированию",
        moved_key="P-2",
    )
    by_key = {issue["key"]: issue for issue in updated["issues"]}
    assert by_key["P-2"]["significance"] == 1
    assert by_key["P-1"]["significance"] == 2
    assert "significance" not in by_key["P-3"]


def test_apply_priority_queue_ranked_update_adds_issue_from_warehouse():
    queue = {
        "ranked_order": ["P-1"],
        "order": ["P-1"],
        "issues": [_issue("P-1", 1), _issue("P-2", 2)],
        "history": [],
    }
    updated = apply_priority_queue_ranked_update(
        queue,
        ranked_order=["P-2", "P-1"],
        comment="",
        actor_name="PO",
        changed_at="2026-06-12T11:00:00+00:00",
        queue_label="Задачи к выполнению",
        moved_key="P-2",
    )
    assert updated["ranked_order"] == ["P-2", "P-1"]
    assert updated["removed_from_ranked"] == []


def test_apply_priority_queue_ranked_update_removes_issue_to_warehouse():
    queue = {
        "ranked_order": ["P-1", "P-2"],
        "order": ["P-1", "P-2"],
        "issues": [_issue("P-1", 1), _issue("P-2", 2)],
        "history": [],
    }
    updated = apply_priority_queue_ranked_update(
        queue,
        ranked_order=["P-2"],
        comment="",
        actor_name="PO",
        changed_at="2026-06-12T11:00:00+00:00",
        queue_label="Задачи к выполнению",
        moved_key="P-1",
    )
    assert updated["ranked_order"] == ["P-2"]
    assert updated["removed_from_ranked"] == ["P-1"]
    by_key = {issue["key"]: issue for issue in updated["issues"]}
    assert "significance" not in by_key["P-1"]
    assert by_key["P-2"]["significance"] == 1


def test_merge_priority_queue_ignores_legacy_order_for_ranked():
    previous = {
        "order": ["P-1", "P-2", "P-3"],
        "issues": [_issue("P-1", 1), _issue("P-2", 2), _issue("P-3", 3)],
        "history": [],
    }
    merged = merge_priority_queue(
        [_issue("P-1", 1), _issue("P-2", 2), _issue("P-3", 3)],
        previous,
        queue_label="Задачи к выполнению",
        refreshed_at="2026-06-12T11:00:00+00:00",
    )
    assert merged["ranked_order"] == []


def test_clear_priority_queue_ranked_clears_local_and_collects_jira_keys():
    queue = {
        "ranked_order": ["P-1", "P-2"],
        "order": ["P-1", "P-2", "P-3"],
        "issues": [
            {**_issue("P-1", 1), "significance": 1},
            {**_issue("P-2", 2), "significance": 2},
            _issue("P-3", 3),
        ],
        "history": [],
    }
    cleared, keys = clear_priority_queue_ranked(queue)
    assert cleared["ranked_order"] == []
    assert cleared["order"] == []
    assert set(keys) == {"P-1", "P-2", "P-3"}
    by_key = {issue["key"]: issue for issue in cleared["issues"]}
    assert "significance" not in by_key["P-1"]
    assert "significance" not in by_key["P-2"]
