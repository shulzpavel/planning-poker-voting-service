from datetime import datetime, timezone

from app.domain.discovery_dashboard import build_discovery_snapshot


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def segment(status, start, end, current=False, assignee="PO"):
    return {"status": status, "entered_at": start, "left_at": end, "is_current": current, "assignee": assignee}


def issue(key, status, segments, assignee="PO"):
    return {"key": key, "summary": key, "status": status, "assignee": assignee,
            "history_complete": True, "segments": segments}


def test_snapshot_groups_current_work_by_executor_and_reports_age_signal():
    rows = [
        issue("DH-1", "Исследование", [segment("Исследование", "2026-10-01T12:00:00Z", NOW.isoformat(), True)], "PO A"),
        issue("DH-2", "Описание от PO", [segment("Описание от PO", "2026-09-01T12:00:00Z", NOW.isoformat(), True)], "PO A"),
        issue("DH-3", "backlog SA", [segment("backlog SA", "2026-09-20T12:00:00Z", NOW.isoformat(), True)], "PO A"),
        issue("DH-4", "Исследование", [segment("Исследование", "2026-10-20T12:00:00Z", NOW.isoformat(), True)], None),
    ]
    result = build_discovery_snapshot(rows, now=NOW, days=30)
    po_a = next(row for row in result["owners"] if row["name"] == "PO A")
    assert po_a["stage_count"] == 2
    assert po_a["status_counts"] == {"Исследование": 1, "Описание от PO": 1}
    assert po_a["backlog_sa_count"] == 1
    assert po_a["old_count"] == 2
    assert po_a["load_signal"] == "attention"
    assert result["metrics"]["stage_count"] == 3
    assert result["metrics"]["backlog_sa_count"] == 1
    assert result["metrics"]["return_count"] == 0
    research = next(row for row in result["status_stats"] if row["status"] == "Исследование")
    assert research["current"] == 2


def test_snapshot_marks_long_stage_and_keeps_unassigned_separate():
    rows = [issue("DH-1", "Исследование", [
        segment("Исследование", "2026-08-01T12:00:00Z", "2026-08-05T12:00:00Z"),
        segment("Описание от PO", "2026-08-05T12:00:00Z", NOW.isoformat(), True),
    ], None)]
    result = build_discovery_snapshot(rows, now=NOW, days=90)
    assert result["attention"][0]["key"] == "DH-1"
    assert result["attention"][0]["signal"] == "old_current_status"
    assert result["owners"][0]["name"] == "Без исполнителя"


def test_snapshot_marks_backwards_transition_as_return():
    rows = [issue("DH-5", "Исследование", [
        segment("Описание от PO", "2026-10-01T12:00:00Z", "2026-10-10T12:00:00Z"),
        segment("Исследование", "2026-10-10T12:00:00Z", NOW.isoformat(), True),
    ])]
    result = build_discovery_snapshot(rows, now=NOW, days=30)
    assert result["metrics"]["return_count"] == 1
    assert result["transitions"]["rows"][0]["return"] is True
