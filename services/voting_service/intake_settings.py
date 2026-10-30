"""Deployment settings for the private product-intake dashboard."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from services.voting_service.cms_rbac import PERM_PLANNER_VIEW

@dataclass(frozen=True)
class IntakeSettings:
    enabled: bool
    owner: str
    jql: str
    source_field: str
    title: str
    stage_name: str
    timezone: str
    statuses: frozenset[str]
    information_statuses: frozenset[str]
    deferred_statuses: frozenset[str]
    rejected_statuses: frozenset[str]
    refresh_seconds: int
    model: str

    @classmethod
    def from_env(cls) -> "IntakeSettings":
        def names(key: str, default: list[str]) -> frozenset[str]:
            value = json.loads(os.getenv(key) or json.dumps(default))
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError("Invalid intake statuses")
            return frozenset(value)
        timezone = os.getenv("INTAKE_DASHBOARD_TIMEZONE", "Europe/Belgrade")
        ZoneInfo(timezone)
        return cls(os.getenv("INTAKE_DASHBOARD_ENABLED", "false").lower() == "true",
                   (os.getenv("INTAKE_DASHBOARD_OWNER") or os.getenv("CMS_USERNAME", "")).strip(),
                   os.getenv("INTAKE_DASHBOARD_JQL", "").strip(), os.getenv("INTAKE_DASHBOARD_SOURCE_FIELD", "").strip(),
                   os.getenv("INTAKE_DASHBOARD_TITLE", "Продуктовые инициативы"), os.getenv("INTAKE_DASHBOARD_STAGE", "Разбор инициатив"), timezone,
                   names("INTAKE_DASHBOARD_STATUSES", ["Backlog", "Новый запрос", "Нужна информация"]),
                   names("INTAKE_DASHBOARD_INFORMATION_STATUSES", ["Нужна информация"]), names("INTAKE_DASHBOARD_DEFERRED_STATUSES", ["Отложено"]), names("INTAKE_DASHBOARD_REJECTED_STATUSES", ["Отклонено"]),
                   max(60, min(3600, int(os.getenv("INTAKE_DASHBOARD_REFRESH_SECONDS", "300")))),
                   os.getenv("INTAKE_AI_MODEL", "claude-sonnet-4-6").strip())

    def permits(self, actor) -> bool:
        return bool(self.enabled and self.owner and self.jql and actor.username == self.owner and actor.can(PERM_PLANNER_VIEW))

    @property
    def cache_id(self) -> str:
        data = {**self.__dict__, "statuses": sorted(self.statuses), "information_statuses": sorted(self.information_statuses), "deferred_statuses": sorted(self.deferred_statuses), "rejected_statuses": sorted(self.rejected_statuses)}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]

def dashboard_access(actor) -> bool:
    try:
        return IntakeSettings.from_env().permits(actor)
    except (ValueError, KeyError):
        return False
