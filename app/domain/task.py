"""Task model for Planning Poker."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_task_id(data: Dict[str, Any], legacy_context: Optional[str] = None) -> str:
    raw = "|".join(
        str(data.get(key) or "")
        for key in ("jira_key", "summary", "url", "story_points", "jql", "completed_at")
    )
    if legacy_context:
        raw = f"{legacy_context}|{raw}"
    return "legacy-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class Task:
    """Represents a task for voting."""

    task_id: str = field(default_factory=lambda: uuid4().hex)
    jira_key: Optional[str] = None
    summary: str = ""
    url: Optional[str] = None
    story_points: Optional[int] = None
    votes: Dict[int, str] = field(default_factory=dict)
    # Split estimation modes: track_key -> {user_id -> vote_value}
    track_votes: Dict[str, Dict[int, str]] = field(default_factory=dict)
    # Final estimate per track for split modes (front/back/qa, dev/test).
    story_points_by_track: Dict[str, int] = field(default_factory=dict)
    completed_at: Optional[str] = None
    jql: Optional[str] = None
    source: str = "manual"
    ai_summary: Optional[Dict[str, Any]] = None
    # Jira issue body captured at import time. Used by the voter UI to
    # show the original spec inline and by the AI summary prompt as a
    # cheap fallback when the per-request jira-service context fetch
    # fails or is skipped. Optional — manual tasks have no description.
    description: Optional[str] = None
    # Raw Atlassian Document Format payload for the same description.
    # Stored separately from ``description`` (plain text) so the voter UI
    # can render the original Jira formatting (lists, headings, code,
    # links, bold/italic) while the AI prompt still uses the plain text
    # version. ``None`` when the field is empty, when the source is a
    # plain string instead of ADF, or for manual tasks.
    description_adf: Optional[Any] = None
    # Sanitized HTML from Jira ``renderedFields.description``. Preferred
    # by the voter UI when present — matches Jira's own rendering.
    description_html: Optional[str] = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert task to dictionary."""
        return {
            "task_id": self.task_id,
            "jira_key": self.jira_key,
            "summary": self.summary,
            "url": self.url,
            "story_points": self.story_points,
            "votes": {str(k): v for k, v in self.votes.items()},
            "track_votes": {
                track: {str(uid): value for uid, value in votes.items()}
                for track, votes in self.track_votes.items()
            },
            "story_points_by_track": dict(self.story_points_by_track),
            "completed_at": self.completed_at,
            "jql": self.jql,
            "source": self.source,
            "ai_summary": self.ai_summary,
            "description": self.description,
            "description_adf": self.description_adf,
            "description_html": self.description_html,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], legacy_context: Optional[str] = None) -> "Task":
        """Create task from dictionary."""
        votes = {}
        if "votes" in data and isinstance(data["votes"], dict):
            try:
                votes = {int(k): v for k, v in data["votes"].items()}
            except (ValueError, TypeError):
                votes = {}
        track_votes: Dict[str, Dict[int, str]] = {}
        raw_track_votes = data.get("track_votes")
        if isinstance(raw_track_votes, dict):
            for track_key, track_data in raw_track_votes.items():
                if not isinstance(track_data, dict):
                    continue
                try:
                    track_votes[str(track_key)] = {int(uid): value for uid, value in track_data.items()}
                except (ValueError, TypeError):
                    continue
        story_points_by_track: Dict[str, int] = {}
        raw_sp_by_track = data.get("story_points_by_track")
        if isinstance(raw_sp_by_track, dict):
            for track_key, value in raw_sp_by_track.items():
                try:
                    story_points_by_track[str(track_key)] = int(value)
                except (TypeError, ValueError):
                    continue
        task_id = str(data.get("task_id") or data.get("id") or _legacy_task_id(data, legacy_context))
        source = data.get("source")
        if source not in {"jira", "manual"}:
            source = "jira" if data.get("jira_key") else "manual"
        description_raw = data.get("description")
        description = (
            str(description_raw).strip()
            if isinstance(description_raw, str) and description_raw.strip()
            else None
        )
        # Only accept ADF in the structured shape Jira itself uses
        # (``{"type": "doc", ...}``). Everything else (strings, empty
        # dicts, lists, None) is collapsed to ``None`` so the frontend
        # has a single boolean to switch on.
        description_adf_raw = data.get("description_adf")
        description_adf = (
            description_adf_raw
            if isinstance(description_adf_raw, dict) and description_adf_raw.get("type")
            else None
        )
        description_html_raw = data.get("description_html")
        description_html = (
            str(description_html_raw).strip()
            if isinstance(description_html_raw, str) and description_html_raw.strip()
            else None
        )
        return cls(
            task_id=task_id,
            jira_key=data.get("jira_key"),
            summary=data.get("summary", ""),
            url=data.get("url"),
            story_points=data.get("story_points"),
            votes=votes,
            track_votes=track_votes,
            story_points_by_track=story_points_by_track,
            completed_at=data.get("completed_at"),
            jql=data.get("jql"),
            source=source,
            ai_summary=data.get("ai_summary") if isinstance(data.get("ai_summary"), dict) else None,
            description=description,
            description_adf=description_adf,
            description_html=description_html,
            created_at=data.get("created_at") or _utc_now(),
            updated_at=data.get("updated_at") or _utc_now(),
        )

    def touch(self) -> None:
        """Update task modification timestamp."""
        self.updated_at = _utc_now()

    @property
    def text(self) -> str:
        """Get task text representation."""
        parts = [self.summary]
        if self.url:
            parts.append(self.url)
        return " ".join(parts).strip()
