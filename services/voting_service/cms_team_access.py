"""Team-scoped access helpers for CMS resources.

Security invariants:
* UI filters are not protection — every list/detail/mutation must use these helpers.
* ``team_id = NULL`` on a record means legacy-shared visibility for all admins.
* Superadmin bypass is only via ``actor.is_superuser``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from services.voting_service._http_shared import CmsPrincipal


def team_scope(actor: CmsPrincipal) -> dict[str, Any]:
    """Parameters for SQL team filters on list/overview queries."""
    return {
        "is_superuser": actor.is_superuser,
        "actor_team_ids": list(actor.team_ids),
    }


def can_access_team(actor: CmsPrincipal, team_id: Optional[int]) -> bool:
    if actor.is_superuser:
        return True
    if team_id is None:
        return True
    return int(team_id) in actor.team_ids


def require_team_access(actor: CmsPrincipal, team_id: Optional[int]) -> None:
    if not can_access_team(actor, team_id):
        raise HTTPException(status_code=403, detail="Forbidden")


def assert_record_access(actor: CmsPrincipal, record: dict[str, Any]) -> None:
    """Check team access for a loaded parent record (session/plan/retro)."""
    raw_team_id = record.get("team_id")
    team_id = int(raw_team_id) if raw_team_id is not None else None
    if not can_access_team(actor, team_id):
        raise HTTPException(status_code=404, detail="Not found")


def assert_user_sessions_access(actor: CmsPrincipal, team_ids: list[Optional[int]]) -> None:
    """Check team access for a participant linked to one or more sessions."""
    for team_id in team_ids:
        if not can_access_team(actor, team_id):
            raise HTTPException(status_code=404, detail="Not found")


def resolve_create_team_id(actor: CmsPrincipal, team_id: Optional[int]) -> Optional[int]:
    """Resolve team_id for resource creation.

    * Superadmin may omit team_id (legacy NULL) or pass any team.
    * Single-team admin auto-assigns their team when omitted.
    * Multi-team admin must pass an accessible team_id.
    * Admin with no teams may only create legacy NULL records.
    """
    if actor.is_superuser:
        if team_id is None:
            return None
        return int(team_id)

    teams = actor.team_ids
    if team_id is None:
        if len(teams) == 1:
            return int(next(iter(teams)))
        if len(teams) == 0:
            return None
        raise HTTPException(status_code=400, detail="team_id is required")

    resolved = int(team_id)
    if resolved not in teams:
        raise HTTPException(status_code=403, detail="Forbidden")
    return resolved


def require_superuser(actor: CmsPrincipal) -> None:
    if not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
