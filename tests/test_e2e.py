"""Opt-in end-to-end tests against real model and API endpoints."""

from __future__ import annotations

import os

import httpx
import pytest

from agent_team.config import Config
from agent_team.engine import AgentTeamEngine


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AGENT_TEAM_RUN_LIVE_E2E") != "1",
        reason="set AGENT_TEAM_RUN_LIVE_E2E=1 to exercise live endpoints",
    ),
]


async def test_end_to_end(tmp_path):
    """The live model path must produce a non-failed report."""
    config = Config.from_env()
    config.runtime.state_dir = tmp_path / "state"
    config.runtime.workspace_dir = tmp_path / "workspace"
    engine = AgentTeamEngine(config)
    try:
        report = await engine.run(
            "Create a Python function to calculate fibonacci numbers",
            "live-e2e-session",
        )
    finally:
        await engine.close()

    assert report.status not in {"failed", "blocked"}, report.model_dump_json(indent=2)
    assert report.summary.strip()
    assert report.requirement_results


async def test_api_endpoint():
    """The live HTTP service must accept and complete a real session turn."""
    base_url = os.getenv("AGENT_TEAM_URL", "http://localhost:8080").rstrip("/")
    api_token = os.getenv("AGENT_TEAM_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        health = await client.get(f"{base_url}/health")
        assert health.status_code == 200, health.text
        assert health.json().get("status") == "ok"

        created = await client.post(f"{base_url}/sessions", json={"prompt": "Test prompt"})
        assert created.status_code == 200, created.text
        session_id = created.json().get("session_id")
        assert session_id

        response = await client.post(
            f"{base_url}/sessions/{session_id}/messages",
            json={"message": "Create a fibonacci function"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload.get("status") not in {"failed", "blocked"}, payload
        assert payload.get("response")
