"""Use case for casting a vote from the public web voting UI.

Wraps the existing :class:`CastVoteUseCase` machinery but raises structured
``WebVoteError`` exceptions instead of returning a bare boolean, so the
HTTP layer can translate them into 400/403 responses without re-deriving
the rejection reason. The atomic fast path (``SessionRepository.cast_vote_atomic``)
is kept for repositories that implement it, but only for the flat ``sp``
mode — split estimation modes always use the read-modify-write mutator so
votes land in the correct track.
"""

from __future__ import annotations

from typing import Optional

from app.domain.estimation import (
    cast_vote_value,
    is_valid_vote_value,
    is_split_mode,
    normalise_estimation_mode,
    resolve_track,
    resolve_track_for_participant,
)
from app.domain.session import Session
from app.ports.session_repository import SessionRepository


class WebVoteError(Exception):
    """Raised by ``WebVoteUseCase`` to signal why a vote was rejected.

    The ``status_code`` mirrors what the HTTP handler should surface
    (400 for state errors, 403 for authorization). Keeps the use case
    free of FastAPI imports.
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class WebVoteUseCase:
    """Cast a vote on the currently active task in a web session."""

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        user_id: int,
        vote_value: str,
        track: Optional[str] = None,
    ) -> Session:
        """Persist the vote and return the post-mutation session.

        Raises ``WebVoteError`` when the session has no active task, the
        participant is not allowed to vote, or (atomic path only) when the
        repository rejects the write for an unknown reason.
        """
        if not is_valid_vote_value(vote_value):
            raise WebVoteError("Vote value must be between 0 and 21 SP", status_code=400)

        repo = self.session_repo

        # Atomic fast path. It writes straight into ``Task.votes`` and knows
        # nothing about estimation tracks, so it is only safe for the legacy
        # flat ``sp`` mode — split modes always go through ``mutate_session``.
        # It returns a bare bool, so on failure we re-read the session to
        # derive the precise reason.
        if hasattr(repo, "cast_vote_atomic"):
            session = await _load_session(repo, chat_id, topic_id)
            if not is_split_mode(session.estimation_mode):
                ok = await repo.cast_vote_atomic(chat_id, topic_id, user_id, vote_value)  # type: ignore[attr-defined]
                if ok:
                    return await _load_session(repo, chat_id, topic_id)
                session = await _load_session(repo, chat_id, topic_id)
                _raise_rejection_reason(session, user_id, track)
                # _raise_rejection_reason always raises; keep mypy/IDE happy.
                raise WebVoteError("Vote rejected", 403)

        def mutate(session: Session) -> None:
            _raise_rejection_reason(session, user_id, track)
            resolved_track = _resolve_vote_track(session, user_id, track)
            cast_vote_value(
                session.current_task,  # type: ignore[arg-type]
                session.estimation_mode,
                user_id,
                resolved_track,
                vote_value,
            )

        session, _ = await repo.mutate_session(chat_id, topic_id, mutate)
        return session


async def _load_session(
    repo: SessionRepository,
    chat_id: int,
    topic_id: Optional[int],
) -> Session:
    if hasattr(repo, "get_session_async"):
        return await repo.get_session_async(chat_id, topic_id)  # type: ignore[attr-defined]
    return await repo.get_session(chat_id, topic_id)


def _resolve_vote_track(session: Session, user_id: int, track: Optional[str]) -> Optional[str]:
    mode = normalise_estimation_mode(session.estimation_mode)
    if not is_split_mode(mode):
        return None
    participant = session.participants.get(user_id)
    team_role = participant.team_role if participant else None
    expected = resolve_track(mode, team_role)
    if not expected:
        raise WebVoteError("Participant role is not mapped to an estimation track", status_code=400)
    if track and track != expected:
        raise WebVoteError("Vote track does not match participant role", status_code=400)
    return expected


def _raise_rejection_reason(session: Session, user_id: int, track: Optional[str] = None) -> None:
    """Translate session state into a structured ``WebVoteError``.

    Returns silently when the vote would be accepted. Otherwise raises
    ``WebVoteError`` with the precise HTTP status the public API should
    surface — preserves the existing 400 vs 403 distinction.
    """
    if not session.current_task:
        raise WebVoteError("No active task", status_code=400)
    if not session.can_vote(user_id):
        raise WebVoteError("Not authorized to vote", status_code=403)
    if is_split_mode(session.estimation_mode):
        _resolve_vote_track(session, user_id, track)
