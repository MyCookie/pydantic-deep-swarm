"""s6 service control with explicit command-path discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence
from ..subprocess_env import sanitized_subprocess_env


class ServiceControlError(RuntimeError):
    """Raised when the supervised Agent Team service cannot be controlled."""


class S6ServiceController:
    """Restart an s6-supervised service without assuming PATH contains /command."""

    def __init__(
        self,
        *,
        service_dir: Path | str | None = None,
        command_candidates: Sequence[Path] | None = None,
        status_command_candidates: Sequence[Path] | None = None,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        configured_service_dir = service_dir or os.getenv("AGENT_TEAM_SERVICE_DIR")
        if configured_service_dir is None:
            raise ServiceControlError(
                "set AGENT_TEAM_SERVICE_DIR or pass service_dir"
            )
        self.service_dir = Path(configured_service_dir)
        configured_command = os.getenv("AGENT_TEAM_S6_SVC")
        defaults = (Path(configured_command),) if configured_command else ()
        self.command_candidates = tuple(defaults if command_candidates is None else command_candidates)
        configured_status = os.getenv("AGENT_TEAM_S6_SVSTAT")
        self.status_command_candidates = tuple(
            (Path(configured_status),) if configured_status else ()
        ) if status_command_candidates is None else tuple(status_command_candidates)
        self.runner = runner

    def _resolve_command(self, name: str = "s6-svc") -> str:
        candidates = self.command_candidates if name == "s6-svc" else self.status_command_candidates
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return str(candidate)
        if candidates:
            raise ServiceControlError(f"configured {name} command is not executable")
        on_path = shutil.which(name)
        if on_path:
            return on_path
        raise ServiceControlError(
            f"{name} was not found in PATH or explicitly configured"
        )

    def preflight(self) -> None:
        """Prove commands and the live supervisor are usable before changing files."""
        if not self.service_dir.is_dir():
            raise ServiceControlError(f"s6 service directory does not exist: {self.service_dir}")
        self._resolve_command()
        command = self._resolve_command("s6-svstat")
        try:
            result = self.runner(
                [command, str(self.service_dir)], check=True,
                capture_output=True, text=True, timeout=5,env=sanitized_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise ServiceControlError(f"s6 supervisor unavailable for {self.service_dir}: {detail}") from exc
        if getattr(result, "returncode", 0) != 0:
            raise ServiceControlError(f"s6 supervisor unavailable for {self.service_dir}: {getattr(result, 'stderr', '')}")
        if not getattr(result, "stdout", "").strip().startswith("up "):
            raise ServiceControlError(f"s6 service is not running: {self.service_dir}")

    def restart(self) -> None:
        """Wait for an actual restart before callers verify the new service."""
        self.preflight()
        command = self._resolve_command()
        try:
            result = self.runner(
                [command, "-r", "-wr", "-T", "10000", str(self.service_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
                env=sanitized_subprocess_env(),
            )
        except PermissionError as exc:
            raise ServiceControlError(
                f"permission denied controlling {self.service_dir}; run the command as root"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
            raise ServiceControlError(
                f"s6 restart failed for {self.service_dir}: {detail or exc}"
            ) from exc
        except OSError as exc:
            raise ServiceControlError(f"unable to restart {self.service_dir}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ServiceControlError(f"s6 restart timed out for {self.service_dir}") from exc
        if getattr(result, "returncode", 0) != 0:
            stderr = getattr(result, "stderr", "")
            raise ServiceControlError(
                f"s6 restart failed for {self.service_dir}: {stderr or result}"
            )
