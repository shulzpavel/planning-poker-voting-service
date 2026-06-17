"""Data models (backward compatibility - re-export from domain)."""

# Re-export from domain for backward compatibility
from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task

__all__ = ["Participant", "Session", "Task"]
