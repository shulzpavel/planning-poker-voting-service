"""End-to-end tests for the retrospective API.

Mounts only ``retro_router`` on a bare FastAPI app with an in-memory retro
repository, a fake Redis (tokens / participants / rate-limit / pub-sub),
and a fake CMS store. Manager auth is bypassed via ``dependency_overrides``
on ``_require_auth`` so the cookie/permission stack isn't needed here.
"""

import itertools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.retro_memory import MemoryRetroRepository
from services.voting_service import retro_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async Redis stand-in for tokens, participants, and counters."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return True

    async def eval(self, *args, **kwargs):
        return 1  # rate limiter: always under the limit

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 0


class FakeCmsStore:
    """In-memory cms_retros table mirroring PostgresCmsStore return shapes."""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self.audit: list[tuple] = []

    def _row(self, retro_id):
        row = self._rows.get(retro_id)
        return dict(row) if row else None

    async def list_retros(self, **kwargs):
        return [dict(r) for r in self._rows.values()]

    async def get_retro(self, retro_id):
        return self._row(retro_id)

    async def create_retro(self, title, config, created_by, team_id=None):
        retro_id = next(self._ids)
        self._rows[retro_id] = {
            "id": retro_id,
            "title": title,
            "status": "draft",
            "config": config,
            "snapshot": None,
            "ai_summary": None,
            "created_by": created_by,
            "team_id": team_id,
            "team": None,
        }
        return self._row(retro_id)

    async def update_retro_config(self, retro_id, title, config):
        if retro_id not in self._rows:
            return None
        self._rows[retro_id].update(title=title, config=config)
        return self._row(retro_id)

    async def update_retro_status(self, retro_id, status):
        if retro_id not in self._rows:
            return None
        self._rows[retro_id]["status"] = status
        return self._row(retro_id)

    async def save_retro_snapshot(self, retro_id, snapshot, status=None):
        if retro_id not in self._rows:
            return None
        self._rows[retro_id]["snapshot"] = snapshot
        if status:
            self._rows[retro_id]["status"] = status
        return self._row(retro_id)

    async def save_retro_ai_summary(self, retro_id, ai_summary):
        if retro_id not in self._rows:
            return None
        self._rows[retro_id]["ai_summary"] = ai_summary
        return self._row(retro_id)

    async def delete_retro(self, retro_id):
        return self._rows.pop(retro_id, None) is not None

    async def record_audit_event(self, **kwargs):
        self.audit.append(kwargs)


