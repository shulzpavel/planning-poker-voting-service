"""Domain-rule tests for the Retrospective aggregate.

These pin the state-machine guards (card collection only in the active
section, dot-vote budget, phase transitions) and the JSON round-trip used
by both the Redis live store and the Postgres durable copy.
"""

import pytest

from app.domain.retro import (
    PHASE_COLLECTING,
    PHASE_DISCUSSING,
    PHASE_DONE,
    PHASE_LOBBY,
    PHASE_VOTING,
    Retrospective,
    RetrospectiveFactory,
    RetroError,
    RetroSection,
)


def _retro(**overrides) -> Retrospective:
    base = Retrospective(
        retro_id=1,
        title="Sprint 42 retro",
        sections=[
            RetroSection(section_id="task", title="По задаче"),
            RetroSection(section_id="sprint", title="По итогам спринта"),
            RetroSection(section_id="process", title="По процессам"),
        ],
        votes_per_person=3,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _mark_all_sections_opened(retro: Retrospective) -> None:
    retro.visited_section_ids = [section.section_id for section in retro.sections]


# ---------------------------------------------------------------------------
# Card collection
# ---------------------------------------------------------------------------


def test_add_card_only_in_active_section_while_collecting():
    retro = _retro()
    retro.open_section("task", deadline=None)

    card = retro.add_card(
        card_id="c1",
        section_id="task",
        text="  Хорошо поработали  ",
        author_id=-7,
        author_name="a@x.test",
        created_at="2026-01-01T00:00:00",
    )
    assert card.text == "Хорошо поработали"  # trimmed
    assert retro.cards_in_section("task") == [card]


def test_add_card_rejected_when_section_not_active():
    retro = _retro()
    retro.open_section("task", deadline=None)
    with pytest.raises(RetroError) as info:
        retro.add_card(
            card_id="c1",
            section_id="sprint",
            text="hi",
            author_id=-7,
            author_name="a@x.test",
            created_at="t",
        )
    assert info.value.status_code == 409


def test_add_card_rejected_outside_collecting_phase():
    retro = _retro(phase=PHASE_VOTING)
    with pytest.raises(RetroError) as info:
        retro.add_card(
            card_id="c1",
            section_id="task",
            text="hi",
            author_id=-7,
            author_name="a@x.test",
            created_at="t",
        )
    assert info.value.status_code == 409


def test_add_card_rejects_empty_text():
    retro = _retro()
    retro.open_section("task", deadline=None)
    with pytest.raises(RetroError):
        retro.add_card(
            card_id="c1",
            section_id="task",
            text="   ",
            author_id=-7,
            author_name="a@x.test",
            created_at="t",
        )


# ---------------------------------------------------------------------------
# Dot voting
# ---------------------------------------------------------------------------


def _seed_cards(retro: Retrospective, count: int) -> None:
    retro.open_section("task", deadline=None)
    for i in range(count):
        retro.add_card(
            card_id=f"c{i}",
            section_id="task",
            text=f"card {i}",
            author_id=-7,
            author_name="a@x.test",
            created_at="t",
        )


def test_vote_toggles_and_respects_budget():
    retro = _retro(votes_per_person=2)
    _seed_cards(retro, 3)
    _mark_all_sections_opened(retro)
    retro.start_voting()

    retro.toggle_vote("c0", user_id=-1)
    retro.toggle_vote("c1", user_id=-1)
    assert retro.votes_used_by(-1) == 2

    # Third distinct card exceeds the budget of 2.
    with pytest.raises(RetroError) as info:
        retro.toggle_vote("c2", user_id=-1)
    assert info.value.status_code == 409

    # Un-voting frees budget back up.
    retro.toggle_vote("c0", user_id=-1)
    assert retro.votes_used_by(-1) == 1
    retro.toggle_vote("c2", user_id=-1)
    assert retro.votes_used_by(-1) == 2


def test_vote_rejected_outside_voting_phase():
    retro = _retro()
    _seed_cards(retro, 1)  # leaves phase=collecting
    with pytest.raises(RetroError) as info:
        retro.toggle_vote("c0", user_id=-1)
    assert info.value.status_code == 409


def test_vote_unknown_card_404():
    retro = _retro()
    _seed_cards(retro, 1)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    with pytest.raises(RetroError) as info:
        retro.toggle_vote("missing", user_id=-1)
    assert info.value.status_code == 404


def test_grouping_moves_votes_to_group_and_blocks_card_votes():
    retro = _retro(votes_per_person=2)
    _seed_cards(retro, 3)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.toggle_vote("c0", user_id=-1)
    retro.toggle_vote("c1", user_id=-2)

    group = retro.create_group(
        group_id="g1",
        title="Release pain",
        card_ids=["c0", "c1"],
        created_at="t",
    )

    assert group.votes == {-1, -2}
    assert retro.find_card("c0").group_id == "g1"
    assert retro.find_card("c0").votes == set()
    assert retro.votes_used_by(-1) == 1
    with pytest.raises(RetroError) as info:
        retro.toggle_vote("c0", user_id=-1)
    assert info.value.status_code == 409

    retro.toggle_vote("g1", user_id=-1, target_type="group")
    assert retro.votes_used_by(-1) == 0


def test_grouping_requires_same_section_and_unique_cards():
    retro = _retro()
    retro.open_section("task", deadline=None)
    retro.add_card(card_id="c0", section_id="task", text="a", author_id=-7, author_name="a@x.test", created_at="t")
    retro.close_section()
    retro.open_section("sprint", deadline=None)
    retro.add_card(card_id="c1", section_id="sprint", text="b", author_id=-7, author_name="a@x.test", created_at="t")

    with pytest.raises(RetroError) as info:
        retro.create_group(group_id="g1", title="Mixed", card_ids=["c0", "c1"], created_at="t")
    assert info.value.status_code == 409


def test_rename_and_ungroup_restores_card_voting():
    retro = _retro(votes_per_person=1)
    _seed_cards(retro, 2)
    group = retro.create_group(group_id="g1", title="Old", card_ids=["c0", "c1"], created_at="t")
    assert retro.rename_group(group.group_id, "New").title == "New"
    assert retro.ungroup(group.group_id) is True
    assert retro.groups == []
    assert retro.find_card("c0").group_id is None
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.toggle_vote("c0", user_id=-1)
    assert retro.votes_used_by(-1) == 1


def test_group_mutations_rejected_after_done():
    retro = _retro()
    _seed_cards(retro, 2)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.start_discussion()
    retro.finalize()

    with pytest.raises(RetroError) as info:
        retro.create_group(group_id="g1", title="Late", card_ids=["c0", "c1"], created_at="t")
    assert info.value.status_code == 409


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------


def test_phase_flow_lobby_to_done():
    retro = _retro()
    assert retro.phase == PHASE_LOBBY
    retro.open_section("task", deadline="2026-01-01T00:05:00")
    assert retro.phase == PHASE_COLLECTING
    assert retro.active_section_id == "task"
    retro.open_section("sprint", deadline=None)
    retro.open_section("process", deadline=None)
    retro.start_voting()
    assert retro.phase == PHASE_VOTING
    assert retro.active_section_id is None
    retro.start_discussion()
    assert retro.phase == PHASE_DISCUSSING
    retro.finalize()
    assert retro.phase == PHASE_DONE


def test_open_section_after_voting_is_rejected():
    retro = _retro()
    retro.open_section("task", deadline=None)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    with pytest.raises(RetroError) as info:
        retro.open_section("task", deadline=None)
    assert info.value.status_code == 409


def test_open_unknown_section_404():
    retro = _retro()
    with pytest.raises(RetroError) as info:
        retro.open_section("nope", deadline=None)
    assert info.value.status_code == 404


def test_start_voting_requires_all_sections_opened():
    retro = _retro()
    retro.open_section("task", deadline=None)

    with pytest.raises(RetroError) as info:
        retro.start_voting()

    assert info.value.status_code == 409
    assert "Сначала откройте все секции" in info.value.message


def test_open_section_tracks_visited_sections_once():
    retro = _retro()
    retro.open_section("task", deadline=None)
    retro.open_section("sprint", deadline=None)
    retro.open_section("task", deadline=None)

    assert retro.visited_section_ids == ["task", "sprint"]


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------


def test_action_items_add_and_remove():
    retro = _retro()
    retro.open_section("task", deadline=None)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.start_discussion()
    item = retro.add_action_item(item_id="a1", text="Fix CI", assignee="lead", created_at="t")
    assert item.text == "Fix CI"
    assert retro.action_items == [item]
    assert retro.remove_action_item("a1") is True
    assert retro.action_items == []
    assert retro.remove_action_item("a1") is False


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


def test_add_participant_inserts_then_refreshes():
    retro = _retro()
    assert retro.add_participant(-7, "old@x.test", "backend") is True
    assert retro.add_participant(-7, "new@x.test", "qa") is True
    assert retro.add_participant(-7, "new@x.test", "qa") is False
    assert retro.participants[-7].name == "new@x.test"
    assert retro.participants[-7].role == "qa"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


def test_factory_round_trip_preserves_state():
    retro = _retro(votes_per_person=4)
    retro.open_section("task", deadline="2026-01-01T00:05:00")
    retro.add_card(
        card_id="c0",
        section_id="task",
        text="card",
        author_id=-7,
        author_name="a@x.test",
        created_at="t",
    )
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.toggle_vote("c0", user_id=-1)
    retro.start_discussion()
    retro.add_action_item(item_id="a1", text="do", assignee=None, created_at="t")
    retro.add_participant(-7, "a@x.test", "backend")

    data = RetrospectiveFactory.to_dict(retro)
    restored = RetrospectiveFactory.from_dict(data)

    assert restored.retro_id == retro.retro_id
    assert restored.title == retro.title
    assert [s.section_id for s in restored.sections] == ["task", "sprint", "process"]
    assert restored.votes_per_person == 4
    assert restored.phase == PHASE_DISCUSSING
    assert restored.visited_section_ids == ["task", "sprint", "process"]
    assert restored.find_card("c0").votes == {-1}
    assert restored.action_items[0].text == "do"
    assert restored.participants[-7].role == "backend"


def test_factory_round_trip_preserves_groups():
    retro = _retro(votes_per_person=3)
    _seed_cards(retro, 2)
    _mark_all_sections_opened(retro)
    retro.start_voting()
    retro.toggle_vote("c0", user_id=-1)
    retro.create_group(group_id="g1", title="Grouped", card_ids=["c0", "c1"], created_at="t")

    restored = RetrospectiveFactory.from_dict(RetrospectiveFactory.to_dict(retro))

    assert restored.groups[0].title == "Grouped"
    assert restored.groups[0].votes == {-1}
    assert restored.find_card("c0").group_id == "g1"


def test_factory_defaults_bad_phase_to_lobby():
    restored = RetrospectiveFactory.from_dict({"retro_id": 5, "phase": "garbage"})
    assert restored.phase == PHASE_LOBBY
    assert restored.retro_id == 5
