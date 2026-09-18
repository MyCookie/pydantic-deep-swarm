"""CLI boundary tests for clone-and-stand-up bootstrap."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agent_team.bootstrap import BootstrapReport
from agent_team.cli import cli


REVISION = "a" * 40


class FakeBootstrapper:
    instances: list["FakeBootstrapper"] = []
    ready = True

    def __init__(self, repository_root, pi_binary, **kwargs):
        self.repository_root = Path(repository_root)
        self.pi_binary = Path(pi_binary)
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, **kwargs):
        self.calls.append(kwargs)
        fixture_root = self.repository_root / "deployment-fixture"
        python_executable = Path(
            self.kwargs.get("python_executable") or fixture_root / "venv" / "bin" / "python"
        )
        state_dir = Path(self.kwargs.get("state_dir") or fixture_root / "state")
        config_file = Path(
            self.kwargs.get("config_file") or state_dir / "config" / "config.yaml"
        )
        service_dir = Path(self.kwargs.get("service_dir") or fixture_root / "service")
        s6_svstat = Path(self.kwargs.get("s6_svstat") or fixture_root / "bin" / "s6-svstat")
        return BootstrapReport(
            repository_root=self.repository_root,
            revision=REVISION,
            expected_revision=kwargs.get("expected_revision"),
            pi_binary=self.pi_binary,
            pi_version="pi 0.84.3",
            python_executable=python_executable,
            python_version="3.11.16",
            dependencies={"pydantic": "2.0"},
            model_endpoint="http://localhost:8000/v1",
            model_ids=["bootstrap-model"],
            state_dir=state_dir,
            config_file=config_file,
            service_dir=service_dir,
            s6_svstat=s6_svstat,
            health_url="http://localhost:8080/ready",
            health_ok=self.ready,
            s6_ready=self.ready,
            checks=[],
            ready=self.ready,
            install_actions=[],
        )


def test_bootstrap_cli_passes_paths_and_writes_report(monkeypatch, tmp_path: Path):
    FakeBootstrapper.instances.clear()
    FakeBootstrapper.ready = True
    monkeypatch.setattr("agent_team.cli.Bootstrapper", FakeBootstrapper)
    report_path = tmp_path / "reports" / "bootstrap.json"
    pi_binary = tmp_path / "bin" / "pi"
    python_executable = tmp_path / "venv" / "bin" / "python"
    config_file = tmp_path / "state" / "config" / "config.yaml"
    state_dir = tmp_path / "state"
    service_dir = tmp_path / "service"
    s6_svstat = tmp_path / "bin" / "s6-svstat"

    result = CliRunner().invoke(
        cli,
        [
            "bootstrap",
            "--repo-root",
            str(tmp_path),
            "--pi-binary",
            str(pi_binary),
            "--python",
            str(python_executable),
            "--model-endpoint",
            "http://localhost:8000/v1",
            "--config",
            str(config_file),
            "--state-dir",
            str(state_dir),
            "--service-dir",
            str(service_dir),
            "--s6-svstat",
            str(s6_svstat),
            "--ready-url",
            "http://localhost:8080/ready",
            "--expected-revision",
            REVISION,
            "--report",
            str(report_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ready"] is True
    assert json.loads(report_path.read_text(encoding="utf-8"))["revision"] == REVISION
    instance = FakeBootstrapper.instances[0]
    assert instance.pi_binary == pi_binary
    assert instance.kwargs == {
        "python_executable": python_executable,
        "model_endpoint": "http://localhost:8000/v1",
        "config_file": config_file,
        "state_dir": state_dir,
        "service_dir": service_dir,
        "s6_svstat": s6_svstat,
        "health_url": "http://localhost:8080/ready",
    }
    assert instance.calls == [{"expected_revision": REVISION}]


def test_bootstrap_cli_returns_failure_for_unready_report(monkeypatch, tmp_path: Path):
    FakeBootstrapper.instances.clear()
    FakeBootstrapper.ready = False
    monkeypatch.setattr("agent_team.cli.Bootstrapper", FakeBootstrapper)

    result = CliRunner().invoke(
        cli,
        [
            "bootstrap",
            "--repo-root",
            str(tmp_path),
            "--pi-binary",
            str(tmp_path / "bin" / "pi"),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["ready"] is False
