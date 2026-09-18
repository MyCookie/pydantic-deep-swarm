"""Exact-revision reconciliation of first-party Pi assets.

The reconciler treats the repository and the Pi installation as separate state
surfaces. It reads an exact Git commit, validates the package from that commit,
keeps a stable owned release directory, and asks the absolute Pi executable to
load that directory. It never installs Pi, downloads packages, changes Git
refs, or deletes resources outside its ownership markers.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import uuid
from typing import Any, Callable, Literal, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from pydantic import BaseModel, ConfigDict, Field

from .subprocess_env import sanitized_subprocess_env


COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SCOPE_VALUES = ("project-local", "user-global")
MANAGED_OWNER = "agent-team"
MANAGED_MARKER = ".agent-team-managed.json"
RELEASE_MARKER = "RELEASE.json"
STATE_FILE = "state.json"

_FALLBACK_LOCKS: dict[str, threading.RLock] = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()
_LOCK_STATE = threading.local()


class PiReconcileError(RuntimeError):
    """Raised when Pi reconciliation cannot complete safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PiStatus(_StrictModel):
    """Observed state of the owned Pi installation."""

    scope: Literal["project-local", "user-global"]
    settings_path: Path
    managed_root: Path
    installed_revision: str | None = None
    expected_package_path: Path | None = None
    drift: list[str] = Field(default_factory=list)

    @property
    def synchronized(self) -> bool:
        return self.installed_revision is not None and not self.drift


class PiReconcileResult(_StrictModel):
    """Result of one reconcile or rollback operation."""

    revision: str
    scope: Literal["project-local", "user-global"]
    package_path: Path
    previous_revision: str | None = None
    changed: bool = False
    verified: bool = False
    drift: list[str] = Field(default_factory=list)
    pi_version: str | None = None
    dry_run: bool = False


class _ReconcileState(_StrictModel):
    """Owned durable state used for drift detection and rollback."""

    schema_version: Literal[1] = 1
    owner: Literal["agent-team"] = MANAGED_OWNER
    scope: Literal["project-local", "user-global"]
    current_revision: str
    last_known_good_revision: str | None = None
    history: list[str] = Field(default_factory=list)
    package_path: Path
    package_tree_sha256: str
    settings_unmanaged_sha256: str
    updated_at: str


