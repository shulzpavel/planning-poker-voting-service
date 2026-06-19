from datetime import datetime, timezone
from typing import Optional

from app.domain.scope_flow_pace import (
    FLOW_PACE_TEAM_SLUGS,
    collect_flow_pace_issues,
    collect_flow_pace_scope,
    compute_scope_flow_pace,
    extract_parent_epic_from_jql,
    is_flow_pace_enabled,
)


def _issue(
    key: str,
    *,
    status: str = "В работе",
    category: str = "indeterminate",
    start_date: Optional[str] = None,
    created: Optional[str] = None,
    resolution_date: Optional[str] = None,
    due_date: Optional[str] = None,
    status_entered_at: Optional[str] = None,
    current_status_days: Optional[float] = None,
    story_points: Optional[float] = 3,
    assignee: str = "Dev A",
    parent_key: str = "FLEX-2861",
) -> dict:
    payload = {
        "key": key,
        "summary": key,
        "status": status,
        "status_category": category,
        "start_date": start_date,
        "created": created,
        "resolution_date": resolution_date,
        "due_date": due_date,
        "status_entered_at": status_entered_at,
        "story_points": story_points,
        "assignee": assignee,
        "parent_key": parent_key,
    }
    if current_status_days is not None:
        payload["current_status_days"] = current_status_days
    return payload


def _snapshot(*sections_issues: tuple[str, str, list[dict]]) -> dict:
    sections = []
    for section_id, jql, issues in sections_issues:
        kind = "planned" if section_id == "plan" else "unplanned"
        sections.append(
            {
                "id": section_id,
                "name": section_id.capitalize(),
                "kind": kind,
                "jql": jql,
                "issues": issues,
            }
        )
    return {"sections": sections}


def test_extract_parent_epic_from_jql():
    assert extract_parent_epic_from_jql("parent=FLEX-2861") == "FLEX-2861"
    assert extract_parent_epic_from_jql("project = FLEX AND parent = FLEX-2862") == "FLEX-2862"


def test_flow_pace_enabled_only_for_igaming_rip():
    assert is_flow_pace_enabled("igaming-rip")
    assert FLOW_PACE_TEAM_SLUGS == frozenset({"igaming-rip"})


def test_collect_flow_pace_scope_only_plan_unplan_sections():
    snapshot = _snapshot(
        ("plan", "parent=FLEX-2861", [_issue("FLEX-1")]),
        ("unplan", "parent=FLEX-2862", [_issue("FLEX-2", parent_key="FLEX-2862")]),
    )
    snapshot["priority_queues"] = {
        "todo": {"issues": [_issue("FLEX-99")]},
        "test": {"issues": [_issue("FLEX-100", status="Тестирование")]},
    }
    issues, epics = collect_flow_pace_scope(snapshot)
    assert {issue["key"] for issue in issues} == {"FLEX-1", "FLEX-2"}
    assert len(epics) == 2
    assert issues[0]["flow_epic_key"] == "FLEX-2861"


def test_throughput_counts_done_tasks_by_resolution_date():
    now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue("FLEX-1", status="Готово", category="done", start_date="2026-06-01", resolution_date="2026-06-18"),
                _issue("FLEX-2", status="Готово", category="done", start_date="2026-06-05", resolution_date="2026-06-15"),
                _issue("FLEX-3", status="Готово", category="done", start_date="2026-05-01", resolution_date="2026-05-20"),
            ],
        ),
    )
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    assert result is not None
    assert result["summary"]["done_last_7d"] == 2


def test_stalled_alert_high_after_10_days():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-10",
                    status="К выполнению",
                    category="new",
                    current_status_days=18.0,
                )
            ],
        ),
    )
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    stalled = [item for item in result["alerts"] if item["kind"] == "stalled"]
    assert stalled and stalled[0]["severity"] == "high"


def test_stuck_in_test_high_after_7_days():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-20",
                    status="Тестирование",
                    category="indeterminate",
                    current_status_days=9.0,
                    assignee="QA Person",
                )
            ],
        ),
    )
    issues = snapshot["sections"][0]["issues"]
    issues[0]["role_contributors"] = {"qa": {"name": "QA Person"}}
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    test_alerts = [item for item in result["alerts"] if item["kind"] == "stuck_in_test"]
    assert test_alerts[0]["severity"] == "high"
    assert test_alerts[0]["highlight_name"] == "QA Person"


