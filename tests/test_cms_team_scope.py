"""Team-scoped CMS access helpers and principal shape."""

from services.voting_service._http_shared import CmsPrincipal, _principal_from_record
from services.voting_service.cms_team_access import (
    assert_record_access,
    can_access_team,
    resolve_create_team_id,
    team_scope,
)
from services.voting_service.cms_store import normalize_team_slug
import pytest
from fastapi import HTTPException


def _actor(*, superuser: bool = False, team_ids: tuple[int, ...] = ()) -> CmsPrincipal:
    return CmsPrincipal(
        id=1,
        username="alice",
        display_name=None,
        is_superuser=superuser,
        permissions=frozenset(),
        roles=(),
        pages=(),
        team_ids=frozenset(team_ids),
        teams=(),
    )


def test_principal_from_record_includes_teams():
    principal = _principal_from_record(
        {
            "id": 7,
            "username": "lead",
            "is_superuser": False,
            "permissions": [],
            "roles": [],
            "pages": [],
            "team_ids": [2, 5],
            "teams": [{"id": 2, "slug": "alpha", "name": "Alpha"}],
        }
    )
    assert principal.team_ids == frozenset({2, 5})
    assert len(principal.teams) == 1


def test_legacy_null_team_visible_to_all_admins():
    actor = _actor(team_ids=(10,))
    assert can_access_team(actor, None) is True
    assert_record_access(actor, {"team_id": None}) is None


def test_team_admin_cannot_access_foreign_team():
    actor = _actor(team_ids=(1,))
    assert can_access_team(actor, 2) is False
    with pytest.raises(HTTPException) as exc:
        assert_record_access(actor, {"team_id": 2})
    assert exc.value.status_code == 404


def test_superuser_bypasses_team_checks():
    actor = _actor(superuser=True, team_ids=())
    assert can_access_team(actor, 99) is True
    assert_record_access(actor, {"team_id": 99}) is None


def test_resolve_create_team_id_auto_assigns_single_team():
    actor = _actor(team_ids=(3,))
    assert resolve_create_team_id(actor, None) == 3


def test_resolve_create_team_id_requires_choice_for_multi_team():
    actor = _actor(team_ids=(1, 2))
    with pytest.raises(HTTPException) as exc:
        resolve_create_team_id(actor, None)
    assert exc.value.status_code == 400


def test_team_scope_payload():
    actor = _actor(team_ids=(4, 8))
    scope = team_scope(actor)
    assert scope["is_superuser"] is False
    assert set(scope["actor_team_ids"]) == {4, 8}


def test_team_slug_allows_display_name_format():
    assert normalize_team_slug("iGaming RIP") == "igaming-rip"
