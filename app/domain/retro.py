"""Retrospective domain model.

A retrospective mirrors the planning-poker session mechanics: a manager
configures it, the team joins by a public link, and everything is driven
live. Where poker walks through *tasks* one at a time, a retro walks
through *sections* (categories) one at a time:

    lobby -> collecting -> voting -> discussing -> done

* lobby       — created, participants joining, no input yet.
* collecting  — exactly one section is "active"; participants add
                anonymous cards into that section only. The manager
                advances sections one by one (each with an optional soft
                timer deadline).
* voting      — dot-voting across all cards, capped per person.
* discussing  — cards sorted by votes; the manager captures action items.
* done        — finalized; the AI analysis can run.

Cards are anonymous to everyone in the product surface: public and CMS
projections never expose authors or per-user vote identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


# Phase constants — kept as plain strings so the JSON contract is stable
# across the wire without an enum import on the frontend.
PHASE_LOBBY = "lobby"
PHASE_COLLECTING = "collecting"
PHASE_VOTING = "voting"
PHASE_DISCUSSING = "discussing"
PHASE_DONE = "done"

PHASES = (PHASE_LOBBY, PHASE_COLLECTING, PHASE_VOTING, PHASE_DISCUSSING, PHASE_DONE)

DEFAULT_VOTES_PER_PERSON = 5
DEFAULT_SECTION_SECONDS = 300  # 5 minutes
MAX_CARDS_PER_RETRO = 500


class RetroError(Exception):
    """Domain-level guard violation. Carries an HTTP-ish status for the API layer."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class RetroSection:
    """A retro column / category, e.g. "По итогам спринта"."""

    section_id: str
    title: str

    def to_dict(self) -> Dict[str, Any]:
        return {"section_id": self.section_id, "title": self.title}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetroSection":
        return cls(
            section_id=str(data["section_id"]),
            title=str(data.get("title", "")),
        )


