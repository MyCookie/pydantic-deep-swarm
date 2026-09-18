"""Acceptance tests for exact-revision Pi asset reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_team.pi_reconciler import (
    PiAssetReconciler,
    PiReconcileError,
    PiStatus,
)


ROOT = Path(__file__).parents[1]


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakePi:
    """A deterministic Pi CLI double that persists package settings."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        command = list(args[1:])
        if command == ["--version"]:
            return Result(stdout="pi test 1.0\n")
        if command and command[0] == "install":
            package = Path(command[1])
            local = "-l" in command
            cwd = Path(kwargs["cwd"])
            if local:
                settings = cwd / ".pi" / "settings.json"
            else:
                settings = Path(kwargs["env"]["HOME"]) / ".pi" / "agent" / "settings.json"
            data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
            data.setdefault("packages", []).append(str(package))
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps(data), encoding="utf-8")
            return Result(stdout="Installed\n")
        if command == ["--mode", "rpc", "--no-session"]:
            return Result()
        return Result(returncode=2, stderr=f"unexpected Pi args: {command!r}")


class TrackingPi(FakePi):
    """Pi double that detects overlapping install calls."""

    def __init__(self):
        super().__init__()
        self._activity_lock = threading.Lock()
        self.active_installs = 0
        self.max_active_installs = 0

    def __call__(self, args, **kwargs):
        command = list(args[1:])
        if command and command[0] == "install":
            with self._activity_lock:
                self.active_installs += 1
                self.max_active_installs = max(self.max_active_installs, self.active_installs)
            try:
                time.sleep(0.05)
                return super().__call__(args, **kwargs)
            finally:
                with self._activity_lock:
                    self.active_installs -= 1
        return super().__call__(args, **kwargs)


class FailingPi(FakePi):
    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if list(args[1:]) == ["--version"]:
            return Result(stdout="pi test 1.0\n")
        if len(args) > 1 and args[1] == "install":
            return Result(returncode=17, stderr="install failed")
        return Result()


def init_repo(path: Path) -> tuple[Path, str, str]:
    repo = path / "repo"
    repo.mkdir()
    shutil.copytree(
        ROOT / "pi",
        repo / "pi",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "pi"], check=True)
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
            "v1",
        ],
        check=True,
    )
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "pi" / "README.md").write_text(
        (repo / "pi" / "README.md").read_text(encoding="utf-8") + "\nrevision two\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "pi/README.md"], check=True)
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
            "v2",
        ],
        check=True,
    )
    second = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, first, second