def test_epic_stalled_when_no_done_in_7_days():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue("FLEX-A", status="В работе", current_status_days=2.0),
                _issue("FLEX-B", status="Тестирование", current_status_days=2.0),
            ],
        ),
    )
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    assert any(item["kind"] == "epic_stalled" and item["epic_key"] == "FLEX-2861" for item in result["alerts"])


def test_stuck_before_release_for_ready_status():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-2112",
                    status="К релизу",
                    category="indeterminate",
                    current_status_days=30.3,
                    assignee="Александр Катанский",
                )
            ],
        ),
    )
    issues = snapshot["sections"][0]["issues"]
    issues[0]["summary"] = "Доработка KYC с запросом обновления документов"
    issues[0]["role_contributors"] = {"qa": {"name": "Александр Катанский"}}
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    release_alerts = [item for item in result["alerts"] if item["issue_key"] == "FLEX-2112"]
    assert release_alerts
    assert release_alerts[0]["kind"] == "stuck_before_release"
    assert release_alerts[0]["title"] == "Долго ждёт релиза"
    assert "К релизу" in release_alerts[0]["detail"]
    assert "тестировании" not in release_alerts[0]["title"].lower()


def test_done_issues_do_not_generate_alerts():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "unplan",
            "parent=FLEX-2862",
            [
                {
                    **_issue("FLEX-1307", status="Готово", category="done", assignee="QA"),
                    "summary": "Добавить название игры в рамку",
                    "status_bucket_durations": {"dev": 0.2, "test": 251.0},
                    "status_durations": {"К тестированию": 251.0, "Done": 8.1},
                    "role_contributors": {"qa": {"name": "Сергей Баранов"}},
                }
            ],
        ),
    )
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    issue_alerts = [item for item in result["alerts"] if item.get("issue_key") == "FLEX-1307"]
    assert issue_alerts == []


def test_missing_start_date_severity_by_days():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot_low = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [_issue("FLEX-2865", status="In Progress", current_status_days=3.0)],
        ),
    )
    result_low = compute_scope_flow_pace(snapshot_low, team_slug="igaming-rip", now=now)
    low_alert = next(item for item in result_low["alerts"] if item["issue_key"] == "FLEX-2865")
    assert low_alert["kind"] == "missing_start_date"
    assert low_alert["severity"] == "low"
    assert "Low ≥3д" in low_alert["criteria"]

    snapshot_stall = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [_issue("FLEX-2673", status="In Progress", current_status_days=18.0)],
        ),
    )
    result_stall = compute_scope_flow_pace(snapshot_stall, team_slug="igaming-rip", now=now)
    kinds = [item["kind"] for item in result_stall["alerts"] if item["issue_key"] == "FLEX-2673"]
    assert "stuck_in_dev" in kinds
    assert "missing_start_date" not in kinds


def test_missing_start_date_suppressed_when_stall_alert_present():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [_issue("FLEX-2673", status="In Progress", current_status_days=18.0)],
        ),
    )
    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    kinds = [item["kind"] for item in result["alerts"] if item["issue_key"] == "FLEX-2673"]
    assert "stuck_in_dev" in kinds
    assert "missing_start_date" not in kinds


def test_flow_pace_charts_from_done_issues():
    now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue("FLEX-1", status="Готово", category="done", start_date="2026-06-10", resolution_date="2026-06-18"),
                _issue("FLEX-2", status="Готово", category="done", start_date="2026-05-01", resolution_date="2026-05-10"),
                _issue("FLEX-3", status="In Progress", current_status_days=3.0),
            ],
        ),
        (
            "unplan",
            "parent=FLEX-2862",
            [_issue("FLEX-4", status="Готово", category="done", start_date="2026-06-12", resolution_date="2026-06-19", parent_key="FLEX-2862")],
        ),
    )
    for issue in snapshot["sections"][0]["issues"][:2]:
        issue["status_bucket_durations"] = {"dev": 3.0, "test": 2.0, "pause": 0.0}
    snapshot["sections"][1]["issues"][0]["status_bucket_durations"] = {"dev": 1.0, "test": 4.0, "pause": 1.0}

    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    charts = result["charts"]["donuts"]
    assert len(charts) == 6
    done_mix = next(item for item in charts if item["id"] == "done_mix")
    assert done_mix["center_value"] == "3"
    plan_segment = next(item for item in done_mix["segments"] if item["key"] == "plan")
    unplan_segment = next(item for item in done_mix["segments"] if item["key"] == "unplan")
    assert plan_segment["value"] == 2
    assert unplan_segment["value"] == 1
    cycle = next(item for item in charts if item["id"] == "cycle_time")
    assert cycle["center_value"] != "—"
    assert not any(segment["key"] == "empty" for segment in cycle["segments"])
    qa_iterations = next(item for item in charts if item["id"] == "qa_iterations")
    assert qa_iterations["center_label"] == "QA-heavy"
    signals = next(item for item in charts if item["id"] == "active_signals")
    assert signals["center_label"] == "сигналов"
    assert "methodology" in cycle
    assert "detail_segments" in cycle