@dataclass
class RetroCard:
    """A single anonymous card added by a participant.

    ``votes`` is the set of participant ``user_id``s who placed a dot on
    this card (one dot per person per card). The per-person budget is
    enforced at the retro level, not here.
    """

    card_id: str
    section_id: str
    text: str
    author_id: int
    author_name: str
    created_at: str
    group_id: Optional[str] = None
    votes: set[int] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "card_id": self.card_id,
            "section_id": self.section_id,
            "text": self.text,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "created_at": self.created_at,
            "group_id": self.group_id,
            "votes": sorted(self.votes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetroCard":
        return cls(
            card_id=str(data["card_id"]),
            section_id=str(data["section_id"]),
            text=str(data.get("text", "")),
            author_id=int(data.get("author_id", 0)),
            author_name=str(data.get("author_name", "")),
            created_at=str(data.get("created_at", "")),
            group_id=(data.get("group_id") or None),
            votes={int(uid) for uid in data.get("votes", [])},
        )


@dataclass
class RetroGroup:
    """Manager-created cluster of similar cards.

    Once cards are in a group, participants vote for the group instead of
    individual cards inside it. The source cards remain intact for discussion
    and AI context.
    """

    group_id: str
    section_id: str
    title: str
    card_ids: List[str]
    created_at: str
    votes: set[int] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "section_id": self.section_id,
            "title": self.title,
            "card_ids": list(self.card_ids),
            "created_at": self.created_at,
            "votes": sorted(self.votes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetroGroup":
        return cls(
            group_id=str(data["group_id"]),
            section_id=str(data["section_id"]),
            title=str(data.get("title", "")),
            card_ids=[str(card_id) for card_id in data.get("card_ids", [])],
            created_at=str(data.get("created_at", "")),
            votes={int(uid) for uid in data.get("votes", [])},
        )


@dataclass
class RetroActionItem:
    """A follow-up action captured by the manager during discussion."""

    item_id: str
    text: str
    assignee: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "text": self.text,
            "assignee": self.assignee,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetroActionItem":
        return cls(
            item_id=str(data["item_id"]),
            text=str(data.get("text", "")),
            assignee=(data.get("assignee") or None),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class RetroParticipant:
    """Team member who joined via the public link."""

    user_id: int
    name: str
    role: str

    def to_dict(self) -> Dict[str, Any]:
        return {"user_id": self.user_id, "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetroParticipant":
        return cls(
            user_id=int(data["user_id"]),
            name=str(data.get("name", "")),
            role=str(data.get("role", "")),
        )


@dataclass
class Retrospective:
    """Aggregate root for a live retrospective."""

    retro_id: int
    title: str
    sections: List[RetroSection] = field(default_factory=list)
    votes_per_person: int = DEFAULT_VOTES_PER_PERSON
    default_section_seconds: int = DEFAULT_SECTION_SECONDS
    phase: str = PHASE_LOBBY
    active_section_id: Optional[str] = None
    section_deadline: Optional[str] = None
    visited_section_ids: List[str] = field(default_factory=list)
    participants: Dict[int, RetroParticipant] = field(default_factory=dict)
    cards: List[RetroCard] = field(default_factory=list)
    groups: List[RetroGroup] = field(default_factory=list)
    action_items: List[RetroActionItem] = field(default_factory=list)
    ai_summary: Optional[Dict[str, Any]] = None
    version: int = 0

    # -- lookups ----------------------------------------------------------

    def has_section(self, section_id: str) -> bool:
        return any(s.section_id == section_id for s in self.sections)

    def section_title(self, section_id: str) -> str:
        for section in self.sections:
            if section.section_id == section_id:
                return section.title
        return section_id

    def cards_in_section(self, section_id: str) -> List[RetroCard]:
        return [card for card in self.cards if card.section_id == section_id]

    def votes_used_by(self, user_id: int) -> int:
        card_votes = sum(1 for card in self.cards if card.group_id is None and user_id in card.votes)
        group_votes = sum(1 for group in self.groups if user_id in group.votes)
        return card_votes + group_votes

    def find_card(self, card_id: str) -> Optional[RetroCard]:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        return None

    def find_group(self, group_id: str) -> Optional[RetroGroup]:
        for group in self.groups:
            if group.group_id == group_id:
                return group
        return None

    def group_for_card(self, card_id: str) -> Optional[RetroGroup]:
        card = self.find_card(card_id)
        if card is None or card.group_id is None:
            return None
        return self.find_group(card.group_id)

    def bump_version(self) -> None:
        self.version += 1

    # -- guards / mutations ----------------------------------------------

    def add_card(
        self,
        *,
        card_id: str,
        section_id: str,
        text: str,
        author_id: int,
        author_name: str,
        created_at: str,
    ) -> RetroCard:
        """Add an anonymous card. Only allowed in the active section while collecting."""
        clean = text.strip()
        if not clean:
            raise RetroError("Карточка не может быть пустой")
        if self.phase != PHASE_COLLECTING:
            raise RetroError("Сейчас нельзя добавлять карточки", status_code=409)
        if section_id != self.active_section_id:
            raise RetroError("Эта секция сейчас закрыта", status_code=409)
        if not self.has_section(section_id):
            raise RetroError("Секция не найдена", status_code=404)
        if len(self.cards) >= MAX_CARDS_PER_RETRO:
            raise RetroError("Достигнут лимит карточек для ретро", status_code=409)
        card = RetroCard(
            card_id=card_id,
            section_id=section_id,
            text=clean[:1000],
            author_id=author_id,
            author_name=author_name,
            created_at=created_at,
        )
        self.cards.append(card)
        self.bump_version()
        return card

    def toggle_vote(self, target_id: str, user_id: int, target_type: str = "card") -> Union[RetroCard, RetroGroup]:
        """Place or remove a dot on a card or group.

        Grouped cards are not votable directly; their group is the voting
        target. The per-person budget is shared across card and group targets.
        """
        if self.phase != PHASE_VOTING:
            raise RetroError("Голосование сейчас недоступно", status_code=409)
        if target_type == "group":
            target = self.find_group(target_id)
            if target is None:
                raise RetroError("Группа не найдена", status_code=404)
        else:
            target = self.find_card(target_id)
            if target is None:
                raise RetroError("Карточка не найдена", status_code=404)
            if target.group_id is not None:
                raise RetroError("Голосуйте за группу, в которую входит карточка", status_code=409)

        if user_id in target.votes:
            target.votes.discard(user_id)
            self.bump_version()
            return target
        if self.votes_used_by(user_id) >= self.votes_per_person:
            raise RetroError(
                f"Лимит голосов исчерпан ({self.votes_per_person})", status_code=409
            )
        target.votes.add(user_id)
        self.bump_version()
        return target

    def create_group(self, *, group_id: str, title: str, card_ids: List[str], created_at: str) -> RetroGroup:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        clean = title.strip()
        if not clean:
            raise RetroError("Название группы не может быть пустым")
        unique_ids = list(dict.fromkeys(card_ids))
        if len(unique_ids) < 2:
            raise RetroError("Выберите минимум две карточки для группы")

        cards: List[RetroCard] = []
        section_id: Optional[str] = None
        migrated_votes: set[int] = set()
        for card_id in unique_ids:
            card = self.find_card(card_id)
            if card is None:
                raise RetroError("Карточка не найдена", status_code=404)
            if card.group_id is not None:
                raise RetroError("Карточка уже входит в группу", status_code=409)
            if section_id is None:
                section_id = card.section_id
            elif card.section_id != section_id:
                raise RetroError("Группировать можно карточки только из одной секции", status_code=409)
            migrated_votes.update(card.votes)
            cards.append(card)

        group = RetroGroup(
            group_id=group_id,
            section_id=section_id or "",
            title=clean[:120],
            card_ids=unique_ids,
            created_at=created_at,
            votes=migrated_votes,
        )
        for card in cards:
            card.group_id = group_id
            card.votes.clear()
        self.groups.append(group)
        self.bump_version()
        return group

    def rename_group(self, group_id: str, title: str) -> RetroGroup:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        group = self.find_group(group_id)
        if group is None:
            raise RetroError("Группа не найдена", status_code=404)
        clean = title.strip()
        if not clean:
            raise RetroError("Название группы не может быть пустым")
        group.title = clean[:120]
        self.bump_version()
        return group

    def ungroup(self, group_id: str) -> bool:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        group = self.find_group(group_id)
        if group is None:
            raise RetroError("Группа не найдена", status_code=404)
        for card in self.cards:
            if card.group_id == group_id:
                card.group_id = None
        self.groups = [item for item in self.groups if item.group_id != group_id]
        self.bump_version()
        return True

    def add_action_item(self, *, item_id: str, text: str, assignee: Optional[str], created_at: str) -> RetroActionItem:
        if self.phase != PHASE_DISCUSSING:
            raise RetroError("Action items можно добавлять только на этапе обсуждения", status_code=409)
        clean = text.strip()
        if not clean:
            raise RetroError("Action item не может быть пустым")
        item = RetroActionItem(item_id=item_id, text=clean[:500], assignee=(assignee or None), created_at=created_at)
        self.action_items.append(item)
        self.bump_version()
        return item

    def remove_action_item(self, item_id: str) -> bool:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        before = len(self.action_items)
        self.action_items = [item for item in self.action_items if item.item_id != item_id]
        changed = len(self.action_items) != before
        if changed:
            self.bump_version()
        return changed

    def open_section(self, section_id: str, deadline: Optional[str]) -> None:
        """Manager opens a section for collection. Moves phase to collecting."""
        if not self.has_section(section_id):
            raise RetroError("Секция не найдена", status_code=404)
        if self.phase in (PHASE_VOTING, PHASE_DISCUSSING, PHASE_DONE):
            raise RetroError("Сбор карточек уже завершён", status_code=409)
        self.phase = PHASE_COLLECTING
        self.active_section_id = section_id
        self.section_deadline = deadline
        if section_id not in self.visited_section_ids:
            self.visited_section_ids.append(section_id)
        self.bump_version()

    def close_section(self) -> None:
        """Pause collection (e.g. timer ended) — section stays selected but locked."""
        self.active_section_id = None
        self.section_deadline = None
        self.bump_version()

    def start_voting(self) -> None:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        if self.phase != PHASE_COLLECTING:
            raise RetroError("Сначала откройте хотя бы одну секцию", status_code=409)
        missing = [section.title for section in self.sections if section.section_id not in self.visited_section_ids]
        if missing:
            raise RetroError("Сначала откройте все секции: " + ", ".join(missing), status_code=409)
        self.phase = PHASE_VOTING
        self.active_section_id = None
        self.section_deadline = None
        self.bump_version()

    def start_discussion(self) -> None:
        if self.phase == PHASE_DONE:
            raise RetroError("Ретро уже завершено", status_code=409)
        if self.phase != PHASE_VOTING:
            raise RetroError("Сначала запустите голосование", status_code=409)
        self.phase = PHASE_DISCUSSING
        self.active_section_id = None
        self.section_deadline = None
        self.bump_version()

    def finalize(self) -> None:
        self.phase = PHASE_DONE
        self.active_section_id = None
        self.section_deadline = None
        self.bump_version()

    def add_participant(self, user_id: int, name: str, role: str) -> bool:
        """Add or refresh a participant. Returns True when state changed."""
        existing = self.participants.get(user_id)
        if existing is not None:
            if existing.name == name and existing.role == role:
                return False
            existing.name = name
            existing.role = role
            self.bump_version()
            return True
        self.participants[user_id] = RetroParticipant(user_id=user_id, name=name, role=role)
        self.bump_version()
        return True


class RetrospectiveFactory:
    """Serialize / deserialize retrospectives for Redis + Postgres JSONB."""

    @staticmethod
    def to_dict(retro: Retrospective) -> Dict[str, Any]:
        return {
            "retro_id": retro.retro_id,
            "title": retro.title,
            "sections": [s.to_dict() for s in retro.sections],
            "votes_per_person": retro.votes_per_person,
            "default_section_seconds": retro.default_section_seconds,
            "phase": retro.phase,
            "active_section_id": retro.active_section_id,
            "section_deadline": retro.section_deadline,
            "visited_section_ids": list(retro.visited_section_ids),
            "participants": {str(uid): p.to_dict() for uid, p in retro.participants.items()},
            "cards": [c.to_dict() for c in retro.cards],
            "groups": [g.to_dict() for g in retro.groups],
            "action_items": [a.to_dict() for a in retro.action_items],
            "ai_summary": retro.ai_summary,
            "version": retro.version,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any], fallback_retro_id: Optional[int] = None) -> Retrospective:
        raw_retro_id = data.get("retro_id") if data.get("retro_id") is not None else fallback_retro_id
        if raw_retro_id is None:
            raise ValueError("retro_id is required")
        retro_id = int(raw_retro_id)
        phase = str(data.get("phase", PHASE_LOBBY))
        if phase not in PHASES:
            phase = PHASE_LOBBY
        participants = {}
        for uid, participant_data in data.get("participants", {}).items():
            try:
                participants[int(uid)] = RetroParticipant.from_dict(participant_data)
            except (TypeError, ValueError, KeyError):
                continue
        sections = []
        for section_data in data.get("sections", []):
            try:
                sections.append(RetroSection.from_dict(section_data))
            except (TypeError, ValueError, KeyError):
                continue
        cards = []
        for card_data in data.get("cards", []):
            try:
                cards.append(RetroCard.from_dict(card_data))
            except (TypeError, ValueError, KeyError):
                continue
        groups = []
        for group_data in data.get("groups", []):
            try:
                group = RetroGroup.from_dict(group_data)
            except (TypeError, ValueError, KeyError):
                continue
            existing_card_ids = {card.card_id for card in cards}
            group.card_ids = [card_id for card_id in group.card_ids if card_id in existing_card_ids]
            if group.card_ids:
                groups.append(group)
        group_ids = {group.group_id for group in groups}
        card_group_id = {
            card_id: group.group_id
            for group in groups
            for card_id in group.card_ids
        }
        for card in cards:
            if card.card_id in card_group_id:
                card.group_id = card_group_id[card.card_id]
            if card.group_id not in group_ids:
                card.group_id = None
        action_items = []
        for action_data in data.get("action_items", []):
            try:
                action_items.append(RetroActionItem.from_dict(action_data))
            except (TypeError, ValueError, KeyError):
                continue
        section_ids = [section.section_id for section in sections]
        raw_visited = data.get("visited_section_ids")
        if isinstance(raw_visited, list):
            visited_section_ids = [str(section_id) for section_id in raw_visited if str(section_id) in section_ids]
        elif phase in (PHASE_VOTING, PHASE_DISCUSSING, PHASE_DONE):
            visited_section_ids = list(section_ids)
        else:
            active_section_id = data.get("active_section_id")
            visited_section_ids = [active_section_id] if active_section_id in section_ids else []
        return Retrospective(
            retro_id=retro_id,
            title=str(data.get("title", "")),
            sections=sections,
            votes_per_person=int(data.get("votes_per_person", DEFAULT_VOTES_PER_PERSON)),
            default_section_seconds=int(data.get("default_section_seconds", DEFAULT_SECTION_SECONDS)),
            phase=phase,
            active_section_id=data.get("active_section_id"),
            section_deadline=data.get("section_deadline"),
            visited_section_ids=visited_section_ids,
            participants=participants,
            cards=cards,
            groups=groups,
            action_items=action_items,
            ai_summary=data.get("ai_summary"),
            version=int(data.get("version", 0)),
        )
