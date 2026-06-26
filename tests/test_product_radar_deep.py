"""Deep signals and drill-down for product radar."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.product_radar import (
    collect_deep_radar_signals,
    compute_product_radar_snapshot,
    normalize_radar_issue,
)


def _base_issue(key: str, **extra) -> dict:
    payload = {
        "key": key,
        "summary": f"Task {key}",
        "url": f"https://jira.example/browse/{key}",
        "status": "В работе",
        "status_category": "indeterminate",
        "team": "Alpha",
        "assignee": "Dev",
        "story_points": 3,
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-06-01T00:00:00+00:00",
        "status_changed_at": "2026-06-01T00:00:00+00:00",
        "status_entered_at": "2026-06-01T00:00:00+00:00",
        "status_segments": [
            {
                "status": "В работе",
                "assignee": "Dev",
                "bucket": "in_work",
                "days": 4.0,
                "started_at": "2026-05-28T00:00:00+00:00",
            }
        ],
    }
    payload.update(extra)
    return normalize_radar_issue(payload)


def test_normalize_radar_issue_builds_timeline_and_drilldown():
    issue = _base_issue("BT-1", subtasks=[{"key": "BT-2", "status": "В работе", "status_category": "indeterminate"}])
    assert issue["timeline"]
    assert issue["timeline"][0]["status"] == "В работе"
    assert issue["drilldown"]["subtasks"][0]["key"] == "BT-2"


def test_cross_team_block_signal():
    blocked = _base_issue("BT-10", team="Frontend")
    blocker = _base_issue(
        "BT-99",
        team="Backend",
        status="В работе",
        issue_links=[],
    )
    blocked["issue_links"] = [
        {
            "relation": "blocked_by",
            "key": "BT-99",
            "status": "В работе",
            "status_category": "indeterminate",
            "team": "Backend",
        }
    ]
    signals = collect_deep_radar_signals([blocked, blocker], issues_by_key={"BT-10": blocked, "BT-99": blocker})
    kinds = {signal["kind"] for signal in signals}
    assert "cross_team_block" in kinds
    cross = next(signal for signal in signals if signal["kind"] == "cross_team_block")
    assert cross["blocking_team"] == "Backend"
    assert cross["blocked_team"] == "Frontend"


def test_subtask_gap_signal_when_half_idle():
    issue = _base_issue(
        "BT-20",
        subtasks=[
            {"key": "BT-21", "status": "К выполнению", "status_category": "new"},
            {"key": "BT-22", "status": "К выполнению", "status_category": "new"},
        ],
        role_workload_items=[{"subtask_key": "BT-21"}],
    )
    signals = collect_deep_radar_signals([issue])
    assert any(signal["kind"] == "subtask_gap" for signal in signals)


def test_team_blocking_available_in_analytics():
    blocked = _base_issue("BT-30", team="A")
    blocker = _base_issue("BT-31", team="B")
    blocked["issue_links"] = [
        {
            "relation": "blocked_by",
            "key": "BT-31",
            "status": "В работе",
            "status_category": "indeterminate",
            "team": "B",
        }
    ]
    snapshot = compute_product_radar_snapshot([blocked, blocker], now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert "charts" not in snapshot
    team_blocking = (snapshot.get("analytics") or {}).get("team_blocking")
    assert team_blocking is not None or any(signal.get("kind") == "cross_team_block" for signal in snapshot["signals"])
