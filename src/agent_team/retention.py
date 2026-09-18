"""Opt-in durable-state retention without touching workspace deliverables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .config import RetentionConfig
from .persistence import SessionStore

if TYPE_CHECKING:
    from .memory.knowledge import KnowledgeStore


@dataclass(frozen=True)
class RetentionSweepResult:
    sessions: int = 0
    projects: int = 0
    artifacts: int = 0
    memory_scopes: int = 0
    knowledge_records: int = 0


def _cutoff(now: datetime, days: float | None) -> datetime | None:
    if days is None:
        return None
    return now - timedelta(days=days)


def _latest_mtime(directory: Path) -> datetime | None:
    """Return the latest tree mtime, skipping trees containing symlinks."""
    latest: float | None = None
    for root, directories, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        entries = [*(root_path / name for name in directories), *(root_path / name for name in files)]
        if any(entry.is_symlink() for entry in entries):
            return None
        for entry in [root_path, *entries]:
            try:
                modified = entry.stat().st_mtime
            except OSError:
                return None
            latest = modified if latest is None else max(latest, modified)
    return datetime.fromtimestamp(latest, tz=timezone.utc) if latest is not None else None


def _prune_directories(root: Path, cutoff: datetime | None) -> int:
    if cutoff is None or not root.is_dir() or root.is_symlink():
        return 0
    root_resolved = root.resolve()
    removed = 0
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            if not directory.resolve().is_relative_to(root_resolved):
                continue
        except OSError:
            continue
        modified = _latest_mtime(directory)
        if modified is not None and modified < cutoff:
            shutil.rmtree(directory)
            removed += 1
    return removed


class DurableRetention:
    """Apply explicitly configured retention to Agent Team-owned durable state."""

    def __init__(self, state_dir: Path | str, config: RetentionConfig):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.config = config

    def sweep(
        self,
        *,
        session_store: SessionStore,
        knowledge_store: KnowledgeStore | None = None,
        now: datetime | None = None,
    ) -> RetentionSweepResult:
        observed_now = now or datetime.now(timezone.utc)
        if observed_now.tzinfo is None:
            raise ValueError("retention sweep time must be timezone-aware")

        session_cutoff = _cutoff(observed_now, self.config.session_max_age_days)
        project_cutoff = _cutoff(observed_now, self.config.project_max_age_days)
        memory_cutoff = _cutoff(observed_now, self.config.memory_max_age_days)
        knowledge_cutoff = _cutoff(observed_now, self.config.knowledge_max_age_days)

        sessions = session_store.prune_before(session_cutoff) if session_cutoff else 0
        projects = _prune_directories(self.state_dir / "projects", project_cutoff)
        artifacts = _prune_directories(self.state_dir / "artifacts", project_cutoff)
        memory_scopes = _prune_directories(self.state_dir / "memory", memory_cutoff)
        knowledge_records = (
            knowledge_store.prune_before(knowledge_cutoff)
            if knowledge_store is not None and knowledge_cutoff is not None
            else 0
        )
        return RetentionSweepResult(
            sessions=sessions,
            projects=projects,
            artifacts=artifacts,
            memory_scopes=memory_scopes,
            knowledge_records=knowledge_records,
        )
