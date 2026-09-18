"""End-to-end acceptance of the complete local GitOps release path."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent_team.bootstrap import Bootstrapper
from agent_team.pi_reconciler import PiAssetReconciler


ROOT = Path(__file__).parents[1]


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class InstalledPi:
    """Deterministic Pi installation boundary for the parent acceptance test."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        command = list(args[1:])
        self.calls.append(list(args))
        if command == ["--version"]:
            return Result(stdout="pi test 1.0\n")
        if command and command[0] == "install":
            package_path = Path(command[1])
            if "-l" in command:
                settings_path = Path(kwargs["cwd"]) / ".pi" / "settings.json"
            else:
                settings_path = Path(kwargs["env"]["HOME"]) / ".pi" / "agent" / "settings.json"
            settings = {}
            if settings_path.exists():
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings.setdefault("packages", []).append(str(package_path))
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            return Result(stdout="Installed\n")
        if command == ["--mode", "rpc", "--no-session"]:
            return Result()
        return Result(returncode=2, stderr=f"unexpected Pi command: {command!r}")


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(cwd: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=cwd, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        cwd=cwd,
        check=True,
    )
    return git("rev-parse", "HEAD", cwd=cwd)


def make_origin(tmp_path: Path) -> tuple[Path, str, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    shutil.copytree(ROOT / "pi", origin / "pi", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "src", origin / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", origin / "pyproject.toml")
    shutil.copy(ROOT / ".gitignore", origin / ".gitignore")
    subprocess.run(["git", "init", "--quiet", str(origin)], check=True)
    first = commit(origin, "first trusted assets")

    prompt = origin / "pi" / "README.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nsecond revision\n", encoding="utf-8")
    second = commit(origin, "second trusted assets")
    return origin, first, second


def write_config(path: Path, state_dir: Path, endpoint: str) -> Path:
    config = path / "config" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""runtime:\n  state_dir: {state_dir}\nmodels:\n  principal:\n    model: test-model\n    base_url: {endpoint}\n  manager:\n    model: test-model\n    base_url: {endpoint}\n  worker:\n    model: test-model\n    base_url: {endpoint}\n  curator:\n    model: test-model\n    base_url: {endpoint}\n""",
        encoding="utf-8",
    )
    return config


def executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_trusted_revision_can_be_cloned_validated_reconciled_rolled_back_and_stood_up(tmp_path: Path):
    origin, first, second = make_origin(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True)
    assert git("rev-parse", "HEAD", cwd=clone) == second

    manifest_check = subprocess.run(
        [sys.executable, "pi/manifest_validator.py", "pi/manifest.json"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    assert manifest_check.returncode == 0, manifest_check.stderr

    state_dir = tmp_path / "external-state"
    config = write_config(state_dir, state_dir, "http://localhost:1/v1")
    pi_binary = executable(
        tmp_path / "pi-bin",
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then printf 'pi test 1.0\\n'; exit 0; fi\nexit 1\n",
    )
    s6_svstat = executable(tmp_path / "s6-svstat", "#!/bin/sh\nprintf 'up (pid 1)\\n'\n")
    service_dir = tmp_path / "service"
    service_dir.mkdir()

    def http_get(url, _headers, _timeout):
        if url.endswith("/models"):
            return 200, b'{"data":[{"id":"test-model"}]}'
        if url.endswith("/ready"):
            return 200, b'{"status":"ok","ready":true}'
        raise AssertionError(url)

    bootstrap = Bootstrapper(
        clone,
        pi_binary,
        python_executable=Path(sys.executable),
        model_endpoint="http://localhost:1/v1",
        config_file=config,
        state_dir=state_dir,
        service_dir=service_dir,
        s6_svstat=s6_svstat,
        health_url="http://localhost:1/ready",
        http_get=http_get,
    ).run(expected_revision=second)
    assert bootstrap.ready is True, bootstrap.model_dump_json(indent=2)
    assert bootstrap.install_actions == []

    fake_pi = InstalledPi()
    reconciler = PiAssetReconciler(clone, pi_binary, runner=fake_pi)
    first_result = reconciler.reconcile(first)
    second_result = reconciler.reconcile(second)
    rollback_result = reconciler.rollback()

    assert first_result.verified is True
    assert second_result.verified is True
    assert rollback_result.verified is True
    assert rollback_result.revision == first
    assert reconciler.status().drift == []
    assert any(call[1:3] == ["install", str(second_result.package_path)] for call in fake_pi.calls)
    assert any(call[1:] == ["--mode", "rpc", "--no-session"] for call in fake_pi.calls)
    lock_path = clone / ".agent-team-pi.lock"
    assert lock_path.is_file()
    assert git("check-ignore", "--no-index", "--", lock_path.name, cwd=clone) == lock_path.name
