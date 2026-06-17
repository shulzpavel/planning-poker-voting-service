"""Tests for session summary CSV helpers."""

import csv
import io

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from config import UserRole
from services.voting_service.app_api import (
    _csv_ai_summary_fields,
    _csv_report,
    _markdown_report,
    _serialize_completed_task,
    _summary_payload,
)


def test_csv_ai_summary_fields_empty_when_missing() -> None:
    assert _csv_ai_summary_fields(None) == ("", "", "", "", "", "", "", "", "")
    assert _csv_ai_summary_fields({}) == ("", "", "", "", "", "", "", "", "")


def test_csv_ai_summary_fields_flattens_persisted_ai_result() -> None:
    fields = _csv_ai_summary_fields(
        {
            "description": "Line one\nLine two",
            "complexity": "Medium",
            "methods": ["API", "DB"],
            "sp_dev": 8,
            "sp_test": 5,
            "sp_final": 8,
            "confidence": "medium",
            "assumptions": ["Need fixtures", "Partner API stable"],
            "estimation_model": "max(sp_dev, sp_test)",
        }
    )
    assert fields == (
        "Line one Line two",
        "Medium",
        "API; DB",
        "8",
        "5",
        "8",
        "medium",
        "Need fixtures; Partner API stable",
        "max(sp_dev, sp_test)",
    )


def test_serialize_completed_task_includes_ai_summary() -> None:
    session = Session(chat_id=1, topic_id=None)
    session.participants[1] = Participant(user_id=1, name="dev@betboom.com", role=UserRole.PARTICIPANT)
    task = Task(
        summary="Auth flow",
        ai_summary={
            "description": "OAuth rollout",
            "methods": ["SSO"],
            "complexity": "High",
        },
    )
    task.votes[1] = "5"

    payload = _serialize_completed_task(session, task, bucket_index=0)

    assert payload["ai_summary"]["description"] == "OAuth rollout"


def test_summary_payload_excludes_reopened_task_from_completed_slice() -> None:
    session = Session(chat_id=1, topic_id=None)
    reopened = Task(summary="One", story_points=5)
    reopened.votes.clear()
    reopened.completed_at = None
    session.batch_completed = False
    session.tasks_queue = [reopened]
    session.current_task_index = 0
    session.current_batch_started_at = "2026-01-01T00:00:00"

    payload = _summary_payload(session, title="Session")

    assert payload["stats"]["total_completed"] == 0
    assert payload["stats"]["total_story_points"] == 0
    assert payload["completed_tasks"] == []


def test_summary_payload_includes_total_story_points() -> None:
    session = Session(chat_id=1, topic_id=None)
    session.batch_completed = True
    session.last_batch = [
        Task(summary="One", story_points=5),
        Task(summary="Skipped"),
        Task(summary="Two", story_points=8),
    ]

    payload = _summary_payload(session, title="Session")

    assert payload["stats"]["total_story_points"] == 13


def test_summary_payload_includes_post_finish_extension_tasks() -> None:
    session = Session(chat_id=1, topic_id=None)
    session.batch_completed = False
    session.last_batch = [Task(summary="Finished earlier", story_points=5)]
    session.tasks_queue = [
        Task(summary="New completed", story_points=8),
        Task(summary="Not played yet", story_points=13),
    ]
    session.current_task_index = 1

    payload = _summary_payload(session, title="Session")

    assert payload["stats"]["total_completed"] == 2
    assert payload["stats"]["total_story_points"] == 13
    assert [task["summary"] for task in payload["completed_tasks"]] == [
        "Finished earlier",
        "New completed",
    ]


def test_markdown_report_contains_confluence_summary() -> None:
    session = Session(chat_id=1, topic_id=None)
    session.participants[1] = Participant(user_id=1, name="dev@betboom.com", role=UserRole.PARTICIPANT)
    task = Task(
        jira_key="BB-1",
        summary="Checkout flow",
        url="https://jira.example/browse/BB-1",
        story_points=8,
        ai_summary={
            "description": "Payment rollout",
            "methods": ["API"],
            "complexity": "Medium",
            "sp_dev": 8,
            "sp_test": 5,
            "sp_final": 8,
            "confidence": "medium",
            "assumptions": ["Need PSP sandbox"],
            "estimation_model": "max(sp_dev, sp_test)",
        },
    )
    task.votes[1] = "8"
    session.batch_completed = True
    session.last_batch = [task]

    report = _markdown_report(_summary_payload(session, title="Sprint Planning"))

    assert "# Planning Poker: Sprint Planning" in report
    assert "**TOTAL SP:** 8" in report
    assert "[BB-1](https://jira.example/browse/BB-1)" in report
    assert "Payment rollout" in report
    assert "**AI estimate:** dev 8 SP, test 5 SP, final 8 SP" in report
    assert "**AI assumptions:** Need PSP sandbox" in report
    assert "**AI estimation model:** max(sp_dev, sp_test)" in report
    assert "\n---\n\n### 1. BB-1" in report
    assert report.rstrip().endswith("---")


