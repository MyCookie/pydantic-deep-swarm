"""Safe, deterministic reconciliation of Agent Team model configuration."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import yaml
from pathlib import Path


class ConfigReconcileError(RuntimeError):
    """Raised when a managed configuration cannot be reconciled safely."""


def reject_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ConfigReconcileError("managed target or ancestor is a symlink")


def atomic_write(path: Path, content: bytes, *, expected: bytes | None, mode: int = 0o600) -> None:
    """Replace one operator-owned file; report uncertainty after publication."""
    reject_symlinks(path)
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise ConfigReconcileError("concurrent configuration change; retry after coordinating editors")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    committed = False
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if (path.read_bytes() if path.exists() else None) != expected:
            raise ConfigReconcileError("concurrent configuration change before publication")
        os.replace(temporary, path)
        committed = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception as exc:
        error = ConfigReconcileError("configuration commit uncertain after replacement" if committed else "configuration staging failed; previous file preserved")
        error.commit_state = "uncertain" if committed else "not_committed"
        raise error from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class OwnedYamlReconciler:
    """Commit concrete role selections to exactly one external YAML file."""

    def __init__(self, selection, *, state_dir=None, workspace_dir=None):
        self.selection = selection
        self.path = selection.config_file
        self.state_dir = state_dir
        self.workspace_dir = workspace_dir

    def render(self, models: dict[str, str]) -> bytes:
        data = yaml.safe_load(self.path.read_text()) if self.path.exists() else {}
        data = data or {}
        if not self.path.exists():
            data["runtime"] = {"state_dir": str(self.selection.config.runtime.state_dir), "workspace_dir": str(self.selection.config.runtime.workspace_dir)}
        roles = data.setdefault("models", {})
        changed = False
        for role, value in models.items():
            selected = self.selection.config.models[role]
            if selected.model != value or not self.path.exists():
                entry = roles.setdefault(role, {})
                if entry is None:
                    entry = roles[role] = {}
                entry["model"] = value
                if not self.path.exists():
                    entry["base_url"] = selected.base_url
                changed = True
        return yaml.safe_dump(data, sort_keys=False).encode() if changed else self.path.read_bytes()

    def apply(self, models: dict[str, str], *, dry_run: bool = False) -> list[str]:
        from ..config import resolve_configuration
        from ..runtime_boundary import ensure_external_runtime_paths
        path = self.path
        reject_symlinks(self.selection.config_input_path or path)
        reject_symlinks(path)
        ensure_external_runtime_paths(path)
        before = path.read_bytes() if path.exists() else None
        content = self.render(models)
        if dry_run:
            return [str(path)] if content != before else []
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlinks(path)
        lock_path = path.with_name(path.name + ".reconcile.lock")
        reject_symlinks(lock_path)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (path.read_bytes() if path.exists() else None) != before:
                raise ConfigReconcileError("concurrent configuration change before ownership; retry")
            # Re-read and validate under lock through the same canonical loader.
            selection = resolve_configuration(path, state_dir=self.state_dir, workspace_dir=self.workspace_dir,allow_missing_named=True)
            if selection.config != self.selection.config:
                raise ConfigReconcileError("effective configuration changed during reconciliation")
            if content == before:
                return []
            data = yaml.safe_load(content)
            from ..config import Config, effective_models
            data = Config._expand_env_vars(data)
            data["models"] = effective_models(data.get("models", {}))
            runtime = data.setdefault("runtime", {})
            runtime.setdefault("state_dir", self.selection.config.runtime.state_dir)
            if runtime.get("workspace_dir") is None:
                runtime["workspace_dir"] = self.selection.config.runtime.workspace_dir
            candidate = Config(**data)
            ensure_external_runtime_paths(candidate.runtime.state_dir,workspace_dir=candidate.runtime.workspace_dir)
            atomic_write(path, content, expected=before)
            return [str(path)]
        finally:
            os.close(descriptor)


_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_RUNFILE_MODEL_RE = re.compile(
    r'(?P<prefix>(?:export\s+)?LLM_MODEL\s*=\s*"\$\{LLM_MODEL:-)'
    r"(?P<model>[^\"}]+)(?P<suffix>\}\")"
)
_CONFIG_MODEL_RE = re.compile(
    r'(?P<prefix>LLM_MODEL"\s*,\s*")(?P<model>[^\"]+)(?P<suffix>")'
)


def _validate_model(model: str) -> str:
    if not model or not _MODEL_RE.fullmatch(model):
        raise ConfigReconcileError(
            "model IDs may contain only letters, numbers, '.', '_', ':', '/', and '-'"
        )
    return model


def extract_runfile_model(text: str) -> str | None:
    """Extract the default model from an s6 runfile."""
    match = _RUNFILE_MODEL_RE.search(text)
    if match:
        return match.group("model")

    assignment = re.search(
        r'(?:export\s+)?LLM_MODEL\s*=\s*["\']([^"\']+)["\']', text
    )
    return assignment.group(1) if assignment else None


def extract_config_model(text: str) -> str | None:
    """Extract the ``Config.from_env`` fallback model from Python source."""
    match = _CONFIG_MODEL_RE.search(text)
    return match.group("model") if match else None



SwarmConfigReconciler = OwnedYamlReconciler