def pi_binary(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "pi"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_reconcile_exact_revision_uses_absolute_pi_and_preserves_unmanaged_files(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    unmanaged_package = str(tmp_path / "unmanaged" / "pi-package")
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(
        json.dumps({"theme": "operator", "packages": [unmanaged_package]}),
        encoding="utf-8",
    )
    unmanaged = repo / ".pi" / "unmanaged.txt"
    unmanaged.write_text("keep", encoding="utf-8")
    fake = FakePi()

    result = PiAssetReconciler(
        repo,
        pi_binary(tmp_path),
        scope="project-local",
        runner=fake,
    ).reconcile(first)

    assert result.revision == first
    assert result.verified is True
    assert result.changed is True
    assert result.package_path.is_absolute()
    assert result.package_path.is_dir()
    settings = json.loads((repo / ".pi" / "settings.json").read_text(encoding="utf-8"))
    assert settings["theme"] == "operator"
    assert settings["packages"] == [unmanaged_package, str(result.package_path)]
    assert unmanaged.read_text(encoding="utf-8") == "keep"
    assert all(call[0][0] == str(pi_binary(tmp_path).resolve()) for call in fake.calls)
    assert fake.calls[1][0][1:3] == ["install", str(result.package_path)]
    assert "-l" in fake.calls[1][0]
    assert fake.calls[2][0][1:] == ["--mode", "rpc", "--no-session"]


def test_concurrent_reconcile_serializes_install_and_leaves_synchronized_state(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    fake = TrackingPi()
    reconcilers = [
        PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake),
        PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reconciler.reconcile, first) for reconciler in reconcilers]
        results = [future.result() for future in futures]

    assert all(result.verified for result in results)
    assert fake.max_active_installs == 1
    status = reconcilers[0].status()
    assert status.installed_revision == first
    assert status.drift == []


def test_status_reports_drift_and_rollback_returns_to_last_known_good(tmp_path: Path):
    repo, first, second = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    first_result = reconciler.reconcile(first)
    second_result = reconciler.reconcile(second)
    assert first_result.verified and second_result.verified
    assert second_result.previous_revision == first

    settings_path = repo / ".pi" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["packages"] = []
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    drift = reconciler.status()
    assert drift.installed_revision == second
    assert any("package" in item for item in drift.drift)

    rollback = reconciler.rollback()
    assert rollback.revision == first
    assert rollback.verified is True
    assert rollback.previous_revision == second
    final_status = reconciler.status()
    assert final_status.drift == []
    assert final_status.installed_revision == first


def test_reconcile_rejects_symbolic_revision_before_mutation(tmp_path: Path):
    repo, _, _ = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    with pytest.raises(PiReconcileError, match="full commit"):
        reconciler.reconcile("main")

    assert fake.calls == []
    assert not (repo / ".pi" / "agent-team").exists()


def test_failed_install_restores_settings_and_owned_tree(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    settings_path = repo / ".pi" / "settings.json"
    settings_path.parent.mkdir()
    original = (
        json.dumps({"theme": "operator", "packages": [str(tmp_path / "unmanaged")]})
        + "\n"
    ).encode()
    settings_path.write_bytes(original)
    fake = FailingPi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    with pytest.raises(PiReconcileError, match="Pi command failed"):
        reconciler.reconcile(first)

    assert settings_path.read_bytes() == original
    assert not (repo / ".pi" / "agent-team").exists()


def test_status_detects_installed_package_content_drift(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)
    result = reconciler.reconcile(first)
    (result.package_path / "README.md").write_text("tampered", encoding="utf-8")

    status = reconciler.status()

    assert any("content drifted" in item for item in status.drift)


def test_failed_post_install_verification_restores_previous_state(tmp_path: Path, monkeypatch):
    repo, first, second = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)
    reconciler.reconcile(first)
    state_path = repo / ".pi" / "agent-team" / "state.json"
    original_state = state_path.read_bytes()
    settings_path = repo / ".pi" / "settings.json"
    original_settings = settings_path.read_bytes()

    def failed_status():
        return PiStatus(
            scope="project-local",
            settings_path=settings_path,
            managed_root=repo / ".pi" / "agent-team",
            installed_revision=second,
            expected_package_path=reconciler._release_path(second) / "pi",
            drift=["simulated verification race"],
        )

    monkeypatch.setattr(reconciler, "status", failed_status)
    with pytest.raises(PiReconcileError, match="completed with drift"):
        reconciler.reconcile(second)

    assert state_path.read_bytes() == original_state
    assert settings_path.read_bytes() == original_settings
    assert not (repo / ".pi" / "agent-team" / "releases" / second).exists()


def test_dry_run_validates_revision_without_installing_or_creating_state(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    result = reconciler.reconcile(first, dry_run=True)

    assert result.dry_run is True
    assert result.verified is False
    assert len(fake.calls) == 1
    assert fake.calls[0][0][1:] == ["--version"]
    assert not (repo / ".pi" / "agent-team").exists()


def test_reconcile_refuses_unmarked_managed_root(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    managed = repo / ".pi" / "agent-team"
    managed.mkdir(parents=True)
    (managed / "unmanaged.txt").write_text("keep", encoding="utf-8")
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    with pytest.raises(PiReconcileError, match="unmarked resources"):
        reconciler.reconcile(first)

    assert (managed / "unmanaged.txt").read_text(encoding="utf-8") == "keep"
    assert not any(call[0][1] == "install" for call in fake.calls)


def test_status_detects_missing_ownership_marker(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)
    reconciler.reconcile(first)
    (repo / ".pi" / "agent-team" / ".agent-team-managed.json").unlink()

    status = reconciler.status()

    assert any("ownership" in item or "marker" in item for item in status.drift)


def test_status_detects_malformed_ownership_marker(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)
    reconciler.reconcile(first)
    (repo / ".pi" / "agent-team" / ".agent-team-managed.json").write_text("[]", encoding="utf-8")

    status = reconciler.status()

    assert any("ownership" in item or "marker" in item for item in status.drift)


def test_reconcile_refuses_symlinked_managed_root(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    managed = repo / ".pi" / "agent-team"
    managed.parent.mkdir(parents=True)
    managed.symlink_to(outside, target_is_directory=True)
    fake = FakePi()
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    with pytest.raises(PiReconcileError, match="symlink"):
        reconciler.reconcile(first)

    assert not (outside / ".agent-team-managed.json").exists()
    assert not any(call[0][1] == "install" for call in fake.calls)


def test_user_global_scope_uses_explicit_home_and_preserves_other_packages(tmp_path: Path):
    repo, first, _ = init_repo(tmp_path)
    home = tmp_path / "operator-home"
    unmanaged_package = str(tmp_path / "unmanaged" / "global-package")
    settings = home / ".pi" / "agent" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"packages": [unmanaged_package], "theme": "dark"}),
        encoding="utf-8",
    )
    fake = FakePi()

    result = PiAssetReconciler(
        repo,
        pi_binary(tmp_path),
        scope="user-global",
        user_home=home,
        runner=fake,
    ).reconcile(first)

    assert result.verified is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["packages"] == [unmanaged_package, str(result.package_path)]
    assert result.package_path.is_relative_to(home / ".pi" / "agent" / "agent-team")
    assert fake.calls[1][1]["env"]["HOME"] == str(home)


def test_pi_subprocess_does_not_inherit_model_credentials(tmp_path: Path, monkeypatch):
    repo, _, _ = init_repo(tmp_path)
    fake = FakePi()
    monkeypatch.setenv("LLM_API_KEY", "test-bearer-credential")
    monkeypatch.setenv("DEPLOYMENT_TOKEN", "test-unrelated-token")
    monkeypatch.setenv("AGENT_TEAM_SAFE_MARKER", "preserved")
    reconciler = PiAssetReconciler(repo, pi_binary(tmp_path), runner=fake)

    assert reconciler._pi_version() == "pi test 1.0"
    assert "LLM_API_KEY" not in fake.calls[0][1]["env"]
    assert "DEPLOYMENT_TOKEN" not in fake.calls[0][1]["env"]
    assert fake.calls[0][1]["env"]["AGENT_TEAM_SAFE_MARKER"] == "preserved"
