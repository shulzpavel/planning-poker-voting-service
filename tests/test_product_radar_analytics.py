"""Tests for product radar analytics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.product_radar_analytics import (
    build_closure_trend,
    build_epic_interior_stats,
    build_phase_totals,
    build_release_contour,
    build_status_age_histogram,
    collect_product_insights,
    compute_period_analytics,
    compute_product_analytics,
    filter_issues_for_period,
)


def _issue(key: str, **extra) -> dict:
    payload = {
        "key": key,
        "summary": f"Task {key}",
        "status": "В работе",
        "status_category": "indeterminate",
        "team": "Alpha",
        "story_points": 5,
        "current_status_days": 10,
        "status_bucket_durations": {"dev": 8, "test": 2},
        "updated": "2026-06-01T00:00:00+00:00",
        "created": "2026-01-01T00:00:00+00:00",
    }
    payload.update(extra)
    return payload


def test_subtask_bottleneck_insight():
    issue = _issue(
        "BT-1",
        subtasks=[
            {"key": "BT-1-1", "status": "Готово", "status_category": "done", "assignee": "Dev1"},
            {"key": "BT-1-2", "status": "Готово", "status_category": "done", "assignee": "Dev2"},
            {"key": "BT-1-3", "status": "В работе", "status_category": "indeterminate", "assignee": "QA"},
        ],
    )
    insights = collect_product_insights([issue], [])
    assert any(item["kind"] == "subtask_bottleneck" for item in insights)
    bottleneck = next(item for item in insights if item["kind"] == "subtask_bottleneck")
    assert bottleneck["blocker_key"] == "BT-1-3"
    assert bottleneck["severity"] == "high"


def test_release_contour_groups_stages():
    issues = [
        _issue("A", status="К выполнению", status_category="new"),
        _issue("B", status="В работе"),
        _issue("C", status="Тестирование"),
        _issue("D", status="К релизу"),
    ]
    contour = build_release_contour(issues)
    by_key = {stage["key"]: stage["count"] for stage in contour["stages"]}
    assert by_key["backlog"] == 1
    assert by_key["dev"] == 1
    assert by_key["test"] == 1
    assert by_key["release"] == 1


def test_compute_product_analytics_has_periods():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    analytics = compute_product_analytics([_issue("X")], [], now=now)
    assert "periods" in analytics
    assert set(analytics["periods"]) == {"month", "quarter", "all"}
    assert analytics["default_period"] == "month"
    assert analytics["periods"]["month"]["period"] == "month"


def test_filter_issues_for_period_excludes_stale():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    fresh = _issue("NEW", updated=now.isoformat())
    stale = _issue("OLD", updated=(now - timedelta(days=120)).isoformat())
    month_issues = filter_issues_for_period([fresh, stale], "month", now=now)
    assert [issue["key"] for issue in month_issues] == ["NEW"]


def test_build_phase_totals_uses_bucket_durations():
    issues = [
        _issue("A", status_bucket_durations={"dev": 10, "test": 3}),
        _issue("B", status_bucket_durations={"pause": 5, "todo": 2}),
    ]
    totals = build_phase_totals(issues, period="month")
    assert totals["dev"] == 10
    assert totals["test"] == 3
    assert totals["pause"] == 5


def test_build_closure_trend_counts_done_issues():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    issues = [
        _issue(
            "DONE-1",
            status="Готово",
            status_category="done",
            resolution_date=(now - timedelta(days=2)).isoformat(),
        ),
        _issue("OPEN-1"),
    ]
    trend = build_closure_trend(issues, now=now, period="month")
    assert sum(item["value"] for item in trend) == 1


def test_build_closure_trend_daily_is_not_cumulative():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    issues = [
        _issue(
            "D1",
            status="Готово",
            status_category="done",
            resolution_date=datetime(2026, 6, 8, 10, tzinfo=timezone.utc).isoformat(),
        ),
        _issue(
            "D2",
            status="Готово",
            status_category="done",
            resolution_date=datetime(2026, 6, 8, 18, tzinfo=timezone.utc).isoformat(),
        ),
        _issue(
            "D3",
            status="Готово",
            status_category="done",
            resolution_date=datetime(2026, 6, 5, tzinfo=timezone.utc).isoformat(),
        ),
    ]
    trend = build_closure_trend(issues, now=now, period="month")
    by_key = {item["key"]: item for item in trend}
    assert by_key["2026-06-08"]["value"] == 2
    assert set(by_key["2026-06-08"]["issue_keys"]) == {"D1", "D2"}
    assert by_key["2026-06-05"]["value"] == 1
    assert sum(item["value"] for item in trend) == 3


def test_build_closure_trend_monthly_for_all_period():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    issues = [
        _issue(
            "M1",
            status="Готово",
            status_category="done",
            resolution_date=datetime(2026, 3, 15, tzinfo=timezone.utc).isoformat(),
        ),
        _issue(
            "M2",
            status="Готово",
            status_category="done",
            resolution_date=datetime(2026, 3, 20, tzinfo=timezone.utc).isoformat(),
        ),
    ]
    trend = build_closure_trend(issues, now=now, period="all")
    assert trend[0]["key"] == "2026-03"
    assert trend[-1]["key"] == "2026-06"
    assert len(trend) == 4
    march = next(item for item in trend if item["key"] == "2026-03")
    assert march["value"] == 2
    assert march["label"].startswith("мар")


def test_build_closure_trend_quarter_daily_three_months():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    trend = build_closure_trend([], now=now, period="quarter")
    assert trend[0]["key"] == "2026-04-01"
    assert trend[-1]["key"] == "2026-06-30"
    assert len(trend) == 91
    assert trend[0]["label"] == "1"
    assert trend[0]["month_key"] == "2026-04"


def test_build_closure_trend_month_covers_full_calendar_month():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    trend = build_closure_trend([], now=now, period="month")
    assert len(trend) == 30
    assert trend[0]["key"] == "2026-06-01"
    assert trend[-1]["key"] == "2026-06-30"
    assert trend[0]["label"] == "1"
    assert trend[9]["label"] == "10"


def test_period_analytics_sorts_load_by_team_desc():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    issues = [
        _issue("A", team="Alpha", story_points=3),
        _issue("B", team="Beta", story_points=13),
    ]
    bundle = compute_period_analytics(issues, [], "all", now=now)
    load = bundle["load_by_team"]
    assert load[0]["label"] == "Beta"
    assert load[0]["value"] >= load[1]["value"]
    assert set(load[0]["issue_keys"]) == {"B"}


def test_status_age_histogram_uses_dynamic_buckets_for_old_tasks():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    issues = [
        _issue("A", current_status_days=45),
        _issue("B", current_status_days=72),
        _issue("C", current_status_days=120),
        _issue("D", current_status_days=150),
    ]
    histogram = build_status_age_histogram(issues, now=now)
    labels = [item["label"] for item in histogram]
    assert len(histogram) >= 2
    assert "30+ дн." not in labels
    assert sum(item["value"] for item in histogram) == 4


def test_status_age_histogram_keeps_short_buckets_and_expands_long_tail():
    issues = [
        _issue("A", current_status_days=5),
        _issue("B", current_status_days=22),
        _issue("C", current_status_days=48),
        _issue("D", current_status_days=110),
    ]
    histogram = build_status_age_histogram(issues)
    labels = [item["label"] for item in histogram]
    assert "4–7 дн." in labels
    assert "15–30 дн." in labels
    assert "30+ дн." not in labels
    assert any("+" in label and not label.startswith("30") for label in labels)
    assert sum(item["value"] for item in histogram) == 4


def test_status_age_histogram_excludes_done_issues():
    issues = [
        _issue("OPEN", current_status_days=20),
        _issue("DONE", status="Готово", status_category="done", current_status_days=90),
        _issue("RESOLVED", status="Closed", resolution="Fixed", current_status_days=40),
    ]
    histogram = build_status_age_histogram(issues)
    assert sum(item["value"] for item in histogram) == 1
    labels = " ".join(item["label"] for item in histogram)
    assert "15–30" in labels or "15-30" in labels.replace("–", "-")


def test_build_epic_interior_stats_groups_by_epic_and_parent_subtasks():
    issues = [
        _issue("EPIC-1", issue_type="Epic", summary="Payments epic"),
        _issue("BT-1", linked_epic_key="EPIC-1", status="В работе"),
        _issue(
            "BT-2",
            linked_epic_key="EPIC-1",
            status="Готово",
            status_category="done",
            resolution_date="2026-06-01T00:00:00+00:00",
        ),
        _issue(
            "STORY-1",
            subtasks=[
                {"key": "STORY-1-1", "status": "Готово", "status_category": "done"},
                {"key": "STORY-1-2", "status": "В работе", "status_category": "indeterminate"},
            ],
        ),
    ]
    stats = build_epic_interior_stats(issues)
    assert stats["totals"]["epics"] == 1
    assert stats["totals"]["tasks"] == 2
    assert stats["totals"]["parents"] == 1
    assert stats["epics"][0]["key"] == "EPIC-1"
    assert stats["epics"][0]["done_subtasks"] == 1
    assert stats["parents"][0]["key"] == "STORY-1"
    assert stats["parents"][0]["open_subtasks"] == 1


def test_build_epic_interior_stats_uses_issue_links():
    issues = [
        _issue("EPIC-9", issue_type="Epic", summary="Big epic"),
        _issue(
            "STORY-9",
            summary="Story with links",
            issue_links=[
                {
                    "key": "REL-1",
                    "summary": "Related one",
                    "relation": "relates",
                    "relation_label": "связана с",
                    "status": "В работе",
                    "status_category": "indeterminate",
                },
                {
                    "key": "REL-2",
                    "summary": "Related two",
                    "relation": "relates",
                    "relation_label": "связана с",
                    "status": "Готово",
                    "status_category": "done",
                },
            ],
        ),
        _issue("REL-1", status="В работе"),
        _issue("REL-2", status="Готово", status_category="done", resolution_date="2026-06-01T00:00:00+00:00"),
    ]
    stats = build_epic_interior_stats(issues)
    story = next(row for row in stats["parents"] if row["key"] == "STORY-9")
    assert story["total_subtasks"] == 2
    assert story["open_subtasks"] == 1
    assert {item["issue_key"] for item in story["subtasks"]} == {"REL-1", "REL-2"}
