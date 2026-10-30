import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.domain.intake_analysis import build_analysis_facts
from app.domain.intake_dashboard import build_intake_snapshot
from services.voting_service.intake_llm import generate_intake_analysis

class Response:
    def __init__(self, payload, status=200): self.payload, self.status = payload, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def json(self): return self.payload

@pytest.mark.asyncio
async def test_server_generation_and_review_resolve_evidence_without_exposing_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    snapshot = build_intake_snapshot([], now=datetime(2026,10,30,tzinfo=timezone.utc), days=30)
    facts = build_analysis_facts(snapshot)
    charts = {kind: {"headline": "Вывод", "explanation": "Данных пока нет", "focus": "Нет завершённых разборов", "fact_ids": [value["facts"][0]["id"]], "keys": []} for kind, value in facts.items()}
    payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(charts)}]}
    session = MagicMock()
    session.post.side_effect = [Response(payload), Response(payload)]
    result = await generate_intake_analysis(session, snapshot, "test-model")
    assert session.post.call_count == 2
    assert result["snapshot_at"] == snapshot["generated_at"]
    assert result["charts"]["flow"]["evidence"] == [facts["flow"]["facts"][0]["text"]]
    assert "test-key" not in json.dumps(result)

@pytest.mark.asyncio
async def test_provider_error_has_no_template_or_raw_provider_body(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    snapshot = build_intake_snapshot([], now=datetime(2026,10,30,tzinfo=timezone.utc), days=30)
    session = MagicMock()
    session.post.return_value = Response({"error": "private provider response"}, status=401)
    with pytest.raises(RuntimeError, match="^intake_ai_provider_http_401$"):
        await generate_intake_analysis(session, snapshot, "test-model")
