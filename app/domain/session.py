"""Session model for Planning Poker."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.domain.estimation import DEFAULT_ESTIMATION_MODE, normalise_estimation_mode
from app.domain.participant import Participant
from app.domain.task import Task
from config import UserRole


@dataclass
class Session:
    """Represents a planning poker session."""

    chat_id: int
    topic_id: Optional[int]
    participants: Dict[int, Participant] = field(default_factory=dict)
    tasks_queue: List[Task] = field(default_factory=list)
    current_task_index: int = 0
    history: List[Task] = field(default_factory=list)
    last_batch: List[Task] = field(default_factory=list)
    batch_completed: bool = False
    active_vote_message_id: Optional[int] = None
    current_batch_id: Optional[str] = None
    current_batch_started_at: Optional[str] = None
    # Snapshot of when the most recent batch was started — preserved when the
    # cursor moves past the last task or finish is called so the summary
    # screen can still show the session duration.
    last_batch_started_at: Optional[str] = None
    revealed_task_id: Optional[str] = None
    tasks_version: int = 0
    estimation_mode: str = DEFAULT_ESTIMATION_MODE

    @property
    def current_task(self) -> Optional[Task]:
        """Get current task."""
        if 0 <= self.current_task_index < len(self.tasks_queue):
            return self.tasks_queue[self.current_task_index]
        return None

    @property
    def is_voting_active(self) -> bool:
        """Check if voting for current batch is in progress."""
        return bool(self.current_task) and bool(self.current_batch_started_at) and not self.batch_completed

    @property
    def current_votes(self) -> Dict[int, str]:
        """Get votes for current task."""
        task = self.current_task
        return task.votes if task else {}

    def get_participant_role(self, user_id: int) -> Optional[UserRole]:
        """Get participant role."""
        participant = self.participants.get(user_id)
        return participant.role if participant else None

    def can_vote(self, user_id: int) -> bool:
        """Check if user can vote."""
        role = self.get_participant_role(user_id)
        return role in {UserRole.PARTICIPANT, UserRole.LEAD}

    def can_manage(self, user_id: int) -> bool:
        """Check if user can manage session."""
        role = self.get_participant_role(user_id)
        return role in {UserRole.ADMIN, UserRole.LEAD}

    def bump_tasks_version(self) -> None:
        """Mark queue/task metadata as changed."""
        self.tasks_version += 1

    @property
    def current_task_id(self) -> Optional[str]:
        """Return stable id of the current task."""
        return self.current_task.task_id if self.current_task else None

    def normalize_current_task_index(self) -> None:
        """Keep current task index inside the queue bounds."""
        if not self.tasks_queue:
            self.current_task_index = 0
            return
        self.current_task_index = max(0, min(self.current_task_index, len(self.tasks_queue) - 1))


class SessionFactory:
    """Serialize and deserialize session domain objects."""

    @staticmethod
    def to_dict(session: Session) -> dict:
        return {
            "chat_id": session.chat_id,
            "topic_id": session.topic_id,
            "participants": {str(uid): participant.to_dict() for uid, participant in session.participants.items()},
            "tasks_queue": [task.to_dict() for task in session.tasks_queue],
            "current_task_index": session.current_task_index,
            "history": [task.to_dict() for task in session.history],
            "last_batch": [task.to_dict() for task in session.last_batch],
            "batch_completed": session.batch_completed,
            "active_vote_message_id": session.active_vote_message_id,
            "current_batch_id": session.current_batch_id,
            "current_batch_started_at": session.current_batch_started_at,
            "last_batch_started_at": session.last_batch_started_at,
            "revealed_task_id": session.revealed_task_id,
            "tasks_version": session.tasks_version,
            "estimation_mode": session.estimation_mode,
        }

    @staticmethod
    def from_dict(
        data: dict,
        fallback_chat_id: Optional[int] = None,
        fallback_topic_id: Optional[int] = None,
    ) -> Session:
        chat_id = int(data.get("chat_id") if data.get("chat_id") is not None else fallback_chat_id)
        topic_id = data.get("topic_id", fallback_topic_id)
        identity = f"{chat_id}:{'none' if topic_id is None else topic_id}"
        participants = {
            int(uid): Participant.from_dict(int(uid), participant_data)
            for uid, participant_data in data.get("participants", {}).items()
        }
        return Session(
            chat_id=chat_id,
            topic_id=topic_id,
            participants=participants,
            tasks_queue=[
                Task.from_dict(task_data, legacy_context=f"{identity}:tasks_queue:{index}")
                for index, task_data in enumerate(data.get("tasks_queue", []))
            ],
            current_task_index=data.get("current_task_index", 0),
            history=[
                Task.from_dict(task_data, legacy_context=f"{identity}:history:{index}")
                for index, task_data in enumerate(data.get("history", []))
            ],
            last_batch=[
                Task.from_dict(task_data, legacy_context=f"{identity}:last_batch:{index}")
                for index, task_data in enumerate(data.get("last_batch", []))
            ],
            batch_completed=data.get("batch_completed", False),
            active_vote_message_id=data.get("active_vote_message_id"),
            current_batch_id=data.get("current_batch_id"),
            current_batch_started_at=data.get("current_batch_started_at"),
            last_batch_started_at=data.get("last_batch_started_at"),
            revealed_task_id=data.get("revealed_task_id"),
            tasks_version=data.get("tasks_version", 0),
            estimation_mode=normalise_estimation_mode(data.get("estimation_mode")),
        )
