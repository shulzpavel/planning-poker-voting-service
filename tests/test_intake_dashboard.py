from datetime import datetime, timezone

from app.domain.intake_dashboard import build_intake_snapshot


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def issue(key, segments, status="Исследование", complete=True):
    return {"key": key, "summary": key, "status": status, "assignee": "PO",
            "source": "Product Owner", "history_complete": complete, "segments": segments}


def segment(status, start, end, current=False):
    return {"status": status, "entered_at": start, "left_at": end, "is_current": current}


def test_snapshot_merges_intake_statuses_without_counting_information_as_a_decision():
    rows = [issue("DH-1", [
        segment("Новый запрос", "2026-10-10T12:00:00Z", "2026-10-12T12:00:00Z"),
        segment("Нужна информация", "2026-10-12T12:00:00Z", "2026-10-15T12:00:00Z"),
        segment("Исследование", "2026-10-15T12:00:00Z", NOW.isoformat(), True),
    ]), issue("DH-2", [segment("Новый запрос", "2026-10-20T12:00:00Z", NOW.isoformat(), True)], "Новый запрос")]
    result = build_intake_snapshot(rows, now=NOW, days=30)
    assert result["metrics"] == {"arrivals": 2, "decisions": 1, "pending": 1,
                                  "p50_days": 5.0, "p85_days": 5.0, "sle_days": None,
                                  "within_sle_pct": None, "over_sle": None, "baseline_n": 0}
    assert result["aging"][0]["age_days"] == 9.0
    assert sum(x["decisions"] for x in result["daily"]) == 1


def test_return_visit_is_separate_and_dispositions_do_not_disappear():
    row = issue("DH-1", [
        segment("Новый запрос", "2026-10-03T12:00:00Z", "2026-10-04T12:00:00Z"),
        segment("Отложено", "2026-10-04T12:00:00Z", "2026-10-10T12:00:00Z"),
        segment("Новый запрос", "2026-10-10T12:00:00Z", "2026-10-12T12:00:00Z"),
        segment("Отклонено", "2026-10-12T12:00:00Z", NOW.isoformat(), True),
    ], "Отклонено")
    result = build_intake_snapshot([row], now=NOW, days=30)
    assert result["metrics"]["decisions"] == 2
    assert result["returns"] == 1
    assert {x["outcome"] for x in result["completed"]} == {"Отложено", "Отклонено"}
    assert result["metrics"]["pending"] == 0


def test_sle_uses_only_twenty_completed_visits_before_window():
    rows = [issue(f"DH-{n}", [
        segment("Новый запрос", "2026-08-01T12:00:00Z", "2026-08-03T12:00:00Z"),
        segment("Исследование", "2026-08-03T12:00:00Z", NOW.isoformat(), True),
    ]) for n in range(20)]
    rows.append(issue("DH-99", [
        segment("Новый запрос", "2026-10-01T12:00:00Z", "2026-10-10T12:00:00Z"),
        segment("Исследование", "2026-10-10T12:00:00Z", NOW.isoformat(), True),
    ]))
    result = build_intake_snapshot(rows, now=NOW, days=30)
    assert result["metrics"]["sle_days"] == 2.0
    assert result["metrics"]["baseline_n"] == 20
    assert result["metrics"]["within_sle_pct"] == 0.0
    assert result["metrics"]["p85_days"] == 9.0


def test_partial_history_counts_current_queue_but_never_invents_age():
    row = issue("DH-1", [], "Нужна информация", complete=False)
    result = build_intake_snapshot([row], now=NOW, days=7)
    assert result["metrics"]["pending"] == 1
    assert result["aging"][0]["age_days"] is None
    assert result["coverage"]["complete"] == 0
    assert result["metrics"]["p85_days"] is None


