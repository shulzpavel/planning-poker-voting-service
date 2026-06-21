"""Validation and stable IDs for web session participants."""

from __future__ import annotations

import hashlib
import os
import re

PARTICIPANT_EMAIL_DOMAIN = os.getenv("WEB_PARTICIPANT_EMAIL_DOMAIN", "example.com").strip().lower()
_MAX_EMAIL_LEN = 64

_DOMAIN_RE = re.escape(PARTICIPANT_EMAIL_DOMAIN)
_EMAIL_RE = re.compile(rf"^[a-z0-9][a-z0-9._-]*@{_DOMAIN_RE}$")
ALLOWED_PARTICIPANT_ROLES = frozenset({"backend", "frontend", "qa"})


def normalize_participant_email(raw: str) -> str:
    return raw.strip().lower()


def validate_participant_email(raw: str) -> str:
    """Return normalized corporate email or raise ValueError with a user-facing message."""
    normalized = normalize_participant_email(raw)
    if not normalized:
        raise ValueError("Введите корпоративную почту")
    if len(normalized) > _MAX_EMAIL_LEN:
        raise ValueError(f"Почта не должна превышать {_MAX_EMAIL_LEN} символов")
    if not _EMAIL_RE.match(normalized):
        raise ValueError(
            f"Укажите почту в формате name@{PARTICIPANT_EMAIL_DOMAIN} "
            "(латиница, цифры, точка, дефис, подчёркивание)"
        )
    return normalized


def validate_participant_role(raw: str) -> str:
    role = raw.strip().lower()
    if role not in ALLOWED_PARTICIPANT_ROLES:
        raise ValueError("Выберите роль в команде")
    return role


def stable_user_id_from_email(email: str) -> int:
    """Map a normalized email to a stable negative user_id (CMS / votes)."""
    digest = hashlib.sha256(f"web-email:{email}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    return -(value + 1)
