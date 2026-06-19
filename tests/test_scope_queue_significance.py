from app.domain.scope_board import apply_priority_queue_reorder, queue_significance_positions


def _issue(key: str, sp: int):
    return {"key": key, "summary": key, "story_points": sp}


def test_queue_significance_positions():
    assert queue_significance_positions(["A-1", "B-2"]) == {"A-1": 1, "B-2": 2}


def test_apply_priority_queue_reorder_sets_significance_for_all_issues():
    queue = {
        "order": ["P-1", "P-2"],
        "issues": [_issue("P-1", 1), _issue("P-2", 2)],
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
    assert [issue["significance"] for issue in updated["issues"]] == [1, 2]