def test_csv_report_is_sectioned_and_contains_total() -> None:
    session = Session(chat_id=1, topic_id=None)
    session.participants[1] = Participant(user_id=1, name="dev@betboom.com", role=UserRole.PARTICIPANT)
    task = Task(
        jira_key="BB-1",
        summary="Checkout flow",
        url="https://jira.example/browse/BB-1",
        story_points=8,
        ai_summary={
            "description": "Payment rollout",
            "methods": ["API"],
            "complexity": "Medium",
            "sp_dev": 8,
            "sp_test": 5,
            "sp_final": 8,
            "confidence": "medium",
            "assumptions": ["Need PSP sandbox"],
            "estimation_model": "max(sp_dev, sp_test)",
        },
    )
    task.votes[1] = "8"
    session.batch_completed = True
    session.last_batch = [task]

    rows = list(csv.reader(io.StringIO(_csv_report(_summary_payload(session, title="Sprint Planning")))))

    assert ["Planning Poker Report"] in rows
    assert ["TOTAL SP", "8"] in rows
    assert ["Estimation Method", "SP"] in rows
    assert ["Results By Task"] in rows
    assert [
        "1",
        "BB-1",
        "Checkout flow",
        "8",
        "SP",
        "Общая оценка (final 8 SP; 8×1; dev@betboom.com: 8)",
        "yes",
        "Payment rollout",
        "Medium",
        "API",
        "8",
        "5",
        "8",
        "medium",
        "Need PSP sandbox",
        "max(sp_dev, sp_test)",
        "https://jira.example/browse/BB-1",
        "",
    ] in rows
    assert ["Vote Details"] in rows
    assert ["1", "BB-1", "Checkout flow", "SP", "—", "—", "dev@betboom.com", "8"] in rows


def test_summary_report_includes_split_method_roles_tracks_and_votes() -> None:
    session = Session(chat_id=1, topic_id=None, estimation_mode="sp_dev_test")
    session.participants[1] = Participant(
        user_id=1,
        name="frontend@betboom.com",
        role=UserRole.PARTICIPANT,
        team_role="frontend",
    )
    session.participants[2] = Participant(
        user_id=2,
        name="qa@betboom.com",
        role=UserRole.PARTICIPANT,
        team_role="qa",
    )
    task = Task(
        jira_key="BB-2",
        summary="Split checkout",
        story_points_by_track={"dev": 8, "test": 5},
    )
    task.track_votes = {
        "dev": {1: "8"},
        "test": {2: "5"},
    }
    session.batch_completed = True
    session.last_batch = [task]

    summary = _summary_payload(session, title="Sprint Planning")

    assert summary["estimation_mode_label"] == "SP Dev / Test"
    assert summary["stats"]["total_story_points"] == 0
    assert summary["stats"]["total_story_points_by_track"] == {"dev": 8, "test": 5}
    assert summary["participants_detailed"] == [
        {"name": "frontend@betboom.com", "role": "frontend", "track": "dev", "track_label": "SP Dev"},
        {"name": "qa@betboom.com", "role": "qa", "track": "test", "track_label": "SP Test"},
    ]
    assert summary["completed_tasks"][0]["votes"] == [
        {
            "name": "frontend@betboom.com",
            "value": "8",
            "role": "frontend",
            "track": "dev",
            "track_label": "SP Dev",
        },
        {
            "name": "qa@betboom.com",
            "value": "5",
            "role": "qa",
            "track": "test",
            "track_label": "SP Test",
        },
    ]

    markdown = _markdown_report(summary)
    assert "**Estimation method:** SP Dev / Test" in markdown
    assert "SP Dev (final 8 SP; 8×1; frontend@betboom.com [frontend]: 8)" in markdown
    assert "| SP Dev | frontend | frontend@betboom.com | 8 |" in markdown
    assert "| SP Test | qa | qa@betboom.com | 5 |" in markdown

    rows = list(csv.reader(io.StringIO(_csv_report(summary))))
    assert ["Split SP totals", "dev: 8, test: 5"] in rows
    assert ["frontend@betboom.com", "frontend", "SP Dev"] in rows
    assert [
        "1",
        "BB-2",
        "Split checkout",
        "dev: 8, test: 5",
        "SP Dev / Test",
        "SP Dev (final 8 SP; 8×1; frontend@betboom.com [frontend]: 8) | SP Test (final 5 SP; 5×1; qa@betboom.com [qa]: 5)",
        "no",
        "—",
        "—",
        "—",
        "—",
        "—",
        "—",
        "—",
        "—",
        "—",
        "",
        "",
    ] in rows
    assert ["1", "BB-2", "Split checkout", "SP Dev / Test", "SP Dev", "frontend", "frontend@betboom.com", "8"] in rows
