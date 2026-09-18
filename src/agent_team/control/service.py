"""s6 service control with explicit command-path discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


class ServiceControlError(RuntimeError):
    """Raised when the supervised Agent Team service cannot be controlled."""


class S6ServiceController:
    """Restart an s6-supervised service without assuming PATH contains /command."""

    def __init__(
        self,
        *,
        service_dir: Path | str | None = None,
        command_candidates: Sequence[Path] | None = None,
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
        self.command_candidates = tuple(command_candidates or defaults)
        self.runner = runner

    def _resolve_command(self) -> str:
        on_path = shutil.which("s6-svc")
        if on_path:
            return on_path
        for candidate in self.command_candidates:
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return str(candidate)
        raise ServiceControlError(
            "s6-svc was not found in PATH or configured via AGENT_TEAM_S6_SVC"
        )

    def restart(self) -> None:
        """Restart the service through its live s6 supervise directory."""
        if not self.service_dir.exists():
            raise ServiceControlError(f"s6 service directory does not exist: {self.service_dir}")

        command = self._resolve_command()
        try:
            result = self.runner(
                [command, "-r", str(self.service_dir)],
                check=True,
                capture_output=True,
                text=True,
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
        if getattr(result, "returncode", 0) != 0:
            stderr = getattr(result, "stderr", "")
            raise ServiceControlError(
                f"s6 restart failed for {self.service_dir}: {stderr or result}"
            )