def test_phase_time_chart_has_only_work_phases():
    now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-10",
                    status="Готово",
                    category="done",
                    start_date="2026-06-01",
                    resolution_date="2026-06-10",
                ),
            ],
        ),
    )
    snapshot["sections"][0]["issues"][0]["status_bucket_durations"] = {
        "dev": 3.0,
        "test": 2.0,
        "pause": 1.0,
        "todo": 5.0,
        "done": 2.0,
        "other": 4.0,
    }

    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    phase = next(item for item in result["charts"]["donuts"] if item["id"] == "phase_time")
    segment_keys = {segment["key"] for segment in phase["segments"]}
    assert segment_keys == {"dev", "test", "pause"}
    assert phase["segments"][0]["value"] == 3.0
    assert "Не фазы" in phase["methodology"]
    detail_keys = {segment["key"] for segment in phase["detail_segments"]}
    assert detail_keys == {"dev", "test", "pause"}
    assert all("Прочее" not in item.get("metric_label", "") for segment in phase["detail_segments"] for item in segment["items"])


def test_phase_detail_includes_status_breakdown():
    now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-11",
                    status="Готово",
                    category="done",
                    start_date="2026-06-01",
                    resolution_date="2026-06-10",
                ),
            ],
        ),
    )
    issue = snapshot["sections"][0]["issues"][0]
    issue["status_bucket_durations"] = {"dev": 5.0, "test": 3.0, "pause": 0.0, "todo": 2.0}
    issue["status_durations"] = {"В работе": 5.0, "Тестирование": 3.0, "К выполнению": 2.0}
    issue["status_flow_bucket_map"] = {
        "В работе": "dev",
        "Тестирование": "test",
        "К выполнению": "todo",
    }

    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    phase = next(item for item in result["charts"]["donuts"] if item["id"] == "phase_time")
    dev_segment = next(segment for segment in phase["detail_segments"] if segment["key"] == "dev")
    assert "В работе 5.0д" in dev_segment["items"][0]["detail"]
    assert "Не фазы: К выполнению 2.0д" in dev_segment["items"][0]["detail"]
    now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(
        (
            "plan",
            "parent=FLEX-2861",
            [
                _issue(
                    "FLEX-2571",
                    status="Готово",
                    category="done",
                    created="2026-04-28",
                    resolution_date="2026-06-08",
                    story_points=5,
                ),
            ],
        ),
    )
    snapshot["sections"][0]["issues"][0]["status_bucket_durations"] = {"dev": 7.1, "test": 33.9, "pause": 0.93}

    result = compute_scope_flow_pace(snapshot, team_slug="igaming-rip", now=now)
    cycle = next(item for item in result["charts"]["donuts"] if item["id"] == "cycle_time")
    assert cycle["center_value"] != "—"
    assert result["summary"]["median_cycle_days"] is not None
    assert result["summary"]["median_cycle_days"] >= 40


def test_collect_flow_pace_issues_wrapper():
    snapshot = _snapshot(("plan", "parent=FLEX-2861", [_issue("FLEX-1")]))
    assert len(collect_flow_pace_issues(snapshot)) == 1


def test_normalize_flow_pace_chart_order():
    from app.domain.scope_flow_pace import normalize_flow_pace_chart_order, reorder_flow_pace_charts

    assert normalize_flow_pace_chart_order(["active_signals", "done_mix", "unknown"])[:2] == [
        "active_signals",
        "done_mix",
    ]
    charts = {
        "donuts": [
            {"id": "done_mix", "title": "A"},
            {"id": "throughput", "title": "B"},
            {"id": "active_signals", "title": "C"},
        ]
    }
    reordered = reorder_flow_pace_charts(charts, ["active_signals", "throughput", "done_mix"])
    assert [item["id"] for item in reordered["donuts"]] == ["active_signals", "throughput", "done_mix"]
