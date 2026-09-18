"""Persistent, role-separated memory for Agent Team agents."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..redaction import redact_sensitive_data


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class AgentMemory:
    """Atomic, process-locked memory for one role and optional project scope."""

    CURRENT_SCHEMA_VERSION = 1
    MAX_ITEMS = 100
    MAX_MAP_ENTRIES = 100

    def __init__(self, agent_id: str, memory_dir: Path, scope: str | None = None):
        self.agent_id = self._validate_component(agent_id, "agent_id")
        self.scope = self._validate_component(scope, "scope") if scope is not None else None
        self.memory_root = Path(memory_dir).expanduser().resolve()
        scoped_root = self.memory_root / self.scope if self.scope is not None else self.memory_root
        self.memory_dir = (scoped_root / self.agent_id).resolve()
        if not self.memory_dir.is_relative_to(self.memory_root):
            raise ValueError("memory scope escapes the configured memory directory")
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "memory.json"
        self.lock_file = self.memory_dir / ".memory.lock"
        self._memory = self._read_snapshot()

    @staticmethod
    def _validate_component(value: str, name: str) -> str:
        if not isinstance(value, str) or not _COMPONENT_RE.fullmatch(value):
            raise ValueError(
                f"{name} must match {_COMPONENT_RE.pattern} and contain no path separators"
            )
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _thread_lock_for(path: Path) -> threading.RLock:
        key = str(path)
        with _THREAD_LOCKS_GUARD:
            return _THREAD_LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Lock this memory file across threads and cooperating processes."""
        thread_lock = self._thread_lock_for(self.lock_file)
        with thread_lock:
            with self.lock_file.open("a+b") as handle:
                locker: tuple[str, Any] | None = None
                try:
                    try:
                        import fcntl
                    except ImportError:
                        import msvcrt

                        handle.seek(0)
                        handle.write(b"\0")
                        handle.flush()
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                        locker = ("msvcrt", msvcrt)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                        locker = ("fcntl", fcntl)
                    yield
                finally:
                    if locker is not None:
                        kind, module = locker
                        if kind == "fcntl":
                            module.flock(handle.fileno(), module.LOCK_UN)
                        else:
                            handle.seek(0)
                            module.locking(handle.fileno(), module.LK_UNLCK, 1)

    def _empty_memory(self) -> dict[str, Any]:
        now = self._now()
        return {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "agent_id": self.agent_id,
            "scope": self.scope,
            "created_at": now,
            "updated_at": now,
            "preferences": {},
            "decisions": [],
            "goals": [],
            "context": {},
            "lessons": [],
        }

    @staticmethod
    def _bounded_mapping(value: Any, limit: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        items = [(str(key), copy.deepcopy(item)) for key, item in value.items()]
        return dict(items[-limit:])

    @staticmethod
    def _bounded_records(value: Any, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [copy.deepcopy(item) for item in value if isinstance(item, dict)][-limit:]

    def _normalize_memory(self, value: Any) -> tuple[dict[str, Any], bool]:
        value = redact_sensitive_data(value)
        if not isinstance(value, dict):
            raise ValueError("memory document must be an object")
        stored_agent = value.get("agent_id")
        stored_scope = value.get("scope")
        if stored_agent not in (None, self.agent_id):
            raise ValueError("memory document agent_id does not match its path")
        if stored_scope not in (None, self.scope):
            raise ValueError("memory document scope does not match its path")
        version = value.get("schema_version", 0)
        if not isinstance(version, int) or version < 0 or version > self.CURRENT_SCHEMA_VERSION:
            raise ValueError("unsupported memory schema version")
        now = self._now()
        state = {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "agent_id": self.agent_id,
            "scope": self.scope,
            "created_at": value.get("created_at") if isinstance(value.get("created_at"), str) else now,
            "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else now,
            "preferences": self._bounded_mapping(value.get("preferences"), self.MAX_MAP_ENTRIES),
            "decisions": self._bounded_records(value.get("decisions"), self.MAX_ITEMS),
            "goals": self._bounded_records(value.get("goals"), self.MAX_ITEMS),
            "context": self._bounded_mapping(value.get("context"), self.MAX_MAP_ENTRIES),
            "lessons": self._bounded_records(value.get("lessons"), self.MAX_ITEMS),
        }
        return state, state != value

    def _backup_corrupt_locked(self) -> None:
        if not self.memory_file.exists():
            return
        backup = self.memory_file.with_name(f"{self.memory_file.name}.corrupt.{uuid.uuid4().hex}")
        try:
            os.replace(self.memory_file, backup)
        except OSError:
            pass

    def _read_locked(self) -> tuple[dict[str, Any], bool]:
        if not self.memory_file.exists():
            return self._empty_memory(), False
        try:
            with self.memory_file.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            return self._normalize_memory(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._backup_corrupt_locked()
            return self._empty_memory(), True

    def _fsync_directory(self) -> None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.memory_dir, flags)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _write_locked(self, state: dict[str, Any]) -> None:
        safe_state = redact_sensitive_data(state)
        state.clear()
        state.update(safe_state)
        fd, temporary = tempfile.mkstemp(prefix=".memory.", dir=self.memory_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.memory_file)
            self._fsync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read_snapshot(self) -> dict[str, Any]:
        with self._lock():
            state, needs_write = self._read_locked()
            if needs_write:
                self._write_locked(state)
            self._memory = copy.deepcopy(state)
            return copy.deepcopy(state)

    def _load_memory(self) -> dict[str, Any]:
        """Compatibility alias for callers of the original private loader."""
        return self._read_snapshot()

    def _mutate(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        with self._lock():
            state, _ = self._read_locked()
            mutator(state)
            state, _ = self._normalize_memory(state)
            state["updated_at"] = self._now()
            self._write_locked(state)
            self._memory = copy.deepcopy(state)

    def _save_memory(self) -> None:
        """Compatibility helper that commits the current snapshot atomically."""
        desired = copy.deepcopy(self._memory)

        def replace_state(state: dict[str, Any]) -> None:
            state.clear()
            state.update(desired)

        self._mutate(replace_state)

    def get_preference(self, key: str, default: Any = None) -> Any:
        state = self._read_snapshot()
        return copy.deepcopy(state.get("preferences", {}).get(key, default))

    def set_preference(self, key: str, value: Any) -> None:
        def mutate(state: dict[str, Any]) -> None:
            preferences = state.setdefault("preferences", {})
            preferences[str(key)] = copy.deepcopy(value)
            state["preferences"] = dict(list(preferences.items())[-self.MAX_MAP_ENTRIES:])

        self._mutate(mutate)

    def add_decision(self, decision: str, context: dict[str, Any] | None = None) -> None:
        def mutate(state: dict[str, Any]) -> None:
            decisions = state.setdefault("decisions", [])
            decisions.append({
                "id": uuid.uuid4().hex[:8],
                "decision": str(decision),
                "context": copy.deepcopy(context or {}),
                "timestamp": self._now(),
            })
            state["decisions"] = decisions[-self.MAX_ITEMS:]

        self._mutate(mutate)

    def get_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        state = self._read_snapshot()
        bounded = max(0, int(limit))
        return copy.deepcopy(state.get("decisions", [])[-bounded:] if bounded else [])

    def add_goal(self, goal: str, status: str = "active") -> None:
        def mutate(state: dict[str, Any]) -> None:
            goals = state.setdefault("goals", [])
            for item in goals:
                if item.get("goal") == goal:
                    item["status"] = status
                    item["updated_at"] = self._now()
                    state["goals"] = goals[-self.MAX_ITEMS:]
                    return
            goals.append({
                "id": uuid.uuid4().hex[:8],
                "goal": str(goal),
                "status": str(status),
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            state["goals"] = goals[-self.MAX_ITEMS:]

        self._mutate(mutate)

    def get_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        state = self._read_snapshot()
        goals = state.get("goals", [])
        return copy.deepcopy([item for item in goals if status is None or item.get("status") == status])

    def set_context(self, key: str, value: Any) -> None:
        def mutate(state: dict[str, Any]) -> None:
            context = state.setdefault("context", {})
            context[str(key)] = copy.deepcopy(value)
            state["context"] = dict(list(context.items())[-self.MAX_MAP_ENTRIES:])

        self._mutate(mutate)

    def get_context(self, key: str, default: Any = None) -> Any:
        state = self._read_snapshot()
        return copy.deepcopy(state.get("context", {}).get(key, default))

    def add_lesson(self, lesson: str, category: str = "general") -> None:
        def mutate(state: dict[str, Any]) -> None:
            lessons = state.setdefault("lessons", [])
            if any(item.get("lesson") == lesson and item.get("category") == category for item in lessons):
                return
            lessons.append({
                "id": uuid.uuid4().hex[:8],
                "lesson": str(lesson),
                "category": str(category),
                "timestamp": self._now(),
            })
            state["lessons"] = lessons[-self.MAX_ITEMS:]

        self._mutate(mutate)

    def get_lessons(self, category: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        state = self._read_snapshot()
        lessons = [item for item in state.get("lessons", []) if category is None or item.get("category") == category]
        bounded = max(0, int(limit))
        return copy.deepcopy(lessons[-bounded:] if bounded else [])

    def get_summary(self, limit: int = 8) -> dict[str, Any]:
        """Return an immutable, bounded snapshot suitable for model prompts."""
        state = self._read_snapshot()
        bounded = max(0, int(limit))
        decisions = state.get("decisions", [])[-bounded:] if bounded else []
        lessons = state.get("lessons", [])[-bounded:] if bounded else []
        goals = [item for item in state.get("goals", []) if item.get("status") == "active"][:bounded]
        return copy.deepcopy({
            "agent_id": state["agent_id"],
            "scope": state["scope"],
            "preferences": dict(list(state.get("preferences", {}).items())[:bounded]),
            "recent_decisions": decisions,
            "active_goals": goals,
            "recent_lessons": lessons,
            "decisions_count": len(state.get("decisions", [])),
            "goals_count": len(state.get("goals", [])),
            "lessons_count": len(state.get("lessons", [])),
            "updated_at": state.get("updated_at"),
        })

    def clear(self) -> None:
        def reset(state: dict[str, Any]) -> None:
            state.clear()
            state.update(self._empty_memory())

        self._mutate(reset)
