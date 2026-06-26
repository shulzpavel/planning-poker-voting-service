"""Tests for precomputed product radar blocking feed."""

from __future__ import annotations

from app.domain.product_radar_blocking_feed import (
    build_snapshot_blocking_feed,
    ensure_snapshot_blocking_feed,
)


def test_build_snapshot_blocking_feed_from_signals_and_team_blocking() -> None:
    feed = build_snapshot_blocking_feed(
        [
            {
                "kind": "cross_team_block",
                "severity": "high",
                "issue_key": "BT-1",
                "blocker_key": "BT-9",
                "blocked_team": "RIP",
                "blocking_team": "Android",
                "title": "Блокировка",
                "detail": "detail",
            }
        ],
        {
            "teams": [
                {
                    "key": "Android",
                    "label": "Android",
                    "items": [{"issue_key": "BT-2", "blocker_key": "BT-9", "blocked_team": "RIP"}],
                }
            ],
            "total_blocks": 2,
        },
    )

    assert feed["total"] == 2
    assert len(feed["blockings"]) == 2
    assert feed["blockings"][0]["category"] == "blocking"


def test_ensure_snapshot_blocking_feed_backfills_legacy_snapshot() -> None:
    snapshot = {
        "signals": [
            {
                "kind": "cross_team_block",
                "severity": "high",
                "issue_key": "BT-1",
                "blocker_key": "BT-9",
                "blocked_team": "RIP",
                "blocking_team": "Android",
                "title": "Блокировка",
                "detail": "detail",
            }
        ],
        "analytics": {"periods": {"all": {"team_blocking": {"teams": [], "total_blocks": 0}}}},
    }

    enriched = ensure_snapshot_blocking_feed(snapshot)

    assert enriched["blocking_feed"]["total"] == 1
    assert enriched["blocking_feed"]["blockings"][0]["blockedKey"] == "BT-1"
