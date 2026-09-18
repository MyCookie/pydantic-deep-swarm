"""HTTP authentication boundary tests for the Agent Team service."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_http_authentication_is_disabled_when_token_is_unset(monkeypatch):
    import agent_team.app as app_module

    monkeypatch.delenv("AGENT_TEAM_API_TOKEN", raising=False)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_configured_http_authentication_protects_api_and_generated_routes(monkeypatch):
    import agent_team.app as app_module

    expected_token = "control-token-for-tests"
    wrong_token = "wrong-control-token"
    monkeypatch.setenv("AGENT_TEAM_API_TOKEN", expected_token)
    transport = httpx.ASGITransport(app=app_module.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
        for path in ("/health", "/openapi.json", "/docs", "/redoc"):
            missing = await client.get(path)
            wrong = await client.get(
                path,
                headers={"Authorization": f"Bearer {wrong_token}"},
            )
            authorized = await client.get(
                path,
                headers={"Authorization": f"Bearer {expected_token}"},
            )

            assert missing.status_code == 401
            assert wrong.status_code == 401
            assert authorized.status_code == 200
            assert missing.headers["www-authenticate"] == "Bearer"
            assert wrong.headers["www-authenticate"] == "Bearer"
            assert expected_token not in missing.text
            assert wrong_token not in wrong.text


@pytest.mark.asyncio
async def test_http_authentication_rejects_before_runtime_initialization(monkeypatch):
    import agent_team.app as app_module

    expected_token = "control-token-for-tests"
    monkeypatch.setenv("AGENT_TEAM_API_TOKEN", expected_token)
    called = False

    def forbidden_initialize():
        nonlocal called
        called = True
        raise AssertionError("runtime initialization must not run for unauthorized requests")

    monkeypatch.setattr(app_module, "_initialize_runtime", forbidden_initialize)
    transport = httpx.ASGITransport(app=app_module.app)
    routes = (
        ("POST", "/sessions"),
        ("POST", "/sessions/test/messages"),
        ("POST", "/sessions/test/delegations"),
        ("POST", "/sessions/test/reset"),
        ("POST", "/sessions/test/cancel"),
        ("GET", "/sessions/test"),
        ("GET", "/sessions/test/report"),
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/models"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
        responses = [
            await client.request(method, path, json={} if method == "POST" else None)
            for method, path in routes
        ]

    assert all(response.status_code == 401 for response in responses)
    assert called is False
