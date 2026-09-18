"""GitOps repository-boundary acceptance tests."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.engine import AgentTeamEngine
from agent_team.runtime_boundary import RuntimeBoundaryError, ensure_external_runtime_paths


ROOT = Path(__file__).parents[1]


def check_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item for item in result.stdout.decode().split("\0") if item]


def test_publication_inventory_excludes_unrelated_research():
    research_files = [
        path.relative_to(ROOT)
        for path in candidate_files()
        if path.exists() and path.relative_to(ROOT).parts[0] == "research"
    ]

    assert research_files == []


def test_publishable_files_have_no_literal_ipv4_or_machine_paths():
    ipv4 = re.compile(r"(?<![\\w.])(?:\\d{1,3}\\.){3}\\d{1,3}(?![\\w.])")
    machine_paths = (
        "/" + "home" + "/",
        "/" + "opt" + "/",
        "/" + "run" + "/service/",
        "/" + "command" + "/",
        "/" + "package" + "/admin/",
    )
    for path in candidate_files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert not ipv4.search(text), path.relative_to(ROOT)
        assert not any(value in text for value in machine_paths), path.relative_to(ROOT)


def test_source_tests_templates_and_pi_assets_are_trackable():
    for path in (
        "src/agent_team/engine.py",
        "tests/test_e2e.py",
        "pyproject.toml",
        "uv.lock",
        "s6-service/agent-team/run",
        "pi/extensions/example.py",
        ".env.example",
    ):
        assert not check_ignored(path), path


def test_runtime_credentials_dependencies_and_downloads_are_ignored():
    for path in (
        ".env",
        ".agent-team/sessions/session.json",
        ".hermes/state.db",
        "runtime.lock",
        "runtime-owner.json",
        ".agent-team-pi.lock",
        "state/projects/project.json",
        "knowledge.db",
        "memory/principal.json",
        "artifacts/result.json",
        "logs/agent-team.log",
        "credentials/token.json",
        ".pi/keys.toml",
        ".pi/agent/settings.json",
        ".venv/bin/python",
        ".cache/pip/http/cache",
        "pip-cache/wheels/package.whl",
        "uv-cache/archive/package.whl",
        "node_modules/package/index.js",
        "bootstrap/install.sh",
        "downloads/model.whl",
        "get-pip.py",
        "pi/.env",
        ".pi/agent-team/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/pi/README.md",
    ):
        assert check_ignored(path), path
    assert not check_ignored("pi/.env.example")


def test_deployment_templates_have_no_instance_specific_values():
    templates = [
        ROOT / "s6-service/agent-team/run",
        ROOT / "s6-service/agent-team-init/run",
        ROOT / ".env.example",
    ]
    forbidden = ("/" + "home" + "/user", "host" + ".docker.internal", "nvidia/Qwen3.8-27B-NVFP4")
    text = "\n".join(path.read_text(encoding="utf-8") for path in templates)
    portable_shebang = "#!/" + "usr/bin/env -S with-contenv sh"
    assert templates[0].read_text(encoding="utf-8").startswith(portable_shebang)
    assert templates[1].read_text(encoding="utf-8").startswith(portable_shebang)
    assert not any(value in text for value in forbidden)
    assert "LLM_BASE_URL" in text
    assert "LLM_MODEL" in text
    assert "AGENT_TEAM_PROJECT_ROOT" in text
    assert "AGENT_TEAM_STATE_DIR" in text
    assert "AGENT_TEAM_VENV" in text
    assert "$project/.venv" not in text
    assert "state directory must be outside project" in text


def test_runtime_state_and_workspace_must_be_external():
    with pytest.raises(RuntimeBoundaryError, match="state_dir"):
        ensure_external_runtime_paths(
            ROOT / "runtime-state",
            workspace_dir=ROOT / "runtime-workspace",
            repository_root=ROOT,
        )

    with pytest.raises(RuntimeBoundaryError, match="workspace_dir"):
        ensure_external_runtime_paths(
            Path("/tmp/agent-team-state"),
            workspace_dir=ROOT / "runtime-workspace",
            repository_root=ROOT,
        )


def test_external_runtime_paths_are_accepted(tmp_path: Path):
    state_dir, workspace_dir = ensure_external_runtime_paths(
        tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        repository_root=ROOT,
    )
    assert state_dir == (tmp_path / "state").resolve()
    assert workspace_dir == (tmp_path / "workspace").resolve()
    assert not (ROOT / "get-pip.py").exists()


def test_engine_rejects_runtime_paths_inside_checkout():
    runtime_root = ROOT / ".gitops-test-runtime"
    try:
        config = Config(
            runtime=RuntimeConfig(
                state_dir=runtime_root,
                workspace_dir=runtime_root / "workspace",
            ),
            memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
            models={
                name: ModelConfig(model=f"{name}-test", base_url="http://localhost:1")
                for name in ("principal", "manager", "worker")
            },
        )
        with pytest.raises(RuntimeBoundaryError, match="state_dir"):
            AgentTeamEngine(config)
    finally:
        import shutil

        shutil.rmtree(runtime_root, ignore_errors=True)


def test_config_reads_external_runtime_roots_from_environment(monkeypatch, tmp_path: Path):
    state_dir = tmp_path / "state"
    workspace_dir = tmp_path / "workspace"
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "not-present.yaml"))
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENT_TEAM_WORKSPACE_DIR", str(workspace_dir))

    config = Config.from_env()

    assert config.runtime.state_dir == state_dir
    assert config.runtime.workspace_dir == workspace_dir


def test_runtime_boundary_uses_deployment_project_root(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    monkeypatch.setenv("AGENT_TEAM_PROJECT_ROOT", str(checkout))

    with pytest.raises(RuntimeBoundaryError, match="state_dir"):
        ensure_external_runtime_paths(checkout / "state")
