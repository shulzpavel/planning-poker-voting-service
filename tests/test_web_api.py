"""Tests for browser voting API helpers."""

import json
import os
import subprocess
import sys
from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.session import Session
from services.voting_service.web_api import _stable_user_id, web_router


class FakeRedis:
    async def get(self, key: str):
        if key == "web:test-token":
            return json.dumps({"chat_id": 123, "topic_id": None})
        return None


class FakeRepo:
    async def get_session_async(self, chat_id: int, topic_id: Optional[int]) -> Session:
        return Session(chat_id=chat_id, topic_id=topic_id)


def test_stable_user_id_is_negative_and_repeatable() -> None:
    assert _stable_user_id("participant-1") == _stable_user_id("participant-1")
    assert _stable_user_id("participant-1") < 0


def test_stable_user_id_does_not_depend_on_python_hash_seed() -> None:
    script = "from services.voting_service.web_api import _stable_user_id; print(_stable_user_id('participant-1'))"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath("backend")

    values = []
    for seed in ("1", "2"):
        run_env = {**env, "PYTHONHASHSEED": seed}
        output = subprocess.check_output([sys.executable, "-c", script], env=run_env, text=True)
        values.append(output.strip())

    assert values[0] == values[1]


def test_websocket_sends_initial_session_state() -> None:
    app = FastAPI()
    app.state.web_redis = FakeRedis()
    app.state.repository = FakeRepo()
    app.include_router(web_router, prefix="/api/v1")

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/test-token") as websocket:
            message = websocket.receive_json()

    assert message["type"] == "session_state"
    assert message["state"]["phase"] == "waiting"
