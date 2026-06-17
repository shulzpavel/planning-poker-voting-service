"""CMS role-based access control primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any


PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 390_000

PERM_OVERVIEW_VIEW = "cms.overview.view"
PERM_SESSIONS_VIEW = "cms.sessions.view"
PERM_USERS_VIEW = "cms.users.view"
PERM_VOTES_VIEW = "cms.votes.view"
PERM_TOKENS_VIEW = "cms.tokens.view"
PERM_WEB_VIEW = "cms.web.view"
PERM_EVENTS_VIEW = "cms.events.view"
PERM_ACCESS_VIEW = "cms.access.view"
PERM_ACCESS_MANAGE = "cms.access.manage"
PERM_TASKS_MANAGE = "cms.tasks.manage"
PERM_APP_SESSIONS_MANAGE = "app.sessions.manage"
PERM_WEB_PARTICIPANTS_DELETE = "cms.web_participants.delete"
PERM_PLANNER_VIEW = "cms.planner.view"
PERM_RETRO_VIEW = "cms.retro.view"
PERM_RETRO_MANAGE = "cms.retro.manage"
PERM_RETRO_ANALYZE = "cms.retro.analyze"

CMS_PERMISSION_DEFINITIONS: list[dict[str, str]] = [
    {
        "key": PERM_OVERVIEW_VIEW,
        "label": "View overview",
        "description": "Can view aggregate CMS statistics.",
    },
    {
        "key": PERM_SESSIONS_VIEW,
        "label": "View sessions",
        "description": "Can view sessions, participants, tasks, and raw session JSON.",
    },
    {
        "key": PERM_USERS_VIEW,
        "label": "View users",
        "description": "Can view user records.",
    },
    {
        "key": PERM_VOTES_VIEW,
        "label": "View votes",
        "description": "Can view vote records.",
    },
    {
        "key": PERM_TOKENS_VIEW,
        "label": "View tokens",
        "description": "Can view web voting tokens.",
    },
    {
        "key": PERM_WEB_VIEW,
        "label": "View web participants",
        "description": "Can view web participant records.",
    },
    {
        "key": PERM_WEB_PARTICIPANTS_DELETE,
        "label": "Delete participants permanently",
        "description": "Can hard-delete participant records, session participant links, web join records, and CMS vote rows.",
    },
    {
        "key": PERM_EVENTS_VIEW,
        "label": "View audit events",
        "description": "Can view CMS audit events.",
    },
    {
        "key": PERM_ACCESS_VIEW,
        "label": "View access settings",
        "description": "Can view CMS admins, roles, pages, and permissions.",
    },
    {
        "key": PERM_ACCESS_MANAGE,
        "label": "Manage access",
        "description": "Can create and update CMS admins and roles.",
    },
    {
        "key": PERM_TASKS_MANAGE,
        "label": "Manage session tasks",
        "description": "Can create, edit, delete, and reorder active session tasks.",
    },
    {
        "key": PERM_APP_SESSIONS_MANAGE,
        "label": "Manage planning sessions",
        "description": "Can create and facilitate planning sessions in the main web app.",
    },
    {
        "key": PERM_PLANNER_VIEW,
        "label": "View sprint planner",
        "description": "Can view and edit the sprint capacity/velocity planner.",
    },
    {
        "key": PERM_RETRO_VIEW,
        "label": "View retrospectives",
        "description": "Can view team retrospectives.",
    },
    {
        "key": PERM_RETRO_MANAGE,
        "label": "Manage retrospectives",
        "description": "Can create, configure, facilitate, and delete team retrospectives.",
    },
    {
        "key": PERM_RETRO_ANALYZE,
        "label": "Analyze retrospectives",
        "description": "Can generate paid AI analysis for completed retrospectives.",
    },
]

# CMS pages that were inherited from the Telegram-era console. They are kept
# in the database for audit/backfill compatibility, but disabled at boot via
# ``DEPRECATED_CMS_PAGE_KEYS`` so they never appear in the navigation.
DEPRECATED_CMS_PAGE_KEYS = ("votes", "web")

CMS_PAGE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "overview",
        "label": "Overview",
        "path": "/cms",
        "permission_key": PERM_OVERVIEW_VIEW,
        "sort_order": 10,
    },
    {
        "key": "sessions",
        "label": "Sessions",
        "path": "/cms/sessions",
        "permission_key": PERM_SESSIONS_VIEW,
        "sort_order": 30,
    },
    {
        "key": "users",
        "label": "Users",
        "path": "/cms/users",
        "permission_key": PERM_USERS_VIEW,
        "sort_order": 50,
    },
    {
        "key": "votes",
        "label": "Votes",
        "path": "/cms/votes",
        "permission_key": PERM_VOTES_VIEW,
        "sort_order": 40,
    },
    {
        "key": "tokens",
        "label": "Tokens",
        "path": "/cms/tokens",
        "permission_key": PERM_TOKENS_VIEW,
        "sort_order": 60,
    },
    {
        "key": "web",
        "label": "Web",
        "path": "/cms/web",
        "permission_key": PERM_WEB_VIEW,
        "sort_order": 70,
    },
    {
        "key": "events",
        "label": "Events",
        "path": "/cms/events",
        "permission_key": PERM_EVENTS_VIEW,
        "sort_order": 80,
    },
    {
        "key": "access",
        "label": "Access",
        "path": "/cms/access",
        "permission_key": PERM_ACCESS_VIEW,
        "sort_order": 90,
    },
    {
        "key": "planner",
        "label": "Sprint planner",
        "path": "/cms/planner",
        "permission_key": PERM_PLANNER_VIEW,
        "sort_order": 20,
    },
    {
        "key": "scope",
        "label": "Отчеты",
        "path": "/cms/scope",
        "permission_key": PERM_PLANNER_VIEW,
        "sort_order": 35,
    },
    {
        "key": "retro",
        "label": "Retrospectives",
        "path": "/cms/retro",
        "permission_key": PERM_RETRO_VIEW,
        "sort_order": 40,
    },
]

OPERATIONAL_VIEW_PERMISSIONS = [
    PERM_OVERVIEW_VIEW,
    PERM_SESSIONS_VIEW,
    PERM_USERS_VIEW,
    PERM_VOTES_VIEW,
    PERM_TOKENS_VIEW,
    PERM_WEB_VIEW,
    PERM_EVENTS_VIEW,
    PERM_PLANNER_VIEW,
    PERM_RETRO_VIEW,
]

ALL_PERMISSION_KEYS = [item["key"] for item in CMS_PERMISSION_DEFINITIONS]


def hash_password(password: str, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{PASSWORD_HASH_SCHEME}${iterations}${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_raw, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_HASH_SCHEME:
            return False
        iterations = int(iterations_raw)
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))