def test_daily_flow_conserves_queue_and_includes_end_day():
    row = issue("DH-1", [segment("Новый запрос", "2026-10-29T10:00:00Z", NOW.isoformat(), True)], "Новый запрос")
    result = build_intake_snapshot([row], now=NOW, days=7)
    assert len(result["daily"]) == 7
    assert result["daily"][-1]["date"] == "2026-10-29"
    assert result["daily"][-1]["queue"] == 1
    assert result["metrics"]["arrivals"] == 1


def test_monthly_report_counts_new_requests_once_and_keeps_old_queue_separate():
    rows = [issue("DH-new", [
        segment("Новый запрос", "2026-10-03T12:00:00Z", "2026-10-04T12:00:00Z"),
        segment("Исследование", "2026-10-04T12:00:00Z", "2026-10-10T12:00:00Z"),
        segment("Новый запрос", "2026-10-10T12:00:00Z", NOW.isoformat(), True),
    ], "Новый запрос"), issue("DH-old", [
        segment("Новый запрос", "2026-08-01T12:00:00Z", NOW.isoformat(), True),
    ], "Новый запрос")]
    report = build_intake_snapshot(rows, now=NOW, days=30)["report"]
    assert report["received"] == 1
    assert report["decided"] == 1
    assert report["returned"] == 1
    assert sum(x["count"] for x in report["outcomes"]) == 1
    assert next(x for x in report["outcomes"] if x["id"] == "waiting")["keys"] == ["DH-new"]
    assert sum(x["count"] for x in report["current_age"]) == 2
    assert report["queue_start"] == 1
    assert report["queue_end"] == 2


def test_report_duration_bands_reconcile_with_unique_first_decisions():
    rows = [issue("DH-1", [
        segment("Новый запрос", "2026-10-01T12:00:00Z", "2026-10-03T12:00:00Z"),
        segment("Исследование", "2026-10-03T12:00:00Z", NOW.isoformat(), True),
    ]), issue("DH-2", [
        segment("Новый запрос", "2026-10-05T12:00:00Z", "2026-10-15T12:00:00Z"),
        segment("Отложено", "2026-10-15T12:00:00Z", NOW.isoformat(), True),
    ], "Отложено")]
    report = build_intake_snapshot(rows, now=NOW, days=30)["report"]
    assert report["received"] == report["decided"] == 2
    assert sum(x["count"] for x in report["resolution_bands"]) == 2
    assert sum(x["received"] for x in report["weekly"]) == 2
    assert sum(x["decided"] for x in report["weekly"]) == 2
    assert {x["id"]: x["count"] for x in report["outcomes"]} == {"forward": 1, "deferred": 1, "rejected": 0, "waiting": 0}


def test_initiative_sources_count_unique_new_requests_and_keep_missing_values():
    rows = [issue("DH-1", [
        segment("Новый запрос", "2026-10-02T12:00:00Z", "2026-10-03T12:00:00Z"),
        segment("Исследование", "2026-10-03T12:00:00Z", "2026-10-10T12:00:00Z"),
        segment("Новый запрос", "2026-10-10T12:00:00Z", NOW.isoformat(), True),
    ], "Новый запрос"), issue("DH-2", [
        segment("Новый запрос", "2026-10-05T12:00:00Z", NOW.isoformat(), True),
    ], "Новый запрос"), issue("DH-old", [
        segment("Новый запрос", "2026-07-01T12:00:00Z", NOW.isoformat(), True),
    ], "Новый запрос")]
    rows[0]["source"] = " SEO "
    rows[0]["assignee"] = "Different person"
    rows[1]["source"] = None
    rows[2]["source"] = "Product Owner"
    report = build_intake_snapshot(rows, now=NOW, days=30)["report"]
    sources = {x["label"]: x for x in report["sources"]}
    assert set(sources) == {"SEO", "Не указан"}
    assert sources["SEO"]["keys"] == ["DH-1"]
    assert sources["Не указан"]["count"] == 1
    assert sum(x["count"] for x in report["sources"]) == report["received"] == 2