def _principal() -> CmsPrincipal:
    return CmsPrincipal(
        id=1,
        username="admin",
        display_name="Admin",
        is_superuser=True,
        permissions=frozenset(),
        roles=(),
        pages=(),
        team_ids=frozenset(),
        teams=(),
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(retro_api.retro_router, prefix="/api/v1")
    app.state.web_redis = FakeRedis()
    app.state.retro_repository = MemoryRetroRepository()
    app.state.cms_store = FakeCmsStore()
    app.dependency_overrides[_require_auth] = lambda: _principal()
    with TestClient(app) as test_client:
        test_client.app = app
        yield test_client


# ---------------------------------------------------------------------------
# Full happy path: create -> invite -> join -> collect -> vote -> finalize
# ---------------------------------------------------------------------------


def test_full_retro_flow(client):
    # Manager creates a retro.
    create = client.post("/api/v1/cms/retros", json={
        "title": "Sprint 42 retro",
        "config": {
            "sections": [
                {"section_id": "sprint", "title": "По итогам спринта"},
                {"section_id": "process", "title": "По процессам"},
            ],
            "votes_per_person": 3,
            "default_section_seconds": 0,
        },
    })
    assert create.status_code == 200
    retro_id = create.json()["id"]

    # Manager mints the public invite (bootstraps live state).
    invite = client.post(f"/api/v1/cms/retros/{retro_id}/invite")
    assert invite.status_code == 200
    token = invite.json()["token"]
    assert invite.json()["state"]["phase"] == "lobby"

    # Participant joins.
    join = client.post("/api/v1/retro/join", json={
        "token": token, "name": "alice@example.com", "role": "backend",
    })
    assert join.status_code == 200
    pid = join.json()["participant_id"]
    assert join.json()["state"]["participants_count"] == 1

    # Adding a card before a section is open is rejected.
    early = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "sprint", "text": "nope",
    })
    assert early.status_code == 409

    # Manager opens a section.
    opened = client.post(f"/api/v1/cms/retros/{retro_id}/open-section", json={"section_id": "sprint"})
    assert opened.status_code == 200
    assert opened.json()["phase"] == "collecting"
    assert opened.json()["active_section_id"] == "sprint"

    # Participant adds a card into the active section.
    card = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "sprint", "text": "Стало быстрее релизить",
    })
    assert card.status_code == 200
    cards = card.json()["cards"]
    assert len(cards) == 1
    card_id = cards[0]["card_id"]

    # Writing into a non-active section is rejected.
    wrong = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "process", "text": "x",
    })
    assert wrong.status_code == 409

    blocked_voting = client.post(f"/api/v1/cms/retros/{retro_id}/phase", json={"target": "voting"})
    assert blocked_voting.status_code == 409

    # Manager opens the remaining section; voting unlocks after every block was opened.
    opened_second = client.post(f"/api/v1/cms/retros/{retro_id}/open-section", json={"section_id": "process"})
    assert opened_second.status_code == 200
    assert opened_second.json()["visited_section_ids"] == ["sprint", "process"]

    # Manager moves to voting.
    voting = client.post(f"/api/v1/cms/retros/{retro_id}/phase", json={"target": "voting"})
    assert voting.status_code == 200
    assert voting.json()["phase"] == "voting"

    # Participant votes (toggle on).
    vote = client.post("/api/v1/retro/vote", json={
        "token": token, "participant_id": pid, "card_id": card_id,
    })
    assert vote.status_code == 200
    assert vote.json()["cards"][0]["vote_count"] == 1
    assert vote.json()["my_votes"] == [card_id]
    assert vote.json()["my_votes_remaining"] == 2

    discussion = client.post(f"/api/v1/cms/retros/{retro_id}/phase", json={"target": "discussing"})
    assert discussion.status_code == 200
    assert discussion.json()["phase"] == "discussing"

    # Manager captures an action item, then finalizes.
    action = client.post(f"/api/v1/cms/retros/{retro_id}/action-items", json={"text": "Ускорить ревью"})
    assert action.status_code == 200
    assert len(action.json()["action_items"]) == 1

    final = client.post(f"/api/v1/cms/retros/{retro_id}/finalize")
    assert final.status_code == 200
    assert final.json()["phase"] == "done"

    # Snapshot persisted to the CMS store.
    row = client.app.state.cms_store._rows[retro_id]
    assert row["status"] == "done"
    assert row["snapshot"]["phase"] == "done"
    assert "participants" not in row["snapshot"]
    assert "author_name" not in row["snapshot"]["cards"][0]
    assert "votes" not in row["snapshot"]["cards"][0]
    assert row["snapshot"]["cards"][0]["vote_count"] == 1

    detail = client.get(f"/api/v1/cms/retros/{retro_id}")
    assert detail.status_code == 200
    assert "participants" not in detail.json()["snapshot"]
    assert "author_id" not in detail.json()["snapshot"]["cards"][0]


def test_close_section_then_open_next_section(client):
    create = client.post("/api/v1/cms/retros", json={
        "title": "sections",
        "config": {
            "sections": [
                {"section_id": "went_well", "title": "Good"},
                {"section_id": "pain_points", "title": "Pain"},
            ],
            "votes_per_person": 3,
            "default_section_seconds": 0,
        },
    })
    retro_id = create.json()["id"]
    token = client.post(f"/api/v1/cms/retros/{retro_id}/invite").json()["token"]
    pid = client.post("/api/v1/retro/join", json={
        "token": token, "name": "facilitator@example.com", "role": "backend",
    }).json()["participant_id"]

    opened = client.post(
        f"/api/v1/cms/retros/{retro_id}/open-section",
        json={"section_id": "went_well"},
    )
    assert opened.json()["active_section_id"] == "went_well"
    client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "went_well", "text": "ship fast",
    })

    paused = client.post(f"/api/v1/cms/retros/{retro_id}/close-section")
    assert paused.status_code == 200
    assert paused.json()["phase"] == "collecting"
    assert paused.json()["active_section_id"] is None

    blocked = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "pain_points", "text": "blocked",
    })
    assert blocked.status_code == 409

    resumed = client.post(
        f"/api/v1/cms/retros/{retro_id}/open-section",
        json={"section_id": "pain_points"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["active_section_id"] == "pain_points"

    card = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "pain_points", "text": "slow ci",
    })
    assert card.status_code == 200


def test_join_rejects_bad_email(client):
    create = client.post("/api/v1/cms/retros", json={"title": "r", "config": {"sections": []}})
    retro_id = create.json()["id"]
    token = client.post(f"/api/v1/cms/retros/{retro_id}/invite").json()["token"]

    bad = client.post("/api/v1/retro/join", json={
        "token": token, "name": "alice@gmail.com", "role": "backend",
    })
    assert bad.status_code == 400


