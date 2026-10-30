from datetime import datetime
import pytest
from app.domain.intake_speed import build_intake_speed

START = datetime.fromisoformat("2026-10-23T00:00:00+02:00")
NOW = datetime.fromisoformat("2026-10-29T16:00:00+01:00")
ROWS = [{"key": "A", "entered_at": "2026-10-22T22:00:00+00:00", "left_at": "2026-10-23T00:00:00+00:00"},
        {"key": "B", "entered_at": "2026-10-22T22:00:00+00:00", "left_at": "2026-10-25T22:00:00+00:00"},
        {"key": "OPEN", "entered_at": "2026-10-23T00:00:00+00:00", "left_at": None},
        {"key": "PREV", "entered_at": "2026-10-16T22:00:00+00:00", "left_at": "2026-10-18T22:00:00+00:00"}]

def result(rows=ROWS):
    return build_intake_speed({"requests": rows, "received": 3, "weekly": [{"start": "2026-10-23", "end": "2026-10-25"}, {"start": "2026-10-26", "end": "2026-10-29"}]}, start=START, now=NOW, timezone="Europe/Belgrade")

def test_equal_elapsed_baseline_excludes_open_requests():
    value = result()
    assert value["current"]["count"] == 2
    assert value["current"]["median"] == pytest.approx((2/24+3)/2)
    assert value["current"]["withinDay"] == 50
    assert value["previous"]["median"] == 2
    assert value["start"]-value["previousStart"] == value["end"]-value["start"]

def test_belgrade_dst_weekly_buckets_and_empty_gaps():
    weeks = result()["weekly"]
    assert [x["keys"] for x in weeks] == [["A", "B"], []]
    assert weeks[1]["median"] is None
    assert weeks[0]["partial"] is True

def test_no_previous_completions_is_not_a_zero_duration():
    value = result([x for x in ROWS if x["key"] != "PREV"])
    assert value["previous"]["median"] is None
    assert value["previous"]["withinDay"] is None
