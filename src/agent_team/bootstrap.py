"""Clone-and-stand-up prerequisite verification for Agent Team.

Bootstrap is intentionally not an installer. It validates a cloned repository,
the already-installed runtimes and services, and writable external state. It
never invokes a package manager, clones a repository, installs Pi, or restarts
s6.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .pi_reconciler import _validate_package
from .subprocess_env import sanitized_subprocess_env


COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
PYTHON_VERSION_RE = re.compile(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?")
PI_VERSION_RE = re.compile(r"\b(?:v)?(\d+)\.(\d+)(?:\.(\d+))?\b")
REQUIRED_MODULES = (
    "pydantic",
    "pydantic_ai",
    "httpx",
    "fastapi",
    "uvicorn",
    "click",
    "aiosqlite",
    "yaml",
)
STATE_SUBDIRECTORIES = (
    "config",
    "state",
    "sessions",
    "checkpoints",
    "memory",
    "knowledge",
    "skills",
    "projects",
    "artifacts",
    "logs",
)
TRACKABLE_PATHS = (
    "src/agent_team/bootstrap.py",
    "src/agent_team/engine.py",
    "tests/test_e2e.py",
    "pyproject.toml",
    "uv.lock",
    "s6-service/agent-team/run",
    "pi/manifest.json",
    ".env.example",
)
IGNORED_PATHS = (
    ".env",
    ".agent-team/sessions/session.json",
    ".hermes/state.db",
    "state/projects/project.json",
    "runtime.lock",
    "runtime-owner.json",
    "memory/principal.json",
    "knowledge/knowledge.db",
    "artifacts/result.json",
    "logs/service.log",
    ".pi/agent/settings.json",
    ".pi/agent-team/state.json",
    ".pi/keys.toml",
    "credentials/token.json",
    "secrets/private.key",
    ".venv/bin/python",
    "node_modules/package/index.js",
    "bootstrap/install.sh",
    "downloads/model.whl",
    "get-pip.py",
)
FORBIDDEN_REPOSITORY_FILES = ("get-pip.py",)


class BootstrapError(ValueError):
    """Raised for invalid bootstrap arguments."""


class BootstrapCheck(BaseModel):
    """One bootstrap observation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["passed", "failed", "skipped"]
    detail: str


