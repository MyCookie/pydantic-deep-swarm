"""Small durable stores for Agent Team sessions and project checkpoints.

Conversation history is session state. It is not copied into agent memory. Project
briefs, reports, and lifecycle checkpoints are written separately so a restart
can be diagnosed without reconstructing a live model call.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .redaction import redact_sensitive_data


class RuntimeAlreadyRunningError(RuntimeError):
    """Raised when another Agent Team runtime owns the state-directory lease."""


def _read_owner_metadata(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise ValueError("Unusable runtime owner metadata")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Changed runtime owner metadata")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            text = handle.read(65537)
        if len(text) > 65536:
            raise ValueError("Oversized runtime owner metadata")
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    finally:
        os.close(fd)


class RuntimeLease:
    """Exclusive OS lease for the single supported Agent Team runtime process."""

    def __init__(self, lock_path: Path | str):
        supplied_lock = Path(lock_path).expanduser()
        self.lock_path = supplied_lock.parent.resolve() / supplied_lock.name
        self.owner_path = self.lock_path.with_name("runtime-owner.json")
        self._fd: int | None = None
        self._token: str | None = None
        self._pid: int | None = None
        self._stopping = False

    @property
    def is_held(self) -> bool:
        if self._fd is None or self._pid != os.getpid():
            return False
        try:
            descriptor, path = os.fstat(self._fd), self.lock_path.lstat()
            return stat.S_ISREG(path.st_mode) and (descriptor.st_dev, descriptor.st_ino) == (path.st_dev, path.st_ino)
        except OSError:
            return False

    @property
    def generation(self) -> str | None:
        return self._token

    def mark_stopping(self) -> None:
        self._stopping = True

    def check_generation(self, generation: str | None) -> None:
        if not self.is_held or self._stopping or generation is None or generation != self._token:
            raise RuntimeError("Runtime does not own an active write generation")

    def _write_owner(self) -> None:
        payload = {
            "pid": os.getpid(),
            "lock_path": str(self.lock_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "owner_token": self._token,
        }
        fd, temporary = tempfile.mkstemp(prefix=".runtime-owner.", dir=self.owner_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.owner_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def acquire(self) -> "RuntimeLease":
        """Acquire without blocking; duplicate runtimes fail closed."""
        if self._fd is not None:
            raise RuntimeError("Runtime lease is already held by this object")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_symlink():
            raise RuntimeError("Runtime lock must be a regular unaliased file")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            lock_info = os.fstat(fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise RuntimeError("Runtime lock must be a regular unaliased file")
            try:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeAlreadyRunningError(
                        f"Another Agent Team runtime already owns {self.lock_path}"
                    ) from exc
            except ImportError:
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeAlreadyRunningError(
                        f"Another Agent Team runtime already owns {self.lock_path}"
                    ) from exc
            self._fd = fd
            fd = None
            self._token = uuid.uuid4().hex
            self._pid = os.getpid()
            self._stopping = False
            try:
                self._write_owner()
            except Exception:
                self.release()
                raise
            return self
        except Exception:
            if fd is not None:
                os.close(fd)
            raise

    def release(self) -> None:
        """Release the lease and remove only this owner's metadata."""
        fd = self._fd
        if fd is None:
            return
        if self._pid != os.getpid():
            # A fork inherits the open-file description, not runtime ownership.
            # Closing its reference must not unlock or unlink the parent's lease.
            os.close(fd)
            self._fd = None
            self._token = None
            self._pid = None
            return
        try:
            try:
                owner = _read_owner_metadata(self.owner_path)
                if owner.get("owner_token") == self._token:
                    self.owner_path.unlink(missing_ok=True)
            except (OSError, ValueError, UnicodeError):
                pass
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)
            self._fd = None
            self._token = None
            self._pid = None

    def __enter__(self) -> "RuntimeLease":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


