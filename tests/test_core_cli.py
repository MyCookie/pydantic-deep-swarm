"""Core entrypoints remain usable without deployment assets."""

import json
import pytest

from click.testing import CliRunner

from agent_team.cli import build_swarm_manager, cli
from agent_team.control.manager import SwarmStatus


def test_discovery_uses_yaml_endpoint(monkeypatch, tmp_path):
    from agent_team.cli import build_model_discovery
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  worker:\n    model: yaml-model\n    base_url: http://yaml/v1\n")
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(config))
    monkeypatch.setenv("LLM_BASE_URL", "http://ignored/v1")
    discovery = build_model_discovery()
    assert discovery.base_url == "http://yaml/v1"


def test_discovery_rejects_mixed_role_endpoints(monkeypatch, tmp_path):
    from agent_team.cli import build_model_discovery
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  principal:\n    base_url: http://one/v1\n  worker:\n    base_url: http://two/v1\n")
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(config))
    with pytest.raises(ValueError, match="single model endpoint"):
        build_model_discovery()


def test_s6_manager_has_effective_yaml_models(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "s6")
    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "core.yaml"))
    (tmp_path / "core.yaml").write_text("models:\n  worker:\n    model: yaml-model\n")
    assert build_swarm_manager().effective_models == {"worker": "yaml-model"}


@pytest.mark.parametrize("authority", ["live", "yaml", "environment", "role"])
def test_s6_effective_models_refresh_preserves_authority(monkeypatch, tmp_path, authority):
    source = tmp_path / "config.py"
    source.write_text('model = os.getenv("LLM_MODEL", "old-model")\n')
    live = tmp_path / "live-run"
    live.write_text('export LLM_MODEL="old-model"\n')
    monkeypatch.setenv("AGENT_TEAM_LIVE_RUNFILE", str(live))
    config = tmp_path / "core.yaml"
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(config))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_SOURCE", str(source))
    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "s6")
    for name in ("LLM_MODEL", "PRINCIPAL_MODEL", "MANAGER_MODEL", "WORKER_MODEL", "CURATOR_MODEL"):
        monkeypatch.delenv(name, raising=False)
    if authority == "yaml":
        config.write_text("models:\n  worker:\n    model: authoritative-model\n")
    elif authority == "environment":
        monkeypatch.setenv("LLM_MODEL", "authoritative-model")
    elif authority == "role":
        monkeypatch.setenv("WORKER_MODEL", "authoritative-model")
    manager = build_swarm_manager()
    source.write_text('model = os.getenv("LLM_MODEL", "new-model")\n')
    live.write_text('export LLM_MODEL="new-model"\n')
    refreshed = manager.effective_models_provider()
    assert refreshed["worker"] == ("new-model" if authority == "live" else "authoritative-model")
    if authority == "live":
        assert manager.effective_models["worker"] == "old-model"


@pytest.mark.parametrize("yaml_selection", [None, "auto", "null", "empty"])
def test_live_pin_with_multiple_models_preserves_config_authority(monkeypatch, tmp_path, yaml_selection):
    source = tmp_path / "config.py"
    source.write_text('model = os.getenv("LLM_MODEL", "auto")\n')
    source_run = tmp_path / "source-run"
    source_run.write_text('export LLM_MODEL="auto"\nexec service\n')
    live = tmp_path / "live-run"
    live.write_text('export LLM_MODEL="selected-model"\nexec service\n')
    config = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(config))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_SOURCE", str(source))
    monkeypatch.setenv("AGENT_TEAM_SOURCE_RUNFILE", str(source_run))
    monkeypatch.setenv("AGENT_TEAM_LIVE_RUNFILE", str(live))
    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "s6")
    for name in ("LLM_MODEL", "PRINCIPAL_MODEL", "MANAGER_MODEL", "WORKER_MODEL", "CURATOR_MODEL"):
        monkeypatch.delenv(name, raising=False)
    if yaml_selection is not None:
        value = {"auto": "auto", "null": "null", "empty": '\"\"'}[yaml_selection]
        config.write_text("models:\n" + "".join(f"  {role}:\n    model: {value}\n" for role in ("principal", "manager", "worker", "curator")))

    class Discovery:
        base_url = "http://models/v1"
        def list_models(self):
            return ["selected-model", "other-model"]
        @staticmethod
        def require_single(models):
            from agent_team.control.discovery import OpenAIModelDiscovery
            return OpenAIModelDiscovery.require_single(models)

    class API:
        def __init__(self, *args, **kwargs):
            pass
        def models(self):
            return {role: {"model": "selected-model", "base_url": "http://models/v1"} for role in ("principal", "manager", "worker", "curator")}
        def health(self):
            return {"status": "ok"}
        def ready(self):
            return {"status": "ok", "ready": True}

    class Service:
        def __init__(self, **kwargs):
            pass
        def preflight(self):
            pass
        def restart(self):
            pass

    monkeypatch.setattr("agent_team.cli.build_model_discovery", Discovery)
    monkeypatch.setattr("agent_team.cli.AgentTeamAPIClient", API)
    monkeypatch.setattr("agent_team.cli.S6ServiceController", Service)
    manager = build_swarm_manager()
    if yaml_selection == "auto":
        from agent_team.control.discovery import ModelDiscoveryError
        with pytest.raises(ModelDiscoveryError, match="multiple models"):
            manager.inspect()
    else:
        assert manager.inspect().synchronized
        assert manager.reconcile("selected-model").verified
        assert '"auto"' in source.read_text()


