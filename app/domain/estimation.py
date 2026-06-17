"""Estimation mode policy for planning poker sessions.

Modes are preset-based and additive: legacy ``sp`` keeps using ``Task.votes``
unchanged. Split modes store votes per track in ``Task.track_votes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.participant import Participant
    from app.domain.session import Session
    from app.domain.task import Task

DEFAULT_ESTIMATION_MODE = "sp"
VALID_ESTIMATION_MODES = frozenset({"sp", "sp_dev_test", "sp_split"})
MAX_STORY_POINTS = 21
SPECIAL_VOTE_VALUES = frozenset({"?", "skip", "needs_review"})

# Participant team roles (web join) -> estimation track per mode.
ROLE_TO_TRACK: dict[str, dict[str, str]] = {
    "sp_dev_test": {
        "frontend": "dev",
        "backend": "dev",
        "qa": "test",
    },
    "sp_split": {
        "frontend": "front",
        "backend": "back",
        "qa": "qa",
    },
}


@dataclass(frozen=True)
class EstimationTrack:
    key: str
    label: str


@dataclass(frozen=True)
class EstimationModeConfig:
    mode: str
    label: str
    description: str
    tracks: tuple[EstimationTrack, ...]


MODE_CONFIGS: dict[str, EstimationModeConfig] = {
    "sp": EstimationModeConfig(
        mode="sp",
        label="SP",
        description="Единая оценка Story Points для всей команды.",
        tracks=(),
    ),
    "sp_dev_test": EstimationModeConfig(
        mode="sp_dev_test",
        label="SP Dev / Test",
        description="Раздельная оценка разработки и тестирования.",
        tracks=(
            EstimationTrack("dev", "SP Dev"),
            EstimationTrack("test", "SP Test"),
        ),
    ),
    "sp_split": EstimationModeConfig(
        mode="sp_split",
        label="SP Front / Back / QA",
        description="Раздельная оценка по направлениям команды.",
        tracks=(
            EstimationTrack("front", "SP Front"),
            EstimationTrack("back", "SP Back"),
            EstimationTrack("qa", "SP QA"),
        ),
    ),
}


def normalise_estimation_mode(mode: Optional[str]) -> str:
    if not mode or mode not in VALID_ESTIMATION_MODES:
        return DEFAULT_ESTIMATION_MODE
    return mode


def get_mode_config(mode: Optional[str]) -> EstimationModeConfig:
    return MODE_CONFIGS[normalise_estimation_mode(mode)]


def is_split_mode(mode: Optional[str]) -> bool:
    return normalise_estimation_mode(mode) != DEFAULT_ESTIMATION_MODE


def resolve_track(mode: Optional[str], team_role: Optional[str]) -> Optional[str]:
    """Map a participant team role to the track they vote in."""
    normalised = normalise_estimation_mode(mode)
    if normalised == DEFAULT_ESTIMATION_MODE:
        return None
    if not team_role:
        return None
    return ROLE_TO_TRACK.get(normalised, {}).get(team_role)


def resolve_track_for_participant(session: Session, user_id: int) -> Optional[str]:
    participant = session.participants.get(user_id)
    team_role = participant.team_role if participant else None
    return resolve_track(session.estimation_mode, team_role)


def is_valid_vote_value(value: str) -> bool:
    if value in SPECIAL_VOTE_VALUES:
        return True
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return False
    return 0 <= numeric <= MAX_STORY_POINTS


def cast_vote_value(task: Task, mode: Optional[str], user_id: int, track: Optional[str], value: str) -> None:
    """Persist a vote for the active mode."""
    if normalise_estimation_mode(mode) == DEFAULT_ESTIMATION_MODE:
        task.votes[user_id] = value
        return
    if not track:
        raise ValueError("Track is required for split estimation modes")
    bucket = task.track_votes.setdefault(track, {})
    bucket[user_id] = value


def participant_has_voted(session: Session, task: Task, user_id: int) -> bool:
    if not session.can_vote(user_id):
        return False
    if normalise_estimation_mode(session.estimation_mode) == DEFAULT_ESTIMATION_MODE:
        return user_id in task.votes
    track = resolve_track_for_participant(session, user_id)
    if not track:
        return False
    return user_id in task.track_votes.get(track, {})


def all_voters_have_voted(session: Session, task: Task) -> bool:
    voter_ids = [uid for uid in session.participants if session.can_vote(uid)]
    if not voter_ids:
        return False
    return all(participant_has_voted(session, task, uid) for uid in voter_ids)


def get_participant_vote_value(session: Session, task: Task, user_id: int) -> Optional[str]:
    if normalise_estimation_mode(session.estimation_mode) == DEFAULT_ESTIMATION_MODE:
        return task.votes.get(user_id)
    track = resolve_track_for_participant(session, user_id)
    if not track:
        return None
    return task.track_votes.get(track, {}).get(user_id)


def clear_task_votes(task: Task, mode: Optional[str] = None) -> None:
    """Reset all recorded votes on a task.

    ``track_votes`` is cleared regardless of the current mode so that
    switching the estimation mode between rounds never leaves stale
    per-track votes behind. ``mode`` is kept for call-site compatibility.
    """
    task.votes.clear()
    task.track_votes.clear()


def build_track_results(session: Session, task: Task) -> Optional[dict[str, list[dict[str, str]]]]:
    if not is_split_mode(session.estimation_mode):
        return None
    config = get_mode_config(session.estimation_mode)
    out: dict[str, list[dict[str, str]]] = {}
    for track in config.tracks:
        votes_for_track = task.track_votes.get(track.key, {})
        out[track.key] = [
            {
                "name": session.participants[uid].name,
                "value": value,
            }
            for uid, value in votes_for_track.items()
            if uid in session.participants
        ]
    return out


def build_flat_results(session: Session, task: Task) -> list[dict[str, str]]:
    """Legacy-compatible flat results list."""
    if normalise_estimation_mode(session.estimation_mode) == DEFAULT_ESTIMATION_MODE:
        return [
            {"name": session.participants[uid].name, "value": val}
            for uid, val in task.votes.items()
            if uid in session.participants
        ]
    rows: list[dict[str, str]] = []
    config = get_mode_config(session.estimation_mode)
    for track in config.tracks:
        for uid, val in task.track_votes.get(track.key, {}).items():
            if uid not in session.participants:
                continue
            rows.append(
                {
                    "name": session.participants[uid].name,
                    "value": val,
                    "track": track.key,
                    "track_label": track.label,
                }
            )
    return rows


def estimation_mode_payload(mode: Optional[str]) -> dict:
    config = get_mode_config(mode)
    return {
        "estimation_mode": config.mode,
        "estimation_mode_label": config.label,
        "estimation_mode_description": config.description,
        "estimation_tracks": [
            {"key": track.key, "label": track.label}
            for track in config.tracks
        ],
    }