def test_state_endpoint_returns_my_votes(client):
    create = client.post("/api/v1/cms/retros", json={
        "title": "r",
        "config": {"sections": [{"section_id": "s", "title": "S"}], "votes_per_person": 2, "default_section_seconds": 0},
    })
    retro_id = create.json()["id"]
    token = client.post(f"/api/v1/cms/retros/{retro_id}/invite").json()["token"]
    pid = client.post("/api/v1/retro/join", json={
        "token": token, "name": "bob@example.com", "role": "qa",
    }).json()["participant_id"]
    client.post(f"/api/v1/cms/retros/{retro_id}/open-section", json={"section_id": "s"})
    card_id = client.post("/api/v1/retro/card", json={
        "token": token, "participant_id": pid, "section_id": "s", "text": "hi",
    }).json()["cards"][0]["card_id"]
    client.post(f"/api/v1/cms/retros/{retro_id}/phase", json={"target": "voting"})
    client.post("/api/v1/retro/vote", json={"token": token, "participant_id": pid, "card_id": card_id})

    state = client.get(f"/api/v1/retro/state/{token}", params={"participant_id": pid})
    assert state.status_code == 200
    assert state.json()["my_votes"] == [card_id]

    # Without participant_id the projection hides per-viewer dots.
    anon = client.get(f"/api/v1/retro/state/{token}")
    assert anon.json()["my_votes"] == []


def test_manager_groups_cards_and_participant_votes_for_group(client):
    create = client.post("/api/v1/cms/retros", json={
        "title": "grouped",
        "config": {"sections": [{"section_id": "s", "title": "S"}], "votes_per_person": 2, "default_section_seconds": 0},
    })
    retro_id = create.json()["id"]
    token = client.post(f"/api/v1/cms/retros/{retro_id}/invite").json()["token"]
    pid = client.post("/api/v1/retro/join", json={
        "token": token, "name": "group.user@example.com", "role": "backend",
    }).json()["participant_id"]
    client.post(f"/api/v1/cms/retros/{retro_id}/open-section", json={"section_id": "s"})
    card_ids = []
    for text in ("slow release", "staging flakes"):
        res = client.post("/api/v1/retro/card", json={
            "token": token, "participant_id": pid, "section_id": "s", "text": text,
        })
        card_ids.append(res.json()["cards"][-1]["card_id"])

    group = client.post(f"/api/v1/cms/retros/{retro_id}/groups", json={
        "title": "Release pain",
        "card_ids": card_ids,
    })
    assert group.status_code == 200
    group_id = group.json()["groups"][0]["group_id"]
    assert group.json()["cards"][0]["group_id"] == group_id

    client.post(f"/api/v1/cms/retros/{retro_id}/phase", json={"target": "voting"})
    card_vote = client.post("/api/v1/retro/vote", json={
        "token": token, "participant_id": pid, "card_id": card_ids[0],
    })
    assert card_vote.status_code == 409

    group_vote = client.post("/api/v1/retro/vote", json={
        "token": token,
        "participant_id": pid,
        "target_type": "group",
        "target_id": group_id,
    })
    assert group_vote.status_code == 200
    assert group_vote.json()["groups"][0]["vote_count"] == 1
    assert group_vote.json()["my_votes"] == [group_id]

    renamed = client.patch(f"/api/v1/cms/retros/{retro_id}/groups/{group_id}", json={"title": "Release flow"})
    assert renamed.status_code == 200
    assert renamed.json()["groups"][0]["title"] == "Release flow"

    ungrouped = client.delete(f"/api/v1/cms/retros/{retro_id}/groups/{group_id}")
    assert ungrouped.status_code == 200
    assert ungrouped.json()["groups"] == []
    assert all(card.get("group_id") is None for card in ungrouped.json()["cards"])


def test_websocket_sends_initial_state(client):
    create = client.post("/api/v1/cms/retros", json={"title": "ws", "config": {"sections": []}})
    retro_id = create.json()["id"]
    token = client.post(f"/api/v1/cms/retros/{retro_id}/invite").json()["token"]

    with client.websocket_connect(f"/api/v1/retro-ws/{token}") as ws:
        message = ws.receive_json()
        assert message["type"] == "retro_state"
        assert message["state"]["retro_id"] == retro_id


def test_websocket_rejects_unknown_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/retro-ws/nope") as ws:
            ws.receive_json()
