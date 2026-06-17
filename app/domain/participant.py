"""Participant model for Planning Poker."""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import UserRole


@dataclass
class Participant:
    """Represents a participant in a session."""

    user_id: int
    name: str
    role: UserRole
    # Web voter discipline: backend | frontend | qa. Optional for legacy
    # Telegram-era participants.
    team_role: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert participant to dictionary."""
        payload: Dict[str, Any] = {
            "name": self.name,
            "role": self.role.value,
        }
        if self.team_role:
            payload["team_role"] = self.team_role
        return payload

    @classmethod
    def from_dict(cls, user_id: int, data: Dict[str, Any]) -> "Participant":
        """Create participant from dictionary."""
        team_role = data.get("team_role")
        if team_role is not None:
            team_role = str(team_role).strip().lower() or None
        return cls(
            user_id=user_id,
            name=data.get("name", "Unknown"),
            role=UserRole(data.get("role", UserRole.PARTICIPANT.value)),
            team_role=team_role,
        )

