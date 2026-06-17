"""Tests for estimation mode policy."""

import pytest

from app.domain.estimation import (
    DEFAULT_ESTIMATION_MODE,
    all_voters_have_voted,
    build_flat_results,
    cast_vote_value,
    clear_task_votes,
    normalise_estimation_mode,
    participant_has_voted,
    resolve_track,
)
from app.domain.participant import Participant
from app.domain.session import Session, SessionFactory
from app.domain.task import Task
from config import UserRole


def _session_with_voters(mode: str = "sp") -> Session:
    session = Session(chat_id=1, topic_id=None, estimation_mode=mode)
    session.tasks_queue = [Task(summary="Estimate checkout")]
    session.current_batch_started_at = "now"
    session.participants[10] = Participant(10, "Frontend Dev", UserRole.PARTICIPANT, team_role="frontend")
    session.participants[20] = Participant(20, "QA", UserRole.PARTICIPANT, team_role="qa")
    return session


class TestEstimationPolicy:
    def test_legacy_session_defaults_to_sp(self):
        loaded = SessionFactory.from_dict({"chat_id": 1, "topic_id": None})
        assert loaded.estimation_mode == DEFAULT_ESTIMATION_MODE

    def test_task_roundtrip_preserves_track_votes(self):
        task = Task(summary="Split task")
        task.track_votes = {"dev": {10: "5"}, "test": {20: "3"}}
        task.story_points_by_track = {"dev": 5, "test": 3}
        restored = Task.from_dict(task.to_dict())
        assert restored.track_votes["dev"][10] == "5"
        assert restored.story_points_by_track["test"] == 3

    def test_resolve_track_for_dev_test_mode(self):
        assert resolve_track("sp_dev_test", "frontend") == "dev"
        assert resolve_track("sp_dev_test", "backend") == "dev"
        assert resolve_track("sp_dev_test", "qa") == "test"

    def test_resolve_track_for_split_mode(self):
        assert resolve_track("sp_split", "frontend") == "front"
        assert resolve_track("sp_split", "backend") == "back"
        assert resolve_track("sp_split", "qa") == "qa"

    def test_legacy_sp_vote_uses_flat_votes(self):
        session = _session_with_voters("sp")
        task = session.current_task
        cast_vote_value(task, session.estimation_mode, 10, None, "8")
        assert task.votes[10] == "8"
        assert participant_has_voted(session, task, 10) is True
        assert all_voters_have_voted(session, task) is False

    def test_split_mode_requires_track_votes(self):
        session = _session_with_voters("sp_dev_test")
        task = session.current_task
        cast_vote_value(task, session.estimation_mode, 10, "dev", "5")
        cast_vote_value(task, session.estimation_mode, 20, "test", "3")
        assert participant_has_voted(session, task, 10) is True
        assert all_voters_have_voted(session, task) is True
        results = build_flat_results(session, task)
        assert len(results) == 2
        assert {row["track"] for row in results} == {"dev", "test"}

    def test_clear_task_votes_respects_mode(self):
        task = Task(summary="Reset")
        task.votes[1] = "5"
        task.track_votes = {"dev": {1: "5"}}
        clear_task_votes(task, "sp_dev_test")
        assert task.votes == {}
        assert task.track_votes == {}

    def test_clear_task_votes_drops_stale_track_votes_in_sp_mode(self):
        # Mode switched split -> sp between rounds: stale track votes must go.
        task = Task(summary="Reset")
        task.votes[1] = "5"
        task.track_votes = {"dev": {1: "5"}}
        clear_task_votes(task, "sp")
        assert task.votes == {}
        assert task.track_votes == {}

    def test_normalise_invalid_mode(self):
        assert normalise_estimation_mode("unknown") == DEFAULT_ESTIMATION_MODE

    def test_web_state_phase_for_split_mode(self):
        pytest.importorskip("redis")
        from services.voting_service.web_api import _build_web_session_state

        session = _session_with_voters("sp_dev_test")
        task = session.current_task
        cast_vote_value(task, session.estimation_mode, 10, "dev", "5")

        state = _build_web_session_state(session)
        assert state["phase"] == "voting"
        assert state["estimation_mode"] == "sp_dev_test"
        assert state["participants"][0]["track"] == "dev"

        cast_vote_value(task, session.estimation_mode, 20, "test", "3")
        state = _build_web_session_state(session)
        assert state["phase"] == "results"
        assert state["track_results"]["dev"][0]["value"] == "5"
