"""CMS session token helpers — cookie policy and Redis payload validation."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

_DEV_ENV_NAMES = frozenset({"dev", "development", "local", "test"})


def resolve_cms_cookie_secure() -> bool:
    """Resolve whether the CMS auth cookie should carry the Secure flag.

    Explicit ``CMS_COOKIE_SECURE`` always wins. When unset, Secure defaults to
    ``True`` outside known dev-like environments so production cannot silently
    run with cleartext cookies.
    """
    raw = os.getenv("CMS_COOKIE_SECURE")
    if raw is not None:
        return raw.lower() == "true"
    app_env = os.getenv("APP_ENV", os.getenv("DEPLOY_ENVIRONMENT", "")).strip().lower()
    if app_env in _DEV_ENV_NAMES:
        return False
    return True


def build_cms_token_payload(
    *,
    admin_id: int,
    username: str,
    ip: str,
    token_version: int,
    ttl_seconds: int,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Build the Redis payload for a freshly minted CMS session token."""
    issued_at = now if now is not None else time.time()
    return {
        "admin_id": int(admin_id),
        "username": username,
        "ip": ip,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
        "token_version": int(token_version),
    }


def cms_token_is_expired(data: dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Return True when the token payload is missing or past its absolute expiry."""
    if not data:
        return True
    expires_at = data.get("expires_at")
    if expires_at is None:
        return True
    try:
        deadline = float(expires_at)
    except (TypeError, ValueError):
        return True
    current = now if now is not None else time.time()
    return current >= deadline


def cms_token_version_matches(data: dict[str, Any], principal_record: dict[str, Any]) -> bool:
    """Return True when the token was issued for the admin's current token version."""
    stored_version = data.get("token_version")
    if stored_version is None:
        return False
    try:
        return int(stored_version) == int(principal_record.get("token_version", 1))
    except (TypeError, ValueError):
        return False
