"""Tests for product radar snapshot computation."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.product_radar import compute_product_radar_snapshot


def _issue(
    key: str,
    *,
    status: str = "В работе",
    category: str = "indeterminate",
    assignee: str = "",
    story_points: float | None = 3,
    created: str = "2026-01-01T00:00:00+00:00",
    updated: str = "2026-06-01T00:00:00+00:00",
) -> dict:
    return {
        "key": key,
        "summary": f"Task {key}",
        "url": f"https://jira.example/browse/{key}",
        "status": status,
        "status_category": category,
        "assignee": assignee,
        "story_points": story_points,
        "created": created,
        "updated": updated,
        "status_changed_at": updated,
        "status_entered_at": updated,
    }


def test_product_radar_classifies_loaded_and_idle_people():
    issues = [
        _issue("A-1", assignee="Alice"),
        _issue("A-2", assignee="Alice"),
        _issue("A-3", assignee="Alice"),
        _issue("B-1", assignee="Bob", status="К выполнению", category="new"),
        _issue("B-2", assignee="Bob", status="К выполнению", category="new"),
        _issue("C-1", assignee="Carol"),
    ]
    snapshot = compute_product_radar_snapshot(issues, now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    people = {row["name"]: row for row in snapshot["people"]}
    assert people["Alice"]["load_band"] == "loaded"
    assert people["Bob"]["load_band"] == "idle"
    assert people["Carol"]["load_band"] == "normal"
    assert snapshot["summary"]["loaded_people"] == 1
    assert snapshot["summary"]["idle_people"] == 1


def test_product_radar_snapshot_has_analytics_and_signals():
    issues = [
        _issue("T-1", status="Тестирование", assignee="QA"),
        _issue("T-2", status="Пауза", assignee="Dev"),
        _issue("T-3", status="К выполнению", category="new", assignee="Dev"),
    ]
    snapshot = compute_product_radar_snapshot(issues, now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert "charts" not in snapshot
    assert snapshot["analytics"]
    assert snapshot["signals"] is not None


def test_product_radar_triggers_group_signals():
    old = "2026-05-01T00:00:00+00:00"
    issues = [
        {
            **_issue("S-1", status="Тестирование", assignee=""),
            "status_changed_at": old,
            "status_entered_at": old,
            "updated": old,
        }
    ]
    snapshot = compute_product_radar_snapshot(issues, now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert snapshot["signals"]
    assert snapshot["triggers"]
    assert any(trigger["id"] == "unassigned" for trigger in snapshot["triggers"])
