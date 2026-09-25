"""CLI gates for the swarm control plane."""

from click.testing import CliRunner

from agent_team.cli import build_swarm_manager, cli
from agent_team.control.manager import SwarmStatus, SwarmReconcileResult


MODEL = "vendor/example-model"


def test_default_swarm_manager_requires_injected_live_service_paths(monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_SERVICE_DIR", raising=False)
    monkeypatch.delenv("AGENT_TEAM_LIVE_RUNFILE", raising=False)

    try:
        build_swarm_manager()
    except ValueError as exc:
        assert "AGENT_TEAM_SERVICE_DIR" in str(exc)
    else:
        raise AssertionError("missing deployment paths must fail closed")


def test_default_swarm_manager_keeps_model_and_control_credentials_separate(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class Discovery:
        def __init__(self, base_url, *, api_key=""):
            captured["model"] = (base_url, api_key)

    class API:
        def __init__(self, base_url, *, api_token=None):
            captured["control"] = (base_url, api_token)

    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("LLM_BASE_URL", "http://model/v1")
    monkeypatch.setenv("LLM_API_KEY", "model-token")
    monkeypatch.setenv("AGENT_TEAM_API_URL", "http://agent-team")
    monkeypatch.setenv("AGENT_TEAM_API_TOKEN", "control-token")
    monkeypatch.setattr("agent_team.cli.OpenAIModelDiscovery", Discovery)
    monkeypatch.setattr("agent_team.cli.AgentTeamAPIClient", API)

    build_swarm_manager()

    assert captured == {
        "model": ("http://model/v1", "model-token"),
        "control": ("http://agent-team", "control-token"),
    }


class FakeDiscovery:
    def detect_model(self):
        return MODEL


class FakeManager:
    def __init__(self):
        self.discovery = FakeDiscovery()
        self.reconcile_calls = []

    def inspect(self):
        return SwarmStatus(
            expected_model=MODEL,
            advertised_models=[MODEL],
            config_model=MODEL,
            source_model=MODEL,
            live_model=MODEL,
            api_models={role: MODEL for role in ("principal", "manager", "worker", "curator")},
            health_ok=True,
            ready_ok=True,
        )

    def reconcile(self, model=None, **kwargs):
        self.reconcile_calls.append((model, kwargs))
        return SwarmReconcileResult(
            model=model or MODEL,
            changed_files=["config.py"],
            restarted=kwargs.get("restart", True),
            verified=kwargs.get("verify", True),
            status=self.inspect(),
        )


def test_swarm_detect_cli_returns_discovered_model(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr("agent_team.cli.build_swarm_manager", lambda: manager)

    result = CliRunner().invoke(cli, ["swarm", "detect"])

    assert result.exit_code == 0
    assert result.output.strip() == MODEL


def test_swarm_reconcile_cli_supports_dry_run_and_json(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr("agent_team.cli.build_swarm_manager", lambda: manager)

    result = CliRunner().invoke(
        cli,
        ["swarm", "reconcile", "--dry-run", "--no-restart", "--json"],
    )

    assert result.exit_code == 0
    assert '"model": "vendor/example-model"' in result.output
    assert manager.reconcile_calls == [
        (None, {"restart": False, "verify": True, "dry_run": True})
    ]


def test_status_and_cancel_cli_use_credential_aware_api_client(monkeypatch):
    calls = []

    class API:
        def health(self):
            calls.append(("health", None))
            return {"status": "ok"}

        def cancel_session(self, session_id):
            calls.append(("cancel", session_id))
            return {"session_id": session_id, "cancelled": True}

    monkeypatch.setattr("agent_team.cli.AgentTeamAPIClient", lambda: API())

    status = CliRunner().invoke(cli, ["status"])
    cancel = CliRunner().invoke(cli, ["cancel", "session-123"])

    assert status.exit_code == 0
    assert "Health: ok" in status.output
    assert cancel.exit_code == 0
    assert "session-123" in cancel.output
    assert calls == [("health", None), ("cancel", "session-123")]
