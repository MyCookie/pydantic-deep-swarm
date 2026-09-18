"""Regression coverage for HTTP and bootstrap readiness semantics."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from agent_team.bootstrap import Bootstrapper
from agent_team.config import Config, RuntimeConfig


@pytest.mark.asyncio
async def test_health_is_liveness_only_and_ready_fails_closed_until_initialized(
    tmp_path,
    monkeypatch,
):
    import agent_team.app as app_module

    old_state = (
        app_module.config,
        app_module.engine,
        app_module.session_store,
        app_module.runtime_lease,
    )
    app_module.config = None
    app_module.engine = None
    app_module.session_store = None
    app_module.runtime_lease = None
    transport = httpx.ASGITransport(app=app_module.app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            not_ready = await client.get("/ready")
            assert not_ready.status_code == 503
            assert not_ready.json() == {
                "status": "not_ready",
                "ready": False,
                "reason": "config_not_loaded",
            }

            missing_state = tmp_path / "missing-state"
            app_module.config = Config(runtime=RuntimeConfig(state_dir=missing_state))
            app_module.session_store = object()
            missing_engine = await client.get("/ready")
            assert missing_engine.status_code == 503
            assert missing_engine.json()["reason"] == "engine_not_initialized"

            app_module.engine = object()
            app_module.session_store = None
            missing_store = await client.get("/ready")
            assert missing_store.status_code == 503
            assert missing_store.json()["reason"] == "engine_not_initialized"

            app_module.session_store = object()
            missing_lease = await client.get("/ready")
            assert missing_lease.status_code == 503
            assert missing_lease.json()["reason"] == "runtime_not_owned"

            app_module.runtime_lease = object()
            inaccessible = await client.get("/ready")
            assert inaccessible.status_code == 503
            assert inaccessible.json()["reason"] == "state_dir_not_accessible"

            missing_state.mkdir()
            monkeypatch.setattr(app_module.os, "access", lambda _path, _mode: False)
            unwritable = await client.get("/ready")
            assert unwritable.status_code == 503
            assert unwritable.json()["reason"] == "state_dir_not_writable"

            monkeypatch.setattr(app_module.os, "access", lambda _path, _mode: True)
            ready = await client.get("/ready")
            assert ready.status_code == 200
            assert ready.json() == {"status": "ok", "ready": True}
    finally:
        (
            app_module.config,
            app_module.engine,
            app_module.session_store,
            app_module.runtime_lease,
        ) = old_state


def test_bootstrap_defaults_to_ready_endpoint_and_rejects_liveness_payload(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("AGENT_TEAM_HEALTH_URL", raising=False)
    requests: list[str] = []

    def liveness_only(url, _headers, _timeout):
        requests.append(url)
        return 200, json.dumps({"status": "ok"}).encode()

    bootstrapper = Bootstrapper(
        tmp_path,
        tmp_path / "missing-pi",
        python_executable=sys.executable,
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "state",
        service_dir=tmp_path / "service",
        health_url="http://agent-team.example/health",
        http_get=liveness_only,
    )
    checks = []

    assert bootstrapper.health_url == "http://agent-team.example/ready"
    assert bootstrapper._check_health(checks) is False
    assert requests == ["http://agent-team.example/ready"]
    assert checks[-1].status == "failed"

    default_bootstrapper = Bootstrapper(
        tmp_path,
        tmp_path / "missing-pi",
        python_executable=sys.executable,
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "default-state",
        service_dir=tmp_path / "service",
        http_get=liveness_only,
    )
    assert default_bootstrapper.health_url == "http://localhost:8080/ready"


def test_bootstrap_readiness_uses_control_token_not_model_token(tmp_path):
    requests = []

    def ready(url, headers, _timeout):
        requests.append((url, headers))
        return 200, json.dumps({"status": "ok", "ready": True}).encode()

    bootstrapper = Bootstrapper(
        tmp_path,
        tmp_path / "missing-pi",
        python_executable=sys.executable,
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "state",
        service_dir=tmp_path / "service",
        api_token="control-token",
        http_get=ready,
    )
    checks = []

    assert bootstrapper._check_health(checks) is True
    assert requests == [
        (
            "http://localhost:8080/ready",
            {"Authorization": "Bearer control-token"},
        )
    ]
