"""Safe, deterministic reconciliation of Agent Team model configuration."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path


class ConfigReconcileError(RuntimeError):
    """Raised when a managed configuration cannot be reconciled safely."""


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


def _atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    if not path.parent.exists():
        raise ConfigReconcileError(f"parent directory does not exist: {path.parent}")

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError as exc:
        raise ConfigReconcileError(f"managed file does not exist: {path}") from exc

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class SwarmConfigReconciler:
    """Reconcile Python defaults and both s6 runfile copies as one operation."""

    def __init__(
        self,
        source_config_path: Path,
        source_runfile_path: Path,
        live_runfile_path: Path,
    ) -> None:
        self.source_config_path = Path(source_config_path)
        self.source_runfile_path = Path(source_runfile_path)
        self.live_runfile_path = Path(live_runfile_path)

    @staticmethod
    def render_runfile(text: str, model: str) -> str:
        model = _validate_model(model)
        replacement = lambda match: (
            f'{match.group("prefix")}{model}{match.group("suffix")}'
        )
        updated, count = _RUNFILE_MODEL_RE.subn(replacement, text, count=1)
        if count:
            return updated

        line = f'export LLM_MODEL="${{LLM_MODEL:-{model}}}"\n'
        exec_match = re.search(r"^exec\s+", text, flags=re.MULTILINE)
        if exec_match:
            return text[: exec_match.start()] + line + text[exec_match.start() :]
        return text.rstrip() + "\n" + line

    @staticmethod
    def render_config_source(text: str, model: str) -> str:
        model = _validate_model(model)
        updated, count = _CONFIG_MODEL_RE.subn(
            lambda match: f'{match.group("prefix")}{model}{match.group("suffix")}',
            text,
            count=1,
        )
        if not count:
            raise ConfigReconcileError(
                "could not find the Config.from_env LLM_MODEL fallback in source"
            )
        return updated

    def _planned_contents(self, model: str) -> dict[Path, str]:
        model = _validate_model(model)
        paths = (
            self.source_config_path,
            self.source_runfile_path,
            self.live_runfile_path,
        )
        contents: dict[Path, str] = {}
        for path in paths:
            if not path.exists():
                raise ConfigReconcileError(f"managed file does not exist: {path}")
            original = path.read_text(encoding="utf-8")
            if path == self.source_config_path:
                contents[path] = self.render_config_source(original, model)
            else:
                contents[path] = self.render_runfile(original, model)
        return contents

    def plan(self, model: str) -> dict[Path, str]:
        """Return only file contents that differ from the requested model."""
        planned = self._planned_contents(model)
        return {
            path: content
            for path, content in planned.items()
            if path.read_text(encoding="utf-8") != content
        }

    def apply(self, model: str, *, dry_run: bool = False) -> list[str]:
        """Apply the reconciliation and return changed paths."""
        changes = self.plan(model)
        if not dry_run:
            for path in changes:
                if not os.access(path.parent, os.W_OK):
                    raise ConfigReconcileError(
                        f"managed file parent is not writable: {path.parent}; run as root"
                    )
            for path, content in changes.items():
                _atomic_write(path, content)
        return [str(path) for path in changes]
