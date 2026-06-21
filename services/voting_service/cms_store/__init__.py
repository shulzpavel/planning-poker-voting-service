"""Postgres read model for CMS/admin screens."""

from __future__ import annotations

import asyncpg

from services.voting_service.cms_store._helpers import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    backfill_cms_from_redis,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    normalize_team_slug,
    session_key,
    token_hash,
    token_prefix,
)
from services.voting_service.cms_store._sql_constants import SqlConstantsMixin
from services.voting_service.cms_store.lists import ListsMixin
from services.voting_service.cms_store.rbac import RBACMixin
from services.voting_service.cms_store.retros import RetrosMixin
from services.voting_service.cms_store.schema import SchemaMixin
from services.voting_service.cms_store.scope_boards import ScopeBoardsMixin
from services.voting_service.cms_store.sessions import SessionsMixin
from services.voting_service.cms_store.sprint_plans import SprintPlansMixin
from services.voting_service.cms_store.teams import TeamsMixin

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PostgresCmsStore",
    "backfill_cms_from_redis",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "normalize_team_slug",
    "session_key",
    "token_hash",
    "token_prefix",
]


class PostgresCmsStore(
    SqlConstantsMixin,
    SchemaMixin,
    RBACMixin,
    TeamsMixin,
    SessionsMixin,
    SprintPlansMixin,
    ScopeBoardsMixin,
    RetrosMixin,
    ListsMixin,
):
    """Normalized read model used by the CMS API."""

    @property
    def pool(self) -> asyncpg.Pool:
        """Backward-compatible alias for tests and callers."""
        return self._pool
