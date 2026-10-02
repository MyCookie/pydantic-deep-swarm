"""TDD gates for the programmatic swarm control plane."""

from __future__ import annotations

from config_doctor_evidence import config_doctor_evidence

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import httpx
import pytest

from agent_team.control.api import AgentTeamAPIClient
from agent_team.control.configuration import (
    ConfigReconcileError,
    SwarmConfigReconciler,
    extract_config_model,
    extract_runfile_model,
)
from agent_team.control.discovery import ModelDiscoveryError, OpenAIModelDiscovery
from agent_team.control.manager import SwarmManager, SwarmStatus
from agent_team.control.service import S6ServiceController, ServiceControlError


MODEL = "vendor/example-model"
OLD_MODEL = "old/model"


def mock_transport(state: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            advertised = state.get("endpoint_models") or [state["endpoint_model"]]
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": model} for model in advertised]},
                request=request,
            )
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/ready":
            return httpx.Response(200, json={"status": "ok", "ready": True}, request=request)
        if request.url.path == "/models":
            role_models = state.get("api_models", {})
            return httpx.Response(
                200,
                json={
                    "models": {
                        role: {
                            "model": role_models.get(role, state["api_model"]),
                            "base_url": "http://model/v1",
                        }
                        for role in ("principal", "manager", "worker", "curator")
                    }
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def test_model_discovery_returns_the_single_endpoint_model():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": MODEL}]},
            request=request,
        )
    ))

    discovery = OpenAIModelDiscovery("http://model/v1", client=client)

    assert discovery.detect_model() == MODEL


def test_model_discovery_sends_neither_inference_nor_control_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": MODEL}]}, request=request)

    discovery = OpenAIModelDiscovery(
        "http://model/v1",
        api_key="model-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert discovery.detect_model() == MODEL
    assert "authorization" not in requests[0].headers
    assert "control-token" not in str(requests[0].headers)


def test_model_discovery_omits_authorization_when_key_is_empty():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": MODEL}]}, request=request)

    discovery = OpenAIModelDiscovery(
        "http://model/v1",
        api_key="",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert discovery.detect_model() == MODEL
    assert "authorization" not in requests[0].headers


def test_agent_team_api_client_sends_only_the_configured_control_bearer():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    api = AgentTeamAPIClient(
        "http://agent",
        api_token="control-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert api.health() == {"status": "ok"}
    assert requests[0].headers["authorization"] == "Bearer control-token"
    assert "model-token" not in requests[0].headers["authorization"]


def test_agent_team_api_client_omits_authorization_when_token_is_empty():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    api = AgentTeamAPIClient(
        "http://agent",
        api_token="",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert api.health() == {"status": "ok"}
    assert "authorization" not in requests[0].headers


def test_model_discovery_rejects_ambiguous_endpoint():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "one"}, {"id": "two"}]},
            request=request,
        )
    ))

    with pytest.raises(ModelDiscoveryError, match="multiple models"):
        OpenAIModelDiscovery("http://model/v1", client=client).detect_model()
























def test_s6_explicit_invalid_command_does_not_fall_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_team.control.service.shutil.which", lambda name: str(tmp_path / name))
    controller = S6ServiceController(service_dir=tmp_path, command_candidates=(tmp_path / "missing",))
    with pytest.raises(ServiceControlError, match="configured s6-svc"):
        controller.preflight()


def test_s6_preflight_rejects_down_service(tmp_path):
    command = tmp_path / "tool"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    controller = S6ServiceController(
        service_dir=tmp_path,
        command_candidates=(command,), status_command_candidates=(command,),
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "down (exitcode 0) 1 seconds", ""),
    )
    with pytest.raises(ServiceControlError, match="not running"):
        controller.preflight()


def test_s6_controller_requires_injected_service_directory(monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_SERVICE_DIR", raising=False)

    with pytest.raises(ServiceControlError, match="AGENT_TEAM_SERVICE_DIR"):
        S6ServiceController()


def test_s6_controller_uses_absolute_command_path(tmp_path: Path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    command = tmp_path / "s6-svc"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    status_command = tmp_path / "s6-svstat"
    status_command.write_text("#!/bin/sh\n")
    status_command.chmod(0o755)
    calls: list[list[str]] = []
    timeouts: list[float] = []

    def runner(args, **kwargs):
        calls.append(args)
        timeouts.append(kwargs["timeout"])
        return type("Completed", (), {"returncode": 0, "stdout": "up (pid 123) 4 seconds", "stderr": ""})()

    controller = S6ServiceController(
        service_dir=service_dir,
        command_candidates=(command,),
        status_command_candidates=(status_command,),
        runner=runner,
    )

    controller.restart()

    assert calls == [
        [str(status_command), str(service_dir)],
        [str(command), "-r", "-wr", "-T", "10000", str(service_dir)],
    ]
    assert timeouts == [5, 15]




def test_s6_controller_wraps_restart_failures(tmp_path: Path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    command = tmp_path / "s6-svc"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)

    status_command = tmp_path / "s6-svstat"
    status_command.write_text("#!/bin/sh\n")
    status_command.chmod(0o755)

    def runner(args, **kwargs):
        raise subprocess.CalledProcessError(111, args, stderr="permission denied")

    controller = S6ServiceController(
        service_dir=service_dir,
        command_candidates=(command,),
        status_command_candidates=(status_command,),
        runner=runner,
    )

    with pytest.raises(Exception, match="permission denied"):
        controller.restart()
