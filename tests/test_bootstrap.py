"""Acceptance tests for clone-and-stand-up bootstrap checks."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from agent_team.bootstrap import BootstrapError, Bootstrapper


ROOT = Path(__file__).parents[1]


def make_clone(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "clone"
    repo.mkdir()
    shutil.copytree(ROOT / "pi", repo / "pi", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy(ROOT / ".gitignore", repo / ".gitignore")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "clone",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, revision


def executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


class ModelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "bootstrap-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/ready":
            body = json.dumps({"status": "ok", "ready": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass


def start_model_server():
    server = ThreadingHTTPServer(("localhost", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://localhost:{server.server_port}/v1"


def write_config(state_dir: Path, endpoint: str) -> Path:
    config = state_dir / "config" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""runtime:\n  state_dir: {state_dir}\nmodels:\n  principal:\n    model: bootstrap-model\n    base_url: {endpoint}\n  manager:\n    model: bootstrap-model\n    base_url: {endpoint}\n  worker:\n    model: bootstrap-model\n    base_url: {endpoint}\n  curator:\n    model: bootstrap-model\n    base_url: {endpoint}\n""",
        encoding="utf-8",
    )
    return config


def test_bootstrap_verifies_clone_environment_and_readiness_without_installing(tmp_path: Path):
    repo, revision = make_clone(tmp_path)
    state_dir = tmp_path / "state"
    server, endpoint = start_model_server()
    pi_log = tmp_path / "pi-install.log"
    pi = executable(
        tmp_path / "pi",
        f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then printf 'pi 0.84.3\\n'; exit 0; fi\nprintf 'unexpected pi command' > {pi_log}\nexit 99\n",
    )
    s6 = executable(tmp_path / "s6-svstat", "#!/bin/sh\nprintf 'up (pid 1)\\n'\n")
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    config = write_config(state_dir, endpoint)

    try:
        report = Bootstrapper(
            repo,
            pi,
            python_executable=ROOT / ".venv" / "bin" / "python",
            model_endpoint=endpoint,
            config_file=config,
            state_dir=state_dir,
            service_dir=service_dir,
            s6_svstat=s6,
            health_url=endpoint.rsplit("/v1", 1)[0] + "/ready",
        ).run(expected_revision=revision)
    finally:
        server.shutdown()
        server.server_close()

    assert report.ready is True, report.model_dump_json(indent=2)
    assert report.revision == revision
    assert report.pi_version == "pi 0.84.3"
    assert report.model_ids == ["bootstrap-model"]
    assert report.health_ok is True
    assert report.s6_ready is True
    assert {check.name for check in report.checks} >= {
        "git revision",
        "repository boundary",
        "Pi version",
        "Python environment",
        "model endpoint",
        "writable state directories",
        "configuration",
        "Pi asset manifest",
        "s6 service",
        "Agent Team health",
    }
    assert all(check.status == "passed" for check in report.checks)
    assert not pi_log.exists()
    for name in (
        "config", "state", "sessions", "checkpoints", "memory", "knowledge",
        "skills", "projects", "artifacts", "logs",
    ):
        assert (state_dir / name).is_dir()


def test_bootstrap_reports_missing_external_tools_without_installing(tmp_path: Path):
    repo, _ = make_clone(tmp_path)
    state_dir = tmp_path / "external-state"

    report = Bootstrapper(
        repo,
        tmp_path / "missing-pi",
        python_executable=tmp_path / "missing-python",
        model_endpoint="http://localhost:1/v1",
        state_dir=state_dir,
        service_dir=tmp_path / "missing-service",
        s6_svstat=tmp_path / "missing-s6-svstat",
        http_get=lambda *_args: (_ for _ in ()).throw(OSError("endpoint unavailable")),
    ).run()

    assert report.ready is False
    assert {"pi", "python", "s6-svstat"} <= set(report.missing_external_tools)
    assert report.install_actions == []
    assert state_dir.is_dir()


