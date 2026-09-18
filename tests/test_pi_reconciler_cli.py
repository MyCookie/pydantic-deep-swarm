"""CLI boundary tests for the Pi reconciler."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agent_team.cli import cli
from agent_team.pi_reconciler import PiReconcileResult, PiStatus


REVISION = "a" * 40
PACKAGE = Path("/tmp/agent-team-release/pi")


class FakeReconciler:
    instances: list["FakeReconciler"] = []

    def __init__(self, repository_root, pi_binary, **kwargs):
        self.repository_root = repository_root
        self.pi_binary = pi_binary
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def reconcile(self, revision, **kwargs):
        self.calls.append(("reconcile", revision, kwargs))
        return PiReconcileResult(
            revision=revision,
            scope=self.kwargs["scope"],
            package_path=PACKAGE,
            changed=True,
            verified=True,
            pi_version="pi test 1.0",
        )

    def status(self):
        self.calls.append(("status",))
        return PiStatus(
            scope=self.kwargs["scope"],
            settings_path=Path("/tmp/settings.json"),
            managed_root=Path("/tmp/agent-team"),
            installed_revision=REVISION,
        )

    def rollback(self, **kwargs):
        self.calls.append(("rollback", kwargs))
        return PiReconcileResult(
            revision=REVISION,
            scope=self.kwargs["scope"],
            package_path=PACKAGE,
            previous_revision="b" * 40,
            changed=True,
            verified=True,
            pi_version="pi test 1.0",
        )


def test_pi_reconcile_cli_passes_exact_revision_and_absolute_binary(monkeypatch, tmp_path: Path):
    FakeReconciler.instances.clear()
    monkeypatch.setattr("agent_team.cli.PiAssetReconciler", FakeReconciler)
    binary = tmp_path / "pi"
    binary.write_text("placeholder", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "pi",
            "reconcile",
            REVISION,
            "--repo-root",
            str(tmp_path),
            "--pi-binary",
            str(binary),
            "--scope",
            "user-global",
            "--user-home",
            str(tmp_path / "home"),
            "--approve",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["revision"] == REVISION
    assert data["verified"] is True
    instance = FakeReconciler.instances[0]
    assert instance.pi_binary == binary
    assert instance.kwargs == {
        "scope": "user-global",
        "user_home": tmp_path / "home",
    }
    assert instance.calls == [("reconcile", REVISION, {"approve_project": True, "dry_run": False})]


def test_pi_status_cli_emits_drift_and_rollback_uses_scope(monkeypatch, tmp_path: Path):
    FakeReconciler.instances.clear()
    monkeypatch.setattr("agent_team.cli.PiAssetReconciler", FakeReconciler)
    binary = tmp_path / "pi"
    binary.write_text("placeholder", encoding="utf-8")

    status = CliRunner().invoke(
        cli,
        [
            "pi",
            "status",
            "--repo-root",
            str(tmp_path),
            "--pi-binary",
            str(binary),
            "--scope",
            "project-local",
            "--json",
        ],
    )
    rollback = CliRunner().invoke(
        cli,
        [
            "pi",
            "rollback",
            "--repo-root",
            str(tmp_path),
            "--pi-binary",
            str(binary),
            "--scope",
            "project-local",
        ],
    )

    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["installed_revision"] == REVISION
    assert rollback.exit_code == 0, rollback.output
    assert "verified: True" in rollback.output
    assert [call[0] for call in FakeReconciler.instances[0].calls] == ["status"]
    assert [call[0] for call in FakeReconciler.instances[1].calls] == ["rollback"]