class BootstrapReport(BaseModel):
    """Complete clone-and-stand-up report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    repository_root: Path
    revision: str | None = None
    expected_revision: str | None = None
    pi_binary: Path
    pi_version: str | None = None
    python_executable: Path
    python_version: str | None = None
    dependencies: dict[str, str] = Field(default_factory=dict)
    model_endpoint: str
    model_ids: list[str] = Field(default_factory=list)
    state_dir: Path
    config_file: Path
    service_dir: Path
    s6_svstat: Path | None = None
    health_url: str
    health_ok: bool = False
    s6_ready: bool = False
    checks: list[BootstrapCheck] = Field(default_factory=list)
    ready: bool = False
    install_actions: list[str] = Field(default_factory=list)
    missing_external_tools: list[str] = Field(default_factory=list)


Runner = Callable[..., Any]
HTTPGetter = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def _absolute_path(value: Path | str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BootstrapError(f"{label} must be an absolute path: {path}")
    return Path(os.path.abspath(os.fspath(path)))


def _command_output(result: Any) -> str:
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return "\n".join(part for part in (stdout, stderr) if part).strip()


def _default_http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except URLError as exc:
        raise BootstrapError(f"HTTP request failed for {url}: {exc.reason}") from exc


def _safe_endpoint(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BootstrapError("model endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BootstrapError("model endpoint must not contain credentials, query, or fragment data")
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return base, base + "/models"


def _readiness_url(value: str) -> str:
    """Normalize a service URL to the readiness endpoint, never liveness."""
    base, _ = _safe_endpoint(value)
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.rsplit("/", 1)[-1] == "health":
        path = path.rsplit("/", 1)[0] + "/ready"
    elif path.rsplit("/", 1)[-1] != "ready":
        path += "/ready"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _json_payload(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"endpoint returned invalid JSON: {exc}") from exc


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_bootstrap_report(path: Path | str, report: BootstrapReport) -> Path:
    """Write a report atomically outside the cloned desired-state tree."""
    destination = Path(path).expanduser()
    _write_json_atomic(destination, report.model_dump(mode="json"))
    return destination


class Bootstrapper:
    """Verify a cloned Agent Team repository without installing dependencies."""

    def __init__(
        self,
        repository_root: Path | str,
        pi_binary: Path | str,
        *,
        python_executable: Path | str | None = None,
        model_endpoint: str | None = None,
        config_file: Path | str | None = None,
        state_dir: Path | str | None = None,
        service_dir: Path | str | None = None,
        s6_svstat: Path | str | None = None,
        health_url: str | None = None,
        api_token: str | None = None,
        timeout: float = 5.0,
        runner: Runner = subprocess.run,
        http_get: HTTPGetter = _default_http_get,
    ) -> None:
        if timeout <= 0:
            raise BootstrapError("bootstrap timeout must be positive")
        self.repository_root = Path(repository_root).expanduser().resolve()
        self.pi_binary = _absolute_path(pi_binary, "Pi binary")
        if python_executable is None:
            configured_python = os.getenv("AGENT_TEAM_PYTHON")
            configured_venv = os.getenv("AGENT_TEAM_VENV")
            if configured_python:
                python_executable = configured_python
            elif configured_venv:
                python_executable = Path(configured_venv).expanduser() / "bin" / "python"
            else:
                raise BootstrapError(
                    "set AGENT_TEAM_PYTHON or AGENT_TEAM_VENV, or pass python_executable"
                )
        self.python_executable = _absolute_path(python_executable, "Python executable")
        self.model_endpoint = model_endpoint or os.getenv(
            "LLM_BASE_URL", "http://model-service:8000/v1"
        )
        self.model_base_url, self.models_url = _safe_endpoint(self.model_endpoint)
        self.config_file = (
            Path(config_file).expanduser().resolve()
            if config_file is not None
            else None
        )
        self.state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir is not None
            else Path(
                os.getenv("AGENT_TEAM_STATE_DIR", str(Path.home() / ".agent-team"))
            ).expanduser().resolve()
        )
        configured_service_dir = service_dir or os.getenv("AGENT_TEAM_SERVICE_DIR")
        if configured_service_dir is None:
            raise BootstrapError(
                "set AGENT_TEAM_SERVICE_DIR or pass service_dir"
            )
        self.service_dir = Path(configured_service_dir).expanduser().resolve()
        self.s6_svstat = (
            _absolute_path(s6_svstat, "s6-svstat") if s6_svstat is not None else None
        )
        self.health_url = _readiness_url(
            health_url
            or os.getenv("AGENT_TEAM_HEALTH_URL", "http://localhost:8080/ready")
        )
        self._api_token = (
            os.getenv("AGENT_TEAM_API_TOKEN", "")
            if api_token is None
            else api_token
        )
        self.timeout = timeout
        self.runner = runner
        self.http_get = http_get

    def _run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        try:
            return self.runner(
                list(command),
                cwd=str(cwd or self.repository_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BootstrapError(f"command timed out: {' '.join(command)}") from exc
        except OSError as exc:
            raise BootstrapError(f"unable to execute {' '.join(command)}: {exc}") from exc

    def _check_git(self, checks: list[BootstrapCheck], expected_revision: str | None) -> str | None:
        try:
            root_result = self._run(["git", "rev-parse", "--show-toplevel"])
        except BootstrapError as exc:
            checks.append(
                BootstrapCheck(
                    name="git revision",
                    status="failed",
                    detail=f"Git unavailable: {exc}",
                )
            )
            return None
        root_output = _command_output(root_result)
        if getattr(root_result, "returncode", 1) != 0 or not root_output:
            checks.append(BootstrapCheck(name="git revision", status="failed", detail="not a Git repository"))
            return None
        actual_root = Path(root_output).resolve()
        if actual_root != self.repository_root:
            checks.append(
                BootstrapCheck(
                    name="git revision",
                    status="failed",
                    detail=f"repository root is {actual_root}, expected {self.repository_root}",
                )
            )
            return None
        head_result = self._run(["git", "rev-parse", "--verify", "HEAD^{commit}"])
        revision = _command_output(head_result)
        status_result = self._run(["git", "status", "--porcelain=v1", "--untracked-files=all"])
        dirty = _command_output(status_result)
        if getattr(head_result, "returncode", 1) != 0 or COMMIT_RE.fullmatch(revision) is None:
            checks.append(BootstrapCheck(name="git revision", status="failed", detail="clone has no full commit revision"))
            return None
        if expected_revision is not None and revision != expected_revision:
            checks.append(
                BootstrapCheck(
                    name="git revision",
                    status="failed",
                    detail=f"HEAD {revision} does not match expected {expected_revision}",
                )
            )
            return revision
        if dirty:
            checks.append(
                BootstrapCheck(
                    name="git revision",
                    status="failed",
                    detail="clone worktree is not clean",
                )
            )
            return revision
        checks.append(
            BootstrapCheck(
                name="git revision",
                status="passed",
                detail=f"clean exact revision {revision}",
            )
        )
        return revision

    def _check_boundary(self, checks: list[BootstrapCheck]) -> None:
        policy_file = self.repository_root / ".gitignore"
        errors: list[str] = []
        if not policy_file.is_file():
            errors.append(".gitignore is missing")
        else:
            try:
                for path in TRACKABLE_PATHS:
                    result = self._run(["git", "check-ignore", "--no-index", "-q", "--", path])
                    if getattr(result, "returncode", 1) == 0:
                        errors.append(f"desired-state path is incorrectly ignored: {path}")
                for path in IGNORED_PATHS:
                    result = self._run(["git", "check-ignore", "--no-index", "-q", "--", path])
                    if getattr(result, "returncode", 1) != 0:
                        errors.append(f"runtime/external path is not ignored: {path}")
            except BootstrapError as exc:
                errors.append(f"Git ignore policy could not be checked: {exc}")
        present_blobs = [
            str(self.repository_root / name)
            for name in FORBIDDEN_REPOSITORY_FILES
            if (self.repository_root / name).exists()
        ]
        if present_blobs:
            errors.append("downloaded bootstrap/install blobs must be outside the repository: " + ", ".join(present_blobs))
        checks.append(
            BootstrapCheck(
                name="repository boundary",
                status="failed" if errors else "passed",
                detail="; ".join(errors) if errors else "desired state is trackable and runtime/external state is ignored",
            )
        )

    def _check_pi(self, checks: list[BootstrapCheck]) -> str | None:
        if not self.pi_binary.is_file() or not os.access(self.pi_binary, os.X_OK):
            checks.append(
                BootstrapCheck(
                    name="Pi version",
                    status="failed",
                    detail=f"preinstalled Pi binary is missing or not executable: {self.pi_binary}",
                )
            )
            return None
        result = self._run([str(self.pi_binary), "--version"])
        output = _command_output(result)
        match = PI_VERSION_RE.search(output)
        if getattr(result, "returncode", 1) != 0 or match is None:
            checks.append(
                BootstrapCheck(
                    name="Pi version",
                    status="failed",
                    detail=output or "Pi --version failed",
                )
            )
            return None
        version = output.splitlines()[0]
        checks.append(BootstrapCheck(name="Pi version", status="passed", detail=version))
        return version

    def _check_python(self, checks: list[BootstrapCheck]) -> tuple[str | None, dict[str, str]]:
        if not self.python_executable.is_file() or not os.access(self.python_executable, os.X_OK):
            checks.append(
                BootstrapCheck(
                    name="Python environment",
                    status="failed",
                    detail=f"Python executable is missing or not executable: {self.python_executable}",
                )
            )
            return None, {}
        version_result = self._run([str(self.python_executable), "--version"])
        version_output = _command_output(version_result)
        version_match = PYTHON_VERSION_RE.search(version_output)
        if getattr(version_result, "returncode", 1) != 0 or version_match is None:
            checks.append(
                BootstrapCheck(
                    name="Python environment",
                    status="failed",
                    detail=version_output or "Python --version failed",
                )
            )
            return None, {}
        major, minor = int(version_match.group(1)), int(version_match.group(2))
        if (major, minor) < (3, 11):
            checks.append(
                BootstrapCheck(
                    name="Python environment",
                    status="failed",
                    detail=f"Python {major}.{minor} is below the project's >=3.11 requirement",
                )
            )
            return None, {}
        environment = sanitized_subprocess_env()
        source = str(self.repository_root / "src")
        environment["PYTHONPATH"] = source + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )
        probe = (
            "import importlib, json, sys; "
            f"names={list(REQUIRED_MODULES)!r}; found={{}}; missing=[]; "
            "\nfor name in names:\n"
            "  try:\n    module=importlib.import_module(name); found[name]=getattr(module, '__version__', 'installed')\n"
            "  except Exception as exc:\n    missing.append(f'{name}: {exc}')\n"
            "print(json.dumps({'python': sys.version.split()[0], 'found': found, 'missing': missing}, sort_keys=True)); "
            "raise SystemExit(1 if missing else 0)"
        )
        dependency_result = self._run(
            [str(self.python_executable), "-c", probe],
            env=environment,
        )
        dependency_output = _command_output(dependency_result)
        dependency_data: dict[str, Any] = {}
        for line in reversed(dependency_output.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                dependency_data = parsed
                break
        dependencies = {
            str(name): str(version)
            for name, version in (dependency_data.get("found", {}) or {}).items()
        }
        missing = dependency_data.get("missing", []) or []
        if getattr(dependency_result, "returncode", 1) != 0 or missing:
            detail = "missing dependencies: " + ", ".join(str(item) for item in missing)
            checks.append(BootstrapCheck(name="Python environment", status="failed", detail=detail))
            return f"{major}.{minor}", dependencies
        checks.append(
            BootstrapCheck(
                name="Python environment",
                status="passed",
                detail=f"Python {major}.{minor} with {len(dependencies)} required modules",
            )
        )
        return dependency_data.get("python", f"{major}.{minor}"), dependencies

    def _check_model_endpoint(self, checks: list[BootstrapCheck]) -> list[str]:
        headers: dict[str, str] = {}
        api_key = os.getenv("LLM_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            status, raw = self.http_get(self.models_url, headers, self.timeout)
            payload = _json_payload(raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            model_ids = [
                str(item.get("id"))
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
            ] if isinstance(data, list) else []
            if status != 200 or not model_ids:
                checks.append(
                    BootstrapCheck(
                        name="model endpoint",
                        status="failed",
                        detail=f"GET {self.models_url} returned HTTP {status} with no model IDs",
                    )
                )
                return []
            checks.append(
                BootstrapCheck(
                    name="model endpoint",
                    status="passed",
                    detail=f"GET {self.models_url} advertised {', '.join(model_ids)}",
                )
            )
            return model_ids
        except (BootstrapError, OSError, ValueError) as exc:
            checks.append(BootstrapCheck(name="model endpoint", status="failed", detail=str(exc)))
            return []

    def _check_state(self, checks: list[BootstrapCheck]) -> None:
        try:
            if self.state_dir.is_symlink():
                raise BootstrapError(f"state directory must not be a symlink: {self.state_dir}")
            if self.state_dir.resolve().is_relative_to(self.repository_root.resolve()):
                raise BootstrapError("state directory must be outside the cloned repository")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            for name in STATE_SUBDIRECTORIES:
                directory = self.state_dir / name
                if directory.is_symlink():
                    raise BootstrapError(f"state subdirectory must not be a symlink: {directory}")
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=directory,
                    prefix=".bootstrap-write-",
                    delete=False,
                ) as handle:
                    probe = Path(handle.name)
                    handle.write(b"bootstrap-write-probe")
                    handle.flush()
                    os.fsync(handle.fileno())
                probe.unlink()
            checks.append(
                BootstrapCheck(
                    name="writable state directories",
                    status="passed",
                    detail=f"verified {len(STATE_SUBDIRECTORIES)} directories outside the clone",
                )
            )
        except (OSError, BootstrapError) as exc:
            checks.append(BootstrapCheck(name="writable state directories", status="failed", detail=str(exc)))

    def _check_manifest(self, checks: list[BootstrapCheck]) -> None:
        package_root = self.repository_root / "pi"
        try:
            _validate_package(package_root)
            checks.append(
                BootstrapCheck(
                    name="Pi asset manifest",
                    status="passed",
                    detail=f"validated {package_root / 'manifest.json'}",
                )
            )
        except Exception as exc:
            checks.append(BootstrapCheck(name="Pi asset manifest", status="failed", detail=str(exc)))

    def _check_config(self, checks: list[BootstrapCheck]) -> Path:
        path = self.config_file or (self.state_dir / "config" / "config.yaml")
        try:
            config_exists = path.exists()
            config = Config.load(path) if config_exists else Config.from_env()
            required_roles = ("principal", "manager", "worker", "curator")
            missing = [
                role
                for role in required_roles
                if not config.models.get(role)
                or not config.models[role].model
                or not config.models[role].base_url
            ]
            configured_state = config.runtime.state_dir.expanduser().resolve()
            if config_exists and configured_state != self.state_dir.resolve():
                missing.append(
                    f"runtime.state_dir={configured_state} does not match bootstrap state_dir={self.state_dir.resolve()}"
                )
            if missing:
                raise BootstrapError("missing or inconsistent configuration: " + ", ".join(missing))
            detail = f"validated {path}" if config_exists else "validated environment fallback configuration"
            checks.append(BootstrapCheck(name="configuration", status="passed", detail=detail))
        except Exception as exc:
            checks.append(BootstrapCheck(name="configuration", status="failed", detail=str(exc)))
        return path

    def _find_s6_svstat(self) -> Path | None:
        if self.s6_svstat is not None:
            return self.s6_svstat
        found = shutil.which("s6-svstat")
        return Path(found).resolve() if found else None

    def _check_s6(self, checks: list[BootstrapCheck]) -> Path | None:
        command = self._find_s6_svstat()
        if command is None or not command.is_file() or not os.access(command, os.X_OK):
            checks.append(BootstrapCheck(name="s6 service", status="failed", detail="s6-svstat is unavailable"))
            return None
        if not self.service_dir.is_dir():
            checks.append(
                BootstrapCheck(
                    name="s6 service",
                    status="failed",
                    detail=f"service directory is missing: {self.service_dir}",
                )
            )
            return command
        result = self._run([str(command), "-o", "up", str(self.service_dir)])
        output = _command_output(result)
        normalized = output.lower()
        ready = getattr(result, "returncode", 1) == 0 and (
            "up" in normalized or normalized in {"true", "1"}
        ) and "down" not in normalized
        checks.append(
            BootstrapCheck(
                name="s6 service",
                status="passed" if ready else "failed",
                detail=output or f"s6-svstat exited {getattr(result, 'returncode', 1)}",
            )
        )
        return command

    def _check_health(self, checks: list[BootstrapCheck]) -> bool:
        try:
            headers = (
                {"Authorization": f"Bearer {self._api_token}"}
                if self._api_token
                else {}
            )
            status, raw = self.http_get(self.health_url, headers, self.timeout)
            payload = _json_payload(raw)
            healthy = (
                status == 200
                and isinstance(payload, dict)
                and payload.get("status") == "ok"
                and payload.get("ready") is True
            )
            checks.append(
                BootstrapCheck(
                    name="Agent Team health",
                    status="passed" if healthy else "failed",
                    detail=f"GET {self.health_url} returned HTTP {status}",
                )
            )
            return healthy
        except (BootstrapError, OSError, ValueError) as exc:
            checks.append(BootstrapCheck(name="Agent Team health", status="failed", detail=str(exc)))
            return False

    def run(self, *, expected_revision: str | None = None) -> BootstrapReport:
        """Run every bootstrap check and return a non-installing report."""
        if expected_revision is not None and COMMIT_RE.fullmatch(expected_revision) is None:
            raise BootstrapError("expected revision must be a full 40- or 64-character hexadecimal commit ID")
        checks: list[BootstrapCheck] = []
        if not self.repository_root.is_dir():
            raise BootstrapError(f"repository root is not a directory: {self.repository_root}")
        required_sources = (self.repository_root / "pyproject.toml", self.repository_root / "src" / "agent_team")
        missing_sources = [str(path) for path in required_sources if not path.exists()]
        if missing_sources:
            checks.append(BootstrapCheck(name="repository layout", status="failed", detail="missing: " + ", ".join(missing_sources)))
        else:
            checks.append(BootstrapCheck(name="repository layout", status="passed", detail="clone contains project source and pyproject.toml"))
        self._check_boundary(checks)
        revision = self._check_git(checks, expected_revision)
        pi_version = self._check_pi(checks)
        python_version, dependencies = self._check_python(checks)
        model_ids = self._check_model_endpoint(checks)
        self._check_manifest(checks)
        self._check_state(checks)
        config_file = self._check_config(checks)
        s6_command = self._check_s6(checks)
        health_ok = self._check_health(checks)
        ready = bool(checks) and all(check.status == "passed" for check in checks)
        missing_external_tools = []
        if shutil.which("git") is None or any(
            check.name == "git revision"
            and check.status == "failed"
            and "git unavailable" in check.detail.lower()
            for check in checks
        ):
            missing_external_tools.append("git")
        missing_external_tools.extend(
            label
            for check_name, label in (
                ("Pi version", "pi"),
                ("Python environment", "python"),
                ("s6 service", "s6-svstat"),
            )
            if any(check.name == check_name and check.status == "failed" for check in checks)
        )
        return BootstrapReport(
            repository_root=self.repository_root,
            revision=revision,
            expected_revision=expected_revision,
            pi_binary=self.pi_binary,
            pi_version=pi_version,
            python_executable=self.python_executable,
            python_version=python_version,
            dependencies=dependencies,
            model_endpoint=self.model_base_url,
            model_ids=model_ids,
            state_dir=self.state_dir,
            config_file=config_file,
            service_dir=self.service_dir,
            s6_svstat=s6_command,
            health_url=self.health_url,
            health_ok=health_ok,
            s6_ready=any(check.name == "s6 service" and check.status == "passed" for check in checks),
            checks=checks,
            ready=ready,
            install_actions=[],
            missing_external_tools=missing_external_tools,
        )


__all__ = [
    "BootstrapCheck",
    "BootstrapError",
    "BootstrapReport",
    "Bootstrapper",
    "STATE_SUBDIRECTORIES",
    "write_bootstrap_report",
]