def test_bootstrap_rejects_uncommitted_tree_and_repo_state_directory(tmp_path: Path):
    repo, _ = make_clone(tmp_path)
    (repo / "uncommitted.txt").write_text("dirty", encoding="utf-8")
    pi = executable(tmp_path / "pi", "#!/bin/sh\nprintf 'pi 0.84.3\\n'\n")
    state_inside_repo = repo / ".agent-team"

    report = Bootstrapper(
        repo,
        pi,
        python_executable=sys.executable,
        model_endpoint="http://localhost:1/v1",
        state_dir=state_inside_repo,
        service_dir=tmp_path / "missing-service",
        s6_svstat=tmp_path / "missing-s6-svstat",
    ).run()

    assert report.ready is False
    assert any(check.name == "git revision" and check.status == "failed" for check in report.checks)
    assert any("outside" in check.detail for check in report.checks if check.name == "writable state directories")
    assert not state_inside_repo.exists()


def test_bootstrap_defaults_to_external_python_environment(tmp_path: Path, monkeypatch):
    repo, _ = make_clone(tmp_path)
    external_venv = tmp_path / "agent-team-venv"
    monkeypatch.setenv("AGENT_TEAM_VENV", str(external_venv))

    bootstrapper = Bootstrapper(
        repo,
        tmp_path / "missing-pi",
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "state",
        service_dir=tmp_path / "service",
    )

    assert bootstrapper.python_executable == external_venv / "bin" / "python"
    assert not str(bootstrapper.python_executable).startswith(str(repo))


def test_bootstrap_requires_an_injected_external_python(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_PYTHON", raising=False)
    monkeypatch.delenv("AGENT_TEAM_VENV", raising=False)

    with pytest.raises(BootstrapError, match="AGENT_TEAM_PYTHON or AGENT_TEAM_VENV"):
        Bootstrapper(
            tmp_path,
            tmp_path / "missing-pi",
            model_endpoint="http://localhost:1/v1",
            state_dir=tmp_path / "state",
            service_dir=tmp_path / "service",
        )


def test_bootstrap_requires_an_injected_service_directory(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_SERVICE_DIR", raising=False)

    with pytest.raises(BootstrapError, match="AGENT_TEAM_SERVICE_DIR"):
        Bootstrapper(
            tmp_path,
            tmp_path / "missing-pi",
            python_executable=sys.executable,
            model_endpoint="http://localhost:1/v1",
            state_dir=tmp_path / "state",
        )


def test_bootstrap_dependency_probe_does_not_inherit_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-model-credential")
    monkeypatch.setenv("DEPLOYMENT_TOKEN", "test-deployment-token")
    monkeypatch.setenv("AGENT_TEAM_SAFE_MARKER", "preserved")
    calls = []

    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Python 3.11.16\n", "")
        payload = {
            "python": "3.11.16",
            "found": {
                name: "test"
                for name in (
                    "pydantic",
                    "pydantic_ai",
                    "httpx",
                    "fastapi",
                    "uvicorn",
                    "click",
                    "aiosqlite",
                    "yaml",
                )
            },
            "missing": [],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload) + "\n", "")

    bootstrapper = Bootstrapper(
        tmp_path,
        tmp_path / "missing-pi",
        python_executable=sys.executable,
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "state",
        service_dir=tmp_path / "service",
        runner=runner,
    )
    checks = []

    version, dependencies = bootstrapper._check_python(checks)

    assert version == "3.11.16"
    assert dependencies["pydantic"] == "test"
    probe_env = calls[1][1]["env"]
    assert "LLM_API_KEY" not in probe_env
    assert "DEPLOYMENT_TOKEN" not in probe_env
    assert probe_env["AGENT_TEAM_SAFE_MARKER"] == "preserved"


def test_bootstrap_reports_missing_git_without_installing(tmp_path: Path):
    repo, _ = make_clone(tmp_path)

    def runner(command, **_kwargs):
        if command[0] == "git":
            raise FileNotFoundError("git")
        raise AssertionError(f"unexpected command: {command}")

    report = Bootstrapper(
        repo,
        tmp_path / "missing-pi",
        python_executable=tmp_path / "missing-python",
        model_endpoint="http://localhost:1/v1",
        state_dir=tmp_path / "state",
        service_dir=tmp_path / "service",
        runner=runner,
        http_get=lambda *_args: (_ for _ in ()).throw(OSError("endpoint unavailable")),
    ).run()

    assert report.ready is False
    assert "git" in report.missing_external_tools
    assert report.install_actions == []
