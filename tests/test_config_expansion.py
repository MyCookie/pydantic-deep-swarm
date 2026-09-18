"""Fail-closed tests for bounded nested environment expansion."""

from __future__ import annotations

from click.testing import CliRunner
import pytest

from agent_team.cli import cli
from agent_team.config import Config


NESTED_BASE_URL = "${PRINCIPAL_BASE_URL:-${LLM_BASE_URL:-http://fallback}}"


def test_nested_expansion_prefers_nonempty_outer_value(monkeypatch):
    monkeypatch.setenv("PRINCIPAL_BASE_URL", "http://principal")
    monkeypatch.setenv("LLM_BASE_URL", "http://shared")

    assert Config._expand_env_vars(NESTED_BASE_URL) == "http://principal"


def test_nested_expansion_falls_back_to_inner_variable(monkeypatch):
    monkeypatch.delenv("PRINCIPAL_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://shared")

    assert Config._expand_env_vars(NESTED_BASE_URL) == "http://shared"


def test_nested_expansion_falls_back_to_inner_literal(monkeypatch):
    monkeypatch.delenv("PRINCIPAL_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    assert Config._expand_env_vars(NESTED_BASE_URL) == "http://fallback"


def test_nested_expansion_treats_empty_outer_as_unset(monkeypatch):
    monkeypatch.setenv("PRINCIPAL_BASE_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://shared")

    assert Config._expand_env_vars(NESTED_BASE_URL) == "http://shared"


def test_nested_expansion_rejects_malformed_or_cyclic_expressions(monkeypatch):
    with pytest.raises(ValueError, match="unterminated"):
        Config._expand_env_vars("${OUTER:-${INNER}")

    monkeypatch.setenv("LOOP_A", "${LOOP_B}")
    monkeypatch.setenv("LOOP_B", "${LOOP_A}")
    with pytest.raises(ValueError, match="cyclic"):
        Config._expand_env_vars("${LOOP_A}")


def test_nested_expansion_rejects_invalid_names_and_excessive_depth(monkeypatch):
    with pytest.raises(ValueError, match="invalid environment variable name"):
        Config._expand_env_vars("${NOT-VALID}")

    for index in range(18):
        monkeypatch.setenv(f"LEVEL_{index}", f"${{LEVEL_{index + 1}}}")
    monkeypatch.setenv("LEVEL_18", "done")
    with pytest.raises(ValueError, match="maximum nesting depth"):
        Config._expand_env_vars("${LEVEL_0}")


def test_generated_init_config_resolves_all_role_models_and_endpoints(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LLM_BASE_URL", "http://shared")
    monkeypatch.setenv("PRINCIPAL_BASE_URL", "http://principal")
    monkeypatch.delenv("MANAGER_BASE_URL", raising=False)
    monkeypatch.delenv("WORKER_BASE_URL", raising=False)
    monkeypatch.delenv("CURATOR_BASE_URL", raising=False)
    monkeypatch.setenv("PRINCIPAL_MODEL", "principal-model")
    monkeypatch.setenv("MANAGER_MODEL", "manager-model")
    monkeypatch.setenv("WORKER_MODEL", "worker-model")
    monkeypatch.delenv("CURATOR_MODEL", raising=False)

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    config_path = state_dir / "config" / "config.yaml"
    assert config_path.is_file()
    generated = config_path.read_text(encoding="utf-8")
    assert "retention:" in generated
    assert "max_session_messages: 200" in generated
    assert "session_max_age_days: null" in generated
    assert "log_max_bytes: null" in generated
    monkeypatch.delenv("AGENT_TEAM_CONFIG_FILE")
    config = Config.from_env()
    assert config.models["principal"].base_url == "http://principal"
    assert config.models["manager"].base_url == "http://shared"
    assert config.models["worker"].base_url == "http://shared"
    assert config.models["curator"].base_url == "http://shared"
    assert config.models["principal"].model == "principal-model"
    assert config.models["manager"].model == "manager-model"
    assert config.models["worker"].model == "worker-model"
    assert config.models["curator"].model == "worker-model"
    assert "${" not in str(config.model_dump(mode="json"))