class RuntimeLeaseInspection:
    """Guard an existing lock without creating it or writing owner metadata.

    Unsupported locking is unavailable, rather than a claim of free ownership.
    A read-only descriptor and an inode check prevent replacing the inspected lock.
    """

    def __init__(self, lock_path: Path | str):
        self.lock_path = Path(lock_path)
        self.status = "unavailable"
        self.owner: dict[str, Any] | None = None
        self._fd: int | None = None

    def __enter__(self) -> "RuntimeLeaseInspection":
        try:
            import fcntl
            before = self.lock_path.lstat()
            import stat
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return self
            fd = os.open(self.lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            self._fd = fd
            current = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                return self
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.status = "occupied"
            else:
                after = self.lock_path.lstat()
                self.status = "free" if (after.st_dev, after.st_ino) == (current.st_dev, current.st_ino) else "unavailable"
            try:
                value = _read_owner_metadata(self.lock_path.with_name("runtime-owner.json"))
                if isinstance(value, dict):
                    self.owner = {}
                    if type(value.get("pid")) is int and value["pid"] > 0:
                        self.owner["pid"] = value["pid"]
                    if isinstance(value.get("started_at"), str):
                        try:
                            self.owner["started_at"] = datetime.fromisoformat(value["started_at"]).isoformat()
                        except ValueError:
                            pass
            except (OSError, ValueError, UnicodeError):
                pass
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *_: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def still_guarded(self) -> bool:
        if self.status != "free" or self._fd is None:
            return False
        try:
            descriptor, path = os.fstat(self._fd), self.lock_path.lstat()
            return stat.S_ISREG(path.st_mode) and (descriptor.st_dev, descriptor.st_ino) == (path.st_dev, path.st_ino)
        except OSError:
            return False

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_.:")
    return cleaned[:160] or uuid.uuid4().hex[:12]


class SessionStore:
    """Atomic JSON session store with a durable client-key index by scan.

    The service is single-process, so an in-process lock is sufficient for
    read/modify/write operations. Writes use replace() so a crash cannot leave
    a half-written session document.
    """

    def __init__(self, root: Path | str, *, max_messages: int = 200, write_guard: Callable[[], None] | None = None):
        self.write_guard = write_guard
        if self.write_guard:
            self.write_guard()
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self.max_messages = int(max_messages)
        self._lock = threading.RLock()

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_safe_id(session_id)}.json"

    def _write(self, record: dict[str, Any]) -> None:
        if self.write_guard:
            self.write_guard()
        safe_record = redact_sensitive_data(record)
        record.clear()
        record.update(safe_record)
        record["updated_at"] = _now()
        path = self._path(record["session_id"])
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.write_guard:
                self.write_guard()
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _read_path(self, path: Path) -> dict[str, Any] | None:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            return redact_sensitive_data(value) if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read_path(self._path(session_id))

    def get_by_client_key(self, client_key: str) -> dict[str, Any] | None:
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                record = self._read_path(path)
                if record and record.get("client_key") == client_key:
                    return record
        return None

    def create(self, client_key: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if client_key:
                existing = self.get_by_client_key(client_key)
                if existing is not None:
                    return existing
            session_id = uuid.uuid4().hex[:12]
            record = {
                "session_id": session_id,
                "client_key": client_key,
                "status": "created",
                "created_at": _now(),
                "updated_at": _now(),
                "messages": [],
                "metadata": dict(metadata or {}),
                "current_project_id": None,
                "last_brief": None,
                "last_report": None,
            }
            self._write(record)
            return record

    def update(self, session_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            record = self.get(session_id)
            if record is None:
                raise KeyError(session_id)
            record.update(changes)
            self._write(record)
            return record

    def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        max_messages: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self.get(session_id)
            if record is None:
                raise KeyError(session_id)
            messages = list(record.get("messages") or [])
            messages.append(dict(message))
            limit = self.max_messages if max_messages is None else max(1, int(max_messages))
            record["messages"] = messages[-limit:]
            self._write(record)
            return record

    def list_sessions(self, *, deadline: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            for path in sorted(self.root.glob("*.json")):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Session inspection deadline exceeded")
                record = self._read_path(path)
                if record:
                    records.append(record)
            return records

    def prune_before(self, cutoff: datetime, *, deadline: float | None = None) -> int:
        """Remove expired non-processing sessions and return the deletion count."""
        if cutoff.tzinfo is None:
            raise ValueError("session retention cutoff must be timezone-aware")
        removed = 0
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Session retention deadline exceeded")
                record = self._read_path(path)
                if not record or record.get("status") == "processing":
                    continue
                try:
                    updated = datetime.fromisoformat(str(record.get("updated_at")))
                except (TypeError, ValueError):
                    continue
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    if self.write_guard:
                        self.write_guard()
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def recover_incomplete(self, *, deadline: float | None = None) -> int:
        """Mark turns interrupted by a process restart as blocked, never fake success."""
        recovered = 0
        with self._lock:
            for record in self.list_sessions(deadline=deadline):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Session recovery deadline exceeded")
                if record.get("status") != "processing":
                    continue
                record["status"] = "blocked"
                record["last_error"] = "Agent Team service restarted while this project was running."
                self._write(record)
                recovered += 1
        return recovered


class ProjectStore:
    """Durable project boundary/checkpoint files under the Agent Team state root."""

    def __init__(self, root: Path | str, *, write_guard: Callable[[], None] | None = None):
        self.write_guard = write_guard
        if self.write_guard:
            self.write_guard()
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def write_json(self, project_id: str, name: str, payload: Any) -> Path:
        with self._lock:
            if self.write_guard:
                self.write_guard()
            payload = redact_sensitive_data(payload)
            directory = self.root / _safe_id(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{_safe_id(name)}.json"
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True, default=str)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.write_guard:
                    self.write_guard()
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return path

    def read_json(self, project_id: str, name: str) -> Any | None:
        path = self.root / _safe_id(project_id) / f"{_safe_id(name)}.json"
        try:
            with path.open(encoding="utf-8") as handle:
                return redact_sensitive_data(json.load(handle))
        except (OSError, json.JSONDecodeError):
            return None
