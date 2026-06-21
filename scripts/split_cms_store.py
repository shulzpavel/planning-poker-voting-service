#!/usr/bin/env python3
"""One-off splitter: cms_store.py -> services/voting_service/cms_store/ package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONOLITH = ROOT / "services/voting_service/cms_store.py"
PKG = ROOT / "services/voting_service/cms_store"


def load_monolith_lines() -> list[str]:
    if MONOLITH.is_file() and MONOLITH.read_text().count("\n") > 500:
        return MONOLITH.read_text().splitlines(keepends=True)
    raw = subprocess.check_output(
        ["git", "show", "main:services/voting_service/cms_store.py"],
        cwd=ROOT,
        text=True,
    )
    return raw.splitlines(keepends=True)


lines = load_monolith_lines()


def slice_lines(start: int, end: int) -> str:
    """Extract 1-indexed inclusive start through exclusive end."""
    return "".join(lines[start - 1 : end - 1])


def unindent_class_body(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        out.append(line[4:] if line.startswith("    ") else line)
    return "".join(out)


def write(path: Path, content: str) -> None:
    path.write_text(content)
    print(f"  wrote {path.relative_to(ROOT)}")


HELPERS = slice_lines(1, 357)
if "TYPE_CHECKING" not in HELPERS:
    HELPERS = HELPERS.replace(
        "from typing import Any, Optional\n",
        "from typing import TYPE_CHECKING, Any, Optional\n\n"
        "if TYPE_CHECKING:\n"
        "    from services.voting_service.cms_store import PostgresCmsStore\n",
    )

SQL_CONSTANTS_BODY = unindent_class_body(
    slice_lines(1912, 1918)
    + slice_lines(2019, 2062)
    + slice_lines(2469, 2479)
    + slice_lines(2616, 2637)
)

SCHEMA_BODY = unindent_class_body(slice_lines(361, 933) + slice_lines(3511, 3513))
SESSIONS_BODY = unindent_class_body(slice_lines(933, 1251))
RBAC_BODY = unindent_class_body(slice_lines(1251, 1493) + slice_lines(1573, 1912))
TEAMS_BODY = unindent_class_body(slice_lines(1493, 1573))
SPRINT_PLANS_BODY = unindent_class_body(slice_lines(1919, 2019))
SCOPE_BOARDS_BODY = unindent_class_body(slice_lines(2063, 2469))
RETROS_BODY = unindent_class_body(slice_lines(2480, 2616))
SESSIONS_LIST_BODY = unindent_class_body(slice_lines(2638, 3174))
LISTS_BODY = unindent_class_body(slice_lines(3174, 3511))

PKG.mkdir(exist_ok=True)

write(PKG / "_helpers.py", HELPERS)

write(
    PKG / "_sql_constants.py",
    '''"""Shared SQL fragments for PostgresCmsStore mixins."""

from __future__ import annotations


class SqlConstantsMixin:
    """SQL SELECT fragments used across CMS store mixins."""

'''
    + SQL_CONSTANTS_BODY,
)

write(
    PKG / "schema.py",
    '''"""CMS store mixin: schema bootstrap and migrations."""

from __future__ import annotations

import json
import os

import asyncpg

from services.voting_service.cms_rbac import (
    ALL_PERMISSION_KEYS,
    CMS_PAGE_DEFINITIONS,
    CMS_PERMISSION_DEFINITIONS,
    DEPRECATED_CMS_PAGE_KEYS,
    OPERATIONAL_VIEW_PERMISSIONS,
    PERM_ACCESS_MANAGE,
    PERM_ACCESS_VIEW,
    PERM_APP_SESSIONS_MANAGE,
    PERM_PLANNER_VIEW,
    PERM_SESSIONS_VIEW,
    PERM_TASKS_MANAGE,
    hash_password,
)


class SchemaMixin:
    """Pool lifecycle, DDL, and access-control seed data."""

'''
    + SCHEMA_BODY,
)

write(
    PKG / "sessions.py",
    '''"""CMS store mixin: live session sync and web token writes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.domain.session import Session

from services.voting_service.cms_store._helpers import (
    _deserialize_session,
    _row_to_dict,
    _serialize_session,
    session_key,
    token_hash,
    token_prefix,
)

logger = logging.getLogger(__name__)


class SessionsMixin:
    """Sync Redis session state into the CMS read model."""

'''
    + SESSIONS_BODY,
)

write(
    PKG / "rbac.py",
    '''"""CMS store mixin: admin auth, roles, and permissions."""

from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_rbac import hash_password, verify_password
from services.voting_service.cms_store._helpers import _row_to_dict, _team_row


class RBACMixin:
    """Admin accounts, RBAC roles, and permission helpers."""

'''
    + RBAC_BODY,
)

write(
    PKG / "teams.py",
    '''"""CMS store mixin: CMS team CRUD."""

from __future__ import annotations

from typing import Any, Optional

from services.voting_service.cms_store._helpers import _team_row, normalize_team_slug


class TeamsMixin:
    """Team listing and mutation."""

'''
    + TEAMS_BODY,
)

write(
    PKG / "sprint_plans.py",
    '''"""CMS store mixin: sprint planner persistence."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _sprint_plan_row


class SprintPlansMixin:
    """Sprint plan CRUD."""

'''
    + SPRINT_PLANS_BODY,
)

write(
    PKG / "scope_boards.py",
    '''"""CMS store mixin: monthly scope boards."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _scope_board_row


class ScopeBoardsMixin:
    """Scope board CRUD and snapshot updates."""

'''
    + SCOPE_BOARDS_BODY,
)

write(
    PKG / "retros.py",
    '''"""CMS store mixin: retrospective boards."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _retro_row


class RetrosMixin:
    """Retrospective CRUD and status transitions."""

'''
    + RETROS_BODY,
)

write(
    PKG / "sessions_list.py",
    '''"""CMS store mixin: CMS session overview and paginated reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_store._helpers import (
    _attach_team_fields,
    _decode_cursor_timestamp,
    _row_to_dict,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class SessionsListMixin:
    """Overview metrics and paginated CMS session queries."""

'''
    + SESSIONS_LIST_BODY,
)

write(
    PKG / "lists.py",
    '''"""CMS store mixin: paginated CMS list endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_store._helpers import (
    _decode_cursor_timestamp,
    _row_to_dict,
    _user_row_dict,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class ListsMixin:
    """Users, votes, audit events, and shared pagination helpers."""

'''
    + LISTS_BODY,
)

write(
    PKG / "__init__.py",
    '''"""Postgres read model for CMS/admin screens."""

from __future__ import annotations

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
from services.voting_service.cms_store.sessions_list import SessionsListMixin
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
    SessionsListMixin,
    ListsMixin,
):
    """Normalized read model used by the CMS API."""
''',
)

if MONOLITH.is_file():
    MONOLITH.unlink()
    print(f"  deleted {MONOLITH.relative_to(ROOT)}")

print("split complete")
