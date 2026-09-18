"""Runtime storage boundary for Git-managed Agent Team checkouts."""

from __future__ import annotations

import os
from pathlib import Path


class RuntimeBoundaryError(ValueError):
    """Raised when mutable runtime storage would live inside the source tree."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _inside(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is the parent itself or one of its descendants."""
    return path == parent or parent in path.parents


def ensure_external_runtime_paths(
    state_dir: str | Path,
    *,
    workspace_dir: str | Path | None = None,
    repository_root: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve runtime paths and require them to be outside the checkout.

    The source tree is Git-managed desired state. Sessions, databases, memory,
    artifacts, logs, and other mutable instance data belong under the external
    state/workspace roots supplied by deployment configuration. Symlinks are
    resolved before validation so a link back into the checkout is rejected.
    """
    repo = _resolved(
        repository_root
        or os.getenv("AGENT_TEAM_PROJECT_ROOT")
        or Path(__file__).resolve().parents[2]
    )
    state = _resolved(state_dir)
    workspace = _resolved(workspace_dir or state / "workspace")

    violations: list[str] = []
    if _inside(state, repo):
        violations.append(f"state_dir={state}")
    if _inside(workspace, repo):
        violations.append(f"workspace_dir={workspace}")
    if violations:
        raise RuntimeBoundaryError(
            "Runtime storage must be outside the Git-managed repository "
            f"({repo}); invalid paths: {', '.join(violations)}"
        )
    return state, workspace