Runner = Callable[..., Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    """Hash every entry in a package using stable names and file contents."""
    digest = hashlib.sha256()
    if not root.is_dir():
        raise PiReconcileError(f"package directory does not exist: {root}")
    entries = sorted(path for path in root.rglob("*") if path != root)
    for path in entries:
        relative = path.relative_to(root).as_posix()
        digest.update(b"path\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"directory\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise PiReconcileError(f"unsupported package entry: {path}")
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write JSON atomically in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _extract_tar_safely(archive: bytes, destination: Path) -> Path:
    """Extract only regular files and directories below the archive's `pi/`."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        members = handle.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise PiReconcileError(f"unsafe Git archive path: {member.name!r}")
            if name.parts[0] != "pi":
                raise PiReconcileError(f"Git archive contained non-Pi path: {member.name!r}")
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise PiReconcileError(f"Git archive path escapes staging directory: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = handle.extractfile(member)
                if source is None:
                    raise PiReconcileError(f"unable to read Git archive member: {member.name!r}")
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            else:
                raise PiReconcileError(
                    f"Git archive contains unsupported member type: {member.name!r}"
                )
    package_root = destination / "pi"
    if not package_root.is_dir():
        raise PiReconcileError("trusted revision does not contain a pi/ package")
    return package_root


def _load_manifest_validator(package_root: Path):
    validator_path = package_root / "manifest_validator.py"
    if not validator_path.is_file():
        raise PiReconcileError(f"trusted Pi package is missing {validator_path.name}")
    module_name = f"_agent_team_manifest_validator_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, validator_path)
    if spec is None or spec.loader is None:
        raise PiReconcileError(f"cannot load manifest validator: {validator_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PiReconcileError(f"manifest validator failed to load: {exc}") from exc
    validate = getattr(module, "validate_manifest", None)
    if not callable(validate):
        raise PiReconcileError("trusted manifest validator has no validate_manifest function")
    return validate


def _validate_package(package_root: Path) -> dict[str, Any]:
    try:
        manifest = _load_manifest_validator(package_root)(package_root / "manifest.json")
    except PiReconcileError:
        raise
    except Exception as exc:
        raise PiReconcileError(f"trusted Pi manifest validation failed: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PiReconcileError("trusted Pi manifest validator returned a non-object")
    return manifest


class PiAssetReconciler:
    """Reconcile one exact Git revision into a Pi scope."""

    def __init__(
        self,
        repository_root: Path | str,
        pi_binary: Path | str,
        *,
        scope: Literal["project-local", "user-global"] = "project-local",
        user_home: Path | str | None = None,
        command_timeout: float = 60.0,
        runner: Runner = subprocess.run,
    ) -> None:
        root = Path(repository_root).expanduser().resolve()
        if scope not in SCOPE_VALUES:
            raise PiReconcileError(f"unsupported Pi scope: {scope!r}")
        if command_timeout <= 0:
            raise PiReconcileError("Pi command timeout must be positive")
        binary = Path(pi_binary).expanduser()
        if not binary.is_absolute():
            raise PiReconcileError("Pi binary must be an absolute path")
        binary = binary.resolve(strict=False)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise PiReconcileError(f"Pi binary is missing or not executable: {binary}")
        self.repository_root = root
        self.pi_binary = binary
        self.scope: Literal["project-local", "user-global"] = scope
        self.user_home = (
            Path(user_home).expanduser().resolve()
            if user_home is not None
            else Path.home().resolve()
        )
        self.command_timeout = command_timeout
        self.runner = runner

    @property
    def settings_path(self) -> Path:
        if self.scope == "project-local":
            return self.repository_root / ".pi" / "settings.json"
        return self.user_home / ".pi" / "agent" / "settings.json"

    @property
    def managed_root(self) -> Path:
        if self.scope == "project-local":
            return self.repository_root / ".pi" / "agent-team"
        return self.user_home / ".pi" / "agent" / "agent-team"

    @property
    def reconcile_lock_path(self) -> Path:
        """Return the lock outside the managed release tree."""
        if self.scope == "project-local":
            return self.repository_root / ".agent-team-pi.lock"
        return self.user_home / ".pi" / "agent" / ".agent-team.lock"

    @contextmanager
    def _reconcile_lock(self):
        """Serialize mutations across threads and independent processes."""
        lock_path = self.reconcile_lock_path
        lock_key = str(lock_path)
        held = getattr(_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _LOCK_STATE.held = held
        if lock_key in held:
            held[lock_key] += 1
            try:
                yield
            finally:
                held[lock_key] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            with _FALLBACK_LOCKS_GUARD:
                thread_lock = _FALLBACK_LOCKS.setdefault(lock_key, threading.RLock())
            with thread_lock:
                held[lock_key] = 1
                try:
                    yield
                finally:
                    held.pop(lock_key, None)
            return

        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[lock_key] = 1
            try:
                yield
            finally:
                held.pop(lock_key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @property
    def state_path(self) -> Path:
        return self.managed_root / STATE_FILE

    def _pi_environment(self) -> dict[str, str]:
        environment = sanitized_subprocess_env()
        if self.scope == "user-global":
            environment["HOME"] = str(self.user_home)
        return environment

    def _run_pi(self, arguments: Sequence[str], *, input_text: str | None = None):
        command = [str(self.pi_binary), *arguments]
        try:
            result = self.runner(
                command,
                cwd=str(self.repository_root),
                env=self._pi_environment(),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PiReconcileError(
                f"Pi command timed out after {self.command_timeout:g}s: {' '.join(arguments)}"
            ) from exc
        except OSError as exc:
            raise PiReconcileError(f"unable to execute Pi binary {self.pi_binary}: {exc}") from exc
        returncode = getattr(result, "returncode", 1)
        if returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            stdout = getattr(result, "stdout", "") or ""
            detail = stderr.strip() or stdout.strip() or f"exit {returncode}"
            raise PiReconcileError(
                f"Pi command failed ({' '.join(arguments)}): {detail}"
            )
        return result

    def _pi_version(self) -> str:
        result = self._run_pi(["--version"])
        output = (getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()
        if not output:
            raise PiReconcileError("Pi --version returned no version")
        return output.splitlines()[0]

    @staticmethod
    def _validate_revision(revision: str) -> str:
        if not isinstance(revision, str) or COMMIT_RE.fullmatch(revision) is None:
            raise PiReconcileError(
                "trusted revision must be a full commit ID (40 or 64 hexadecimal characters)"
            )
        return revision

    def _git(self, arguments: Sequence[str], *, binary: bool = False) -> str | bytes:
        command = ["git", "-C", str(self.repository_root), *arguments]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=not binary,
            )
        except OSError as exc:
            raise PiReconcileError(f"unable to execute git: {exc}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if binary else result.stderr
            detail = (stderr or "").strip() or f"exit {result.returncode}"
            raise PiReconcileError(f"git {' '.join(arguments)} failed: {detail}")
        return result.stdout if binary else result.stdout.strip()

    def _resolve_revision(self, revision: str) -> str:
        revision = self._validate_revision(revision)
        resolved = str(self._git(["rev-parse", "--verify", f"{revision}^{{commit}}"]))
        if resolved != revision:
            raise PiReconcileError(
                f"trusted revision does not resolve exactly to the requested commit: {revision}"
            )
        return resolved

    def _stage_revision(self, revision: str, staging_root: Path) -> Path:
        archive = self._git(["archive", "--format=tar", revision, "--", "pi"], binary=True)
        if not isinstance(archive, bytes):
            raise PiReconcileError("git archive returned unexpected output")
        return _extract_tar_safely(archive, staging_root)

    def _ensure_managed_root(self) -> tuple[bool, bool]:
        """Return (marker_created, root_created) after ownership validation."""
        if self.managed_root.is_symlink():
            raise PiReconcileError(f"refusing symlinked Pi managed root: {self.managed_root}")
        root_created = not self.managed_root.exists()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        marker = self.managed_root / MANAGED_MARKER
        if marker.exists():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PiReconcileError(f"invalid Pi ownership marker: {marker}") from exc
            if not isinstance(data, dict) or data.get("owner") != MANAGED_OWNER or data.get("scope") != self.scope:
                raise PiReconcileError(f"Pi managed root is owned by another scope: {self.managed_root}")
            return False, root_created
        children = [child for child in self.managed_root.iterdir() if child.name != MANAGED_MARKER]
        if children:
            raise PiReconcileError(
                f"refusing to adopt unmarked resources in Pi managed root: {self.managed_root}"
            )
        _write_json_atomic(
            marker,
            {
                "schema_version": 1,
                "owner": MANAGED_OWNER,
                "scope": self.scope,
                "created_at": _now(),
            },
        )
        return True, root_created

    def _release_path(self, revision: str) -> Path:
        return self.managed_root / "releases" / revision

    def _validate_release_marker(self, release_root: Path, revision: str) -> None:
        marker = release_root / RELEASE_MARKER
        if not marker.is_file():
            raise PiReconcileError(f"unmarked Pi release refuses adoption: {release_root}")
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PiReconcileError(f"invalid Pi release marker: {marker}") from exc
        if not isinstance(data, dict):
            raise PiReconcileError(f"invalid Pi release marker: {marker}")
        if data.get("owner") != MANAGED_OWNER or data.get("scope") != self.scope:
            raise PiReconcileError(f"Pi release is not owned by this reconciler: {release_root}")
        if data.get("revision") != revision:
            raise PiReconcileError(f"Pi release marker revision mismatch: {release_root}")

    def _materialize_release(
        self,
        revision: str,
        staged_package: Path,
        manifest: dict[str, Any],
    ) -> tuple[Path, bool]:
        release_root = self._release_path(revision)
        package_path = release_root / "pi"
        source_tree_digest = _tree_digest(staged_package)
        existing = release_root.exists()
        if existing:
            self._validate_release_marker(release_root, revision)
            if package_path.is_dir() and _tree_digest(package_path) == source_tree_digest:
                _validate_package(package_path)
                return package_path, False
            shutil.rmtree(release_root)

        release_root.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.managed_root / f".release-{revision}-{uuid.uuid4().hex}"
        try:
            shutil.copytree(staged_package, temporary / "pi", symlinks=False)
            _validate_package(temporary / "pi")
            _write_json_atomic(
                temporary / RELEASE_MARKER,
                {
                    "schema_version": 1,
                    "owner": MANAGED_OWNER,
                    "scope": self.scope,
                    "revision": revision,
                    "package_path": str(package_path),
                    "package_tree_sha256": source_tree_digest,
                    "manifest_sha256": _sha256_file(temporary / "pi" / "manifest.json"),
                    "created_at": _now(),
                },
            )
            os.replace(temporary, release_root)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return package_path, True

    def _read_settings(self) -> tuple[bytes | None, dict[str, Any]]:
        path = self.settings_path
        if path.is_symlink():
            raise PiReconcileError(f"refusing to modify symlinked Pi settings: {path}")
        if not path.exists():
            return None, {}
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PiReconcileError(f"invalid Pi settings file: {path}") from exc
        if not isinstance(data, dict):
            raise PiReconcileError(f"Pi settings must be a JSON object: {path}")
        packages = data.get("packages", [])
        if not isinstance(packages, list) or any(not isinstance(item, str) for item in packages):
            raise PiReconcileError(f"Pi settings packages must be a string list: {path}")
        return raw, data

    def _settings_base(self) -> Path:
        return self.settings_path.parent

    def _entry_path(self, entry: str) -> Path:
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = self._settings_base() / path
        return path.resolve(strict=False)

    def _is_owned_package_entry(self, entry: str) -> bool:
        try:
            return self._entry_path(entry).is_relative_to(self.managed_root.resolve())
        except (OSError, ValueError):
            return False

    @staticmethod
    def _without_packages(data: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(data)
        result.pop("packages", None)
        return result

    @staticmethod
    def _settings_digest(data: dict[str, Any]) -> str:
        canonical = json.dumps(
            PiAssetReconciler._without_packages(data),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256_bytes(canonical)

    def _write_settings(self, data: dict[str, Any]) -> None:
        _write_json_atomic(self.settings_path, data)

    def _restore_settings(self, raw: bytes | None) -> None:
        path = self.settings_path
        if raw is None:
            if path.exists() and not path.is_symlink():
                path.unlink()
            return
        if path.is_symlink():
            raise PiReconcileError(f"refusing to restore symlinked Pi settings: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".restore.tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _normalize_settings(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        package_path: Path,
    ) -> dict[str, Any]:
        if self._without_packages(after) != self._without_packages(before):
            raise PiReconcileError(
                "Pi install modified settings outside the swarm-owned packages list"
            )
        packages = after.get("packages", [])
        if not isinstance(packages, list) or any(not isinstance(item, str) for item in packages):
            raise PiReconcileError("Pi install produced an invalid packages list")
        expected = package_path.resolve()
        matching = [item for item in packages if self._entry_path(item) == expected]
        if not matching:
            raise PiReconcileError(
                f"Pi install did not register the expected package path: {package_path}"
            )
        unmanaged = [
            item
            for item in packages
            if not self._is_owned_package_entry(item) and self._entry_path(item) != expected
        ]
        normalized = unmanaged + [str(package_path)]
        result = copy.deepcopy(after)
        result["packages"] = normalized
        if result != after:
            self._write_settings(result)
        return result

    def _read_state(self) -> _ReconcileState | None:
        if self.state_path.is_symlink():
            raise PiReconcileError(f"refusing to read symlinked Pi reconciliation state: {self.state_path}")
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = _ReconcileState.model_validate(data)
        except Exception as exc:
            raise PiReconcileError(f"invalid Pi reconciliation state: {self.state_path}") from exc
        if state.scope != self.scope or state.package_path.resolve().is_relative_to(
            self.managed_root.resolve()
        ) is False:
            raise PiReconcileError(f"Pi reconciliation state points outside ownership: {self.state_path}")
        return state

    def _write_state(
        self,
        *,
        revision: str,
        previous: _ReconcileState | None,
        package_path: Path,
        package_tree_sha256: str,
        settings_unmanaged_sha256: str,
    ) -> _ReconcileState:
        prior_current = previous.current_revision if previous is not None else None
        if prior_current and prior_current != revision:
            last_known_good = prior_current
        else:
            last_known_good = previous.last_known_good_revision if previous is not None else None
        history = list(previous.history) if previous is not None else []
        if not history or history[-1] != revision:
            history.append(revision)
        state = _ReconcileState(
            scope=self.scope,
            current_revision=revision,
            last_known_good_revision=last_known_good,
            history=history,
            package_path=package_path.resolve(),
            package_tree_sha256=package_tree_sha256,
            settings_unmanaged_sha256=settings_unmanaged_sha256,
            updated_at=_now(),
        )
        _write_json_atomic(self.state_path, state.model_dump(mode="json"))
        return state

    def _restore_state(self, raw: bytes | None) -> None:
        path = self.state_path
        if path.is_symlink():
            raise PiReconcileError(f"refusing to restore symlinked Pi reconciliation state: {path}")
        if raw is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".restore.tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _cleanup_failed_release(
        self,
        package_path: Path,
        *,
        release_created: bool,
        marker_created: bool,
    ) -> None:
        if release_created:
            release_root = package_path.parent
            if release_root.exists():
                shutil.rmtree(release_root)
        if marker_created and self.managed_root.exists():
            marker = self.managed_root / MANAGED_MARKER
            if marker.exists():
                marker.unlink()
            releases = self.managed_root / "releases"
            if releases.is_dir() and not any(releases.iterdir()):
                releases.rmdir()
            remaining = [child for child in self.managed_root.iterdir()]
            if not remaining:
                self.managed_root.rmdir()

    def status(self) -> PiStatus:
        """Inspect owned files and Pi settings under the reconciliation lock."""
        with self._reconcile_lock():
            return self._status_unlocked()

    def _status_unlocked(self) -> PiStatus:
        """Inspect owned files and Pi settings without acquiring the lock."""
        drift: list[str] = []
        installed_revision: str | None = None
        expected_package_path: Path | None = None
        marker = self.managed_root / MANAGED_MARKER
        if self.managed_root.is_symlink():
            drift.append("Pi managed root is a symlink")
        elif self.managed_root.exists():
            try:
                marker_data = json.loads(marker.read_text(encoding="utf-8"))
                if (
                    not isinstance(marker_data, dict)
                    or marker_data.get("owner") != MANAGED_OWNER
                    or marker_data.get("scope") != self.scope
                ):
                    drift.append("Pi managed root ownership marker is invalid")
            except FileNotFoundError:
                drift.append("Pi managed root ownership marker is missing")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                drift.append("Pi managed root ownership marker is invalid")

        try:
            state = self._read_state()
        except PiReconcileError as exc:
            state = None
            drift.append(str(exc))
        if state is None:
            drift.append("no Pi reconciliation state")
        else:
            installed_revision = state.current_revision
            expected_package_path = state.package_path.resolve()
            try:
                if not expected_package_path.is_relative_to(self.managed_root.resolve()):
                    drift.append("state package path is outside the swarm-owned root")
                if not expected_package_path.is_dir():
                    drift.append("installed package directory is missing")
                else:
                    release_root = expected_package_path.parent
                    self._validate_release_marker(release_root, state.current_revision)
                    if _tree_digest(expected_package_path) != state.package_tree_sha256:
                        drift.append("installed package content drifted")
                    _validate_package(expected_package_path)
            except PiReconcileError as exc:
                drift.append(str(exc))
            except OSError as exc:
                drift.append(f"unable to inspect installed package: {exc}")

            try:
                _, settings = self._read_settings()
                packages = settings.get("packages", [])
                expected = expected_package_path
                matches = [item for item in packages if self._entry_path(item) == expected]
                if len(matches) != 1:
                    drift.append("Pi settings package registration is missing or duplicated")
                owned_entries = [item for item in packages if self._is_owned_package_entry(item)]
                if any(self._entry_path(item) != expected for item in owned_entries):
                    drift.append("Pi settings contains stale swarm-owned package entries")
                if self._settings_digest(settings) != state.settings_unmanaged_sha256:
                    drift.append("Pi settings outside the packages list drifted")
            except PiReconcileError as exc:
                drift.append(str(exc))

        return PiStatus(
            scope=self.scope,
            settings_path=self.settings_path,
            managed_root=self.managed_root,
            installed_revision=installed_revision,
            expected_package_path=expected_package_path,
            drift=drift,
        )

    def reconcile(
        self,
        revision: str,
        *,
        approve_project: bool = False,
        dry_run: bool = False,
    ) -> PiReconcileResult:
        """Install and verify one exact trusted commit revision under a lock."""
        if dry_run:
            return self._reconcile_unlocked(
                revision,
                approve_project=approve_project,
                dry_run=True,
            )
        with self._reconcile_lock():
            return self._reconcile_unlocked(
                revision,
                approve_project=approve_project,
                dry_run=False,
            )

    def _reconcile_unlocked(
        self,
        revision: str,
        *,
        approve_project: bool = False,
        dry_run: bool = False,
    ) -> PiReconcileResult:
        """Install and verify one exact trusted commit revision without locking."""
        exact_revision = self._resolve_revision(revision)
        pi_version = self._pi_version()
        if self.state_path.is_symlink():
            raise PiReconcileError(f"refusing to read symlinked Pi reconciliation state: {self.state_path}")
        state_before_raw = self.state_path.read_bytes() if self.state_path.exists() else None
        previous_state = self._read_state()
        previous_revision = previous_state.current_revision if previous_state is not None else None
        settings_before_raw, settings_before = self._read_settings()
        marker_created = False
        release_created = False
        package_path: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="agent-team-pi-") as temporary_dir:
                staged_package = self._stage_revision(exact_revision, Path(temporary_dir))
                manifest = _validate_package(staged_package)
                if dry_run:
                    package_path = self._release_path(exact_revision) / "pi"
                    return PiReconcileResult(
                        revision=exact_revision,
                        scope=self.scope,
                        package_path=package_path,
                        previous_revision=previous_revision,
                        changed=previous_revision != exact_revision,
                        verified=False,
                        pi_version=pi_version,
                        dry_run=True,
                    )
                marker_created, _ = self._ensure_managed_root()
                package_path, release_created = self._materialize_release(
                    exact_revision,
                    staged_package,
                    manifest,
                )

            install_arguments = ["install", str(package_path)]
            if self.scope == "project-local":
                install_arguments.append("-l")
            if approve_project and self.scope == "project-local":
                install_arguments.append("--approve")
            self._run_pi(install_arguments)
            _, settings_after = self._read_settings()
            final_settings = self._normalize_settings(
                settings_before,
                settings_after,
                package_path,
            )
            self._run_pi(["--mode", "rpc", "--no-session"], input_text="")
            package_digest = _tree_digest(package_path)
            self._write_state(
                revision=exact_revision,
                previous=previous_state,
                package_path=package_path,
                package_tree_sha256=package_digest,
                settings_unmanaged_sha256=self._settings_digest(final_settings),
            )
            final_status = self.status()
            if final_status.drift:
                raise PiReconcileError(
                    "Pi reconciliation completed with drift: " + "; ".join(final_status.drift)
                )
            return PiReconcileResult(
                revision=exact_revision,
                scope=self.scope,
                package_path=package_path,
                previous_revision=previous_revision,
                changed=previous_revision != exact_revision or release_created,
                verified=True,
                drift=[],
                pi_version=pi_version,
            )
        except Exception:
            try:
                self._restore_settings(settings_before_raw)
            finally:
                try:
                    self._restore_state(state_before_raw)
                finally:
                    if package_path is not None:
                        self._cleanup_failed_release(
                            package_path,
                            release_created=release_created,
                            marker_created=marker_created,
                        )
            raise

    def rollback(self, *, approve_project: bool = False) -> PiReconcileResult:
        """Reconcile the previous known-good revision, if one exists."""
        with self._reconcile_lock():
            state = self._read_state()
            if state is None or state.last_known_good_revision is None:
                raise PiReconcileError("no last-known-good Pi revision is available for rollback")
            return self._reconcile_unlocked(
                state.last_known_good_revision,
                approve_project=approve_project,
                dry_run=False,
            )


__all__ = [
    "PiAssetReconciler",
    "PiReconcileError",
    "PiReconcileResult",
    "PiStatus",
]
