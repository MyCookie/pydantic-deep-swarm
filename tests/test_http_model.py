"""Tests for the OpenAI-compatible Agent Team model wrapper."""

import asyncio

from pydantic import BaseModel

from agent_team.models import http_model
from agent_team.models.http_model import SimpleChatModel


class _Answer(BaseModel):
    value: str


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": '{"value":"ok"}'}}]}


class _Client:
    def __init__(self):
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, endpoint, headers, json):
        self.headers = headers
        self.payload = json
        return _Response()


def test_structured_request_merges_schema_into_existing_system_message(monkeypatch):
    client = _Client()
    monkeypatch.setattr(http_model.httpx, "AsyncClient", lambda **_: client)
    model = SimpleChatModel("http://model/v1", "test-model")

    result = asyncio.run(
        model.run_structured(
            [
                {"role": "system", "content": "Use the Principal policy."},
                {"role": "user", "content": "Return an answer."},
            ],
            _Answer,
        )
    )

    assert result.value == "ok"
    messages = client.payload["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "matching this schema" in messages[0]["content"]
    assert "Use the Principal policy." in messages[0]["content"]


def test_bearer_header_is_opt_in(monkeypatch):
    client = _Client()
    monkeypatch.setattr(http_model.httpx, "AsyncClient", lambda **_: client)

    asyncio.run(SimpleChatModel("http://model/v1", "test-model").run([]))
    assert "Authorization" not in client.headers

    asyncio.run(SimpleChatModel("http://model/v1", "test-model", api_key="test-key").run([]))
    assert client.headers["Authorization"] == "Bearer test-key"
