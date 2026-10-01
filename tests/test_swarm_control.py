"""TDD gates for the programmatic swarm control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest

from agent_team.control.api import AgentTeamAPIClient
from agent_team.control.configuration import (
    ConfigReconcileError,
    SwarmConfigReconciler,
    extract_config_model,
    extract_runfile_model,
)
from agent_team.control.discovery import ModelDiscoveryError, VLLMModelDiscovery
from agent_team.control.manager import SwarmManager, SwarmStatus
from agent_team.control.service import S6ServiceController, ServiceControlError


MODEL = "nvidia/Qwen3.8-27B-NVFP4"
OLD_MODEL = "old/model"


def mock_transport(state: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": state["vllm_model"]}]},
                request=request,
            )
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/ready":
            return httpx.Response(200, json={"status": "ok", "ready": True}, request=request)
        if request.url.path == "/models":
            return httpx.Response(
                200,
                json={
                    "models": {
                        role: {"model": state["api_model"], "base_url": "http://vllm/v1"}
                        for role in ("principal", "manager", "worker", "curator")
                    }
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def test_model_discovery_returns_the_single_vllm_model():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": MODEL}]},
            request=request,
        )
    ))

    discovery = VLLMModelDiscovery("http://vllm/v1", client=client)

    assert discovery.detect_model() == MODEL


def test_model_discovery_sends_configured_bearer_without_crossing_control_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": MODEL}]}, request=request)

    discovery = VLLMModelDiscovery(
        "http://vllm/v1",
        api_key="model-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert discovery.detect_model() == MODEL
    assert requests[0].headers["authorization"] == "Bearer model-token"
    assert "control-token" not in requests[0].headers["authorization"]


def test_model_discovery_omits_authorization_when_key_is_empty():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": MODEL}]}, request=request)

    discovery = VLLMModelDiscovery(
        "http://vllm/v1",
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


def test_model_discovery_rejects_ambiguous_vllm_endpoint():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "one"}, {"id": "two"}]},
            request=request,
        )
    ))

    with pytest.raises(ModelDiscoveryError, match="multiple models"):
        VLLMModelDiscovery("http://vllm/v1", client=client).detect_model()


def test_runfile_model_extraction_and_reconciliation():
    runfile = '''#!/bin/sh
export LLM_MODEL="${LLM_MODEL:-old/model}"
export PRINCIPAL_MODEL="${PRINCIPAL_MODEL:-$LLM_MODEL}"
exec server
'''

    assert extract_runfile_model(runfile) == OLD_MODEL

    reconciler = SwarmConfigReconciler(
        source_config_path=Path("/unused/config.py"),
        source_runfile_path=Path("/unused/source-run"),
        live_runfile_path=Path("/unused/live-run"),
    )
    updated = reconciler.render_runfile(runfile, MODEL)

    assert extract_runfile_model(updated) == MODEL
    assert 'PRINCIPAL_MODEL="${PRINCIPAL_MODEL:-$LLM_MODEL}"' in updated
    assert OLD_MODEL not in updated


def test_reconcile_updates_all_layers_restarts_and_verifies(tmp_path: Path):
    source_config = tmp_path / "config.py"
    source_runfile = tmp_path / "source-run"
    live_runfile = tmp_path / "live-run"
    source_config.write_text(
        'model_name = os.getenv(f"{env_prefix}LLM_MODEL", "old/model")\n'
    )
    old_runfile = 'export LLM_MODEL="${LLM_MODEL:-old/model}"\n'
    source_runfile.write_text(old_runfile)
    live_runfile.write_text(old_runfile)

    state = {"vllm_model": MODEL, "api_model": OLD_MODEL}
    transport = mock_transport(state)
    discovery = VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport))
    api = AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport))

    class FakeService:
        def __init__(self):
            self.restart_count = 0

        def restart(self):
            self.restart_count += 1
            state["api_model"] = MODEL

    service = FakeService()
    reconciler = SwarmConfigReconciler(source_config, source_runfile, live_runfile)
    manager = SwarmManager(discovery, api, reconciler, service)

    result = manager.reconcile()

    assert result.model == MODEL
    assert result.restarted is True
    assert result.verified is True
    assert service.restart_count == 1
    assert extract_runfile_model(source_runfile.read_text()) == MODEL
    assert extract_runfile_model(live_runfile.read_text()) == MODEL
    assert MODEL in source_config.read_text()


def test_core_inspection_needs_no_supervisor_or_managed_files():
    state = {"vllm_model": MODEL, "api_model": MODEL}
    transport = mock_transport(state)
    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
        effective_models={role: MODEL for role in ("principal", "manager", "worker", "curator")},
    )
    status = manager.inspect()
    assert status.synchronized
    assert status.supervisor_state == "not_configured"
    manager.effective_models["worker"] = OLD_MODEL
    assert "effective role worker" in manager.inspect().drift[0]
    with pytest.raises(ServiceControlError, match="optional s6"):
        manager.reconcile(dry_run=True)


def test_unavailable_supervisor_prevents_mutation_but_allows_plan(tmp_path):
    paths = [tmp_path / name for name in ("config.py", "source", "live")]
    paths[0].write_text('model = os.getenv("LLM_MODEL", "old/model")\n')
    for path in paths[1:]:
        path.write_text('export LLM_MODEL="${LLM_MODEL:-old/model}"\n')
    originals = [path.read_text() for path in paths]
    state = {"vllm_model": MODEL, "api_model": OLD_MODEL}
    transport = mock_transport(state)

    class Unavailable:
        def preflight(self):
            raise ServiceControlError("supervisor not running")

        def restart(self):
            pytest.fail("restart must not run")

    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
        SwarmConfigReconciler(*paths), Unavailable(),
    )
    plan = manager.reconcile(dry_run=True)
    assert len(plan.changed_files) == 3
    assert plan.status.supervisor_state == "unavailable"
    with pytest.raises(ServiceControlError, match="not running"):
        manager.reconcile()
    assert [path.read_text() for path in paths] == originals


def test_no_restart_reports_pending_even_when_api_already_matches(tmp_path):
    paths = [tmp_path / name for name in ("config.py", "source", "live")]
    paths[0].write_text('model = os.getenv("LLM_MODEL", "old/model")\n')
    for path in paths[1:]:
        path.write_text('export LLM_MODEL="${LLM_MODEL:-old/model}"\n')
    transport = mock_transport({"vllm_model": MODEL, "api_model": MODEL})

    class Available:
        def preflight(self):
            raise ServiceControlError("supervisor not running")

        def restart(self):
            pytest.fail("restart disabled")

    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
        SwarmConfigReconciler(*paths), Available(),
    )
    result = manager.reconcile(restart=False)
    assert result.pending_restart
    assert not result.verified
    assert result.status.supervisor_state == "unavailable"
    assert len(result.changed_files) == 3


def test_core_inspection_reports_missing_effective_roles():
    transport = mock_transport({"vllm_model": MODEL, "api_model": MODEL})
    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
    )
    assert not manager.inspect().synchronized
    manager.effective_models = {"principal": MODEL}
    status = manager.inspect()
    assert not status.synchronized
    assert any("effective role worker" in item for item in status.drift)


def test_adapter_inspection_compares_effective_role_overrides(tmp_path):
    paths = [tmp_path / name for name in ("config.py", "source", "live")]
    paths[0].write_text(f'model = os.getenv("LLM_MODEL", "{MODEL}")\n')
    for path in paths[1:]:
        path.write_text(f'export LLM_MODEL="${{LLM_MODEL:-{MODEL}}}"\n')
    transport = mock_transport({"vllm_model": MODEL, "api_model": MODEL})
    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
        SwarmConfigReconciler(*paths),
        effective_models={role: OLD_MODEL if role == "worker" else MODEL for role in ("principal", "manager", "worker", "curator")},
    )
    status = manager.inspect()
    assert not status.synchronized
    assert any("effective role worker" in item for item in status.drift)


@pytest.mark.parametrize("override", [None, OLD_MODEL])
def test_reconcile_refreshes_effective_models_without_hiding_overrides(tmp_path, override):
    paths = [tmp_path / name for name in ("config.py", "source", "live")]
    paths[0].write_text('model = os.getenv("LLM_MODEL", "old/model")\n')
    for path in paths[1:]:
        path.write_text('export LLM_MODEL="${LLM_MODEL:-old/model}"\n')
    state = {"vllm_model": MODEL, "api_model": OLD_MODEL}
    transport = mock_transport(state)

    class Available:
        def restart(self):
            state["api_model"] = MODEL

    def effective_models():
        model = override or extract_config_model(paths[0].read_text())
        return {role: model for role in ("principal", "manager", "worker", "curator")}

    manager = SwarmManager(
        VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport)),
        AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport)),
        SwarmConfigReconciler(*paths), Available(),
        effective_models_provider=effective_models,
        verification_attempts=1,
    )
    result = manager.reconcile()
    assert result.restarted
    assert result.verified is (override is None)
    assert result.status.effective_models["worker"] == (override or MODEL)


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


def test_reconcile_preflights_all_targets_before_writing(tmp_path: Path, monkeypatch):
    source_config = tmp_path / "config.py"
    source_runfile = tmp_path / "source-run"
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    live_runfile = live_dir / "run"
    source_config.write_text(
        'model_name = os.getenv(f"{env_prefix}LLM_MODEL", "old/model")\n'
    )
    old_runfile = 'export LLM_MODEL="${LLM_MODEL:-old/model}"\n'
    source_runfile.write_text(old_runfile)
    live_runfile.write_text(old_runfile)

    real_access = os.access

    def access(path, mode):
        if Path(path) == live_dir:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", access)
    reconciler = SwarmConfigReconciler(source_config, source_runfile, live_runfile)

    with pytest.raises(ConfigReconcileError, match="not writable"):
        reconciler.apply(MODEL)

    assert "old/model" in source_config.read_text()
    assert "old/model" in source_runfile.read_text()
    assert "old/model" in live_runfile.read_text()


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


def test_reconcile_waits_for_eventual_api_convergence(tmp_path: Path):
    source_config = tmp_path / "config.py"
    source_runfile = tmp_path / "source-run"
    live_runfile = tmp_path / "live-run"
    source_config.write_text(
        'model_name = os.getenv(f"{env_prefix}LLM_MODEL", "old/model")\n'
    )
    old_runfile = 'export LLM_MODEL="${LLM_MODEL:-old/model}"\n'
    source_runfile.write_text(old_runfile)
    live_runfile.write_text(old_runfile)

    state = {"vllm_model": MODEL, "api_model": OLD_MODEL}
    transport = mock_transport(state)
    discovery = VLLMModelDiscovery("http://vllm/v1", client=httpx.Client(transport=transport))
    api = AgentTeamAPIClient("http://agent", client=httpx.Client(transport=transport))

    class FakeService:
        def restart(self):
            pass

    manager = SwarmManager(
        discovery,
        api,
        SwarmConfigReconciler(source_config, source_runfile, live_runfile),
        FakeService(),
        verification_interval=0,
    )
    observations = iter([
        SwarmStatus(expected_model=MODEL, drift=["API warming up"]),
        SwarmStatus(expected_model=MODEL),
    ])
    manager.inspect = lambda **kwargs: next(observations)

    result = manager.reconcile()

    assert result.verified is True
