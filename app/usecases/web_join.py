"""Use case for joining a planning session from the public web UI.

The existing :class:`JoinSessionUseCase` (``app/usecases/join_session.py``)
is the Telegram-era join: it unconditionally replaces any existing
participant record with a fresh one and drops admin votes. The web join
flow has different semantics that the HTTP handler used to encode inline:

* keep an existing participant alive (preserve current votes / role) and
  only refresh their display name — a page reload must not look like the
  participant left and rejoined;
* return whether a *new* participant was inserted, so the caller can
  decide to broadcast a session-state update only for genuine joins.

Extracted from ``web_api.web_join`` so the HTTP handler is a thin
adapter and the business rule lives in the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.domain.participant import Participant
from app.domain.session import Session
from app.ports.session_repository import SessionRepository
from config import UserRole


@dataclass(frozen=True)
class WebJoinResult:
    """Outcome of a web join attempt."""

    session: Session
    added: bool  # True iff the participant was newly inserted into the roster


class JoinWebSessionUseCase:
    """Idempotent join of a web participant into a planning session.

    The session role is always :data:`UserRole.PARTICIPANT` — the web UI
    has no concept of facilitators (those operate via the manager app).
    The participant's *team* role (backend/frontend/qa/manager) is stored
    separately by the HTTP layer in Redis / CMS read model.
    """

    def __init__(self, session_repo: SessionRepository):
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        user_id: int,
        display_name: str,
        team_role: Optional[str] = None,
    ) -> WebJoinResult:
        added = False

        def mutate(session: Session) -> None:
            nonlocal added
            existing = session.participants.get(user_id)
            if existing:
                existing.name = display_name
                if team_role:
                    existing.team_role = team_role
                return
            session.participants[user_id] = Participant(
                user_id=user_id,
                name=display_name,
                role=UserRole.PARTICIPANT,
                team_role=team_role,
            )
            added = True

        session, _ = await self.session_repo.mutate_session(chat_id, topic_id, mutate)
        return WebJoinResult(session=session, added=added)