def test_manager_resolves_empty_role_model_like_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "none")
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "core.yaml"))
    monkeypatch.setenv("LLM_MODEL", "fallback-model")
    (tmp_path / "core.yaml").write_text("models:\n  worker: {}\n")
    assert build_swarm_manager().effective_models == {"worker": "fallback-model"}


def test_init_preserves_loaded_defaults_and_external_state(monkeypatch, tmp_path):
    from agent_team.config import Config
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    state = tmp_path / "external state"
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR", str(state))
    for name in ("LLM_BASE_URL", "LLM_MODEL", "PRINCIPAL_MODEL", "MANAGER_MODEL", "WORKER_MODEL", "CURATOR_MODEL", "PRINCIPAL_BASE_URL", "MANAGER_BASE_URL", "WORKER_BASE_URL", "CURATOR_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    before = Config.from_env()
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    loaded = Config.from_file(state / "config/config.yaml")
    assert loaded == before


def test_explicit_none_ignores_inherited_s6_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "none")
    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_SOURCE", str(tmp_path / "missing.py"))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "core.yaml"))
    (tmp_path / "core.yaml").write_text("models:\n  worker:\n    model: yaml-model\n")
    manager = build_swarm_manager()
    assert manager.service is None
    assert manager.config is None
    assert manager.effective_models == {"worker": "yaml-model"}


def test_default_manager_is_unsupervised(monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_SERVICE_DIR", raising=False)
    monkeypatch.delenv("AGENT_TEAM_SUPERVISOR", raising=False)
    assert build_swarm_manager().service is None


def test_detect_does_not_build_supervisor(monkeypatch):
    class Discovery:
        def detect_model(self):
            return "test-model"

    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "invalid")
    monkeypatch.setattr("agent_team.cli.build_model_discovery", Discovery)
    result = CliRunner().invoke(cli, ["swarm", "detect"])
    assert result.exit_code == 0
    assert result.output.strip() == "test-model"


def test_doctor_forces_core_inspection(monkeypatch):
    calls = []

    class Manager:
        def inspect(self):
            return SwarmStatus(expected_model="test-model", health_ok=True, ready_ok=True)

    def build(*, supervisor):
        calls.append(supervisor)
        return Manager()

    monkeypatch.setattr("agent_team.cli.build_swarm_manager", build)
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["supervisor_state"] == "not_configured"
    assert calls == ["none"]


def test_serve_uses_foreground_runtime_and_explicit_config(monkeypatch, tmp_path):
    calls = []
    config = tmp_path / "config.yaml"
    config.write_text("{}")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(
        cli, ["serve", "--host", "127.0.0.1", "--port", "8090", "--config", str(config)]
    )
    assert result.exit_code == 0
    assert calls == [(("agent_team.app:app",), {"host": "127.0.0.1", "port": 8090})]


def test_invalid_supervisor_fails_clearly(monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_SUPERVISOR", "unknown")
    result = CliRunner().invoke(cli, ["swarm", "status"])
    assert result.exit_code == 1
    assert "must be none or s6" in result.output
