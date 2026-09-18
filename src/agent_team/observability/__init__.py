"""Structured logging for agent team runtime."""

import logging
import json
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
import uuid

from ..redaction import install_redacting_filter, redact_sensitive_data


class StructuredLogger:
    """JSON-structured logger with correlation IDs."""

    def __init__(
        self,
        name: str,
        log_dir: Path | None = None,
        *,
        max_bytes: int | None = None,
        backup_count: int = 5,
    ):
        self.name = name
        self.log_dir = log_dir or Path.home() / ".agent-team" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Setup file handler
        log_file = self.log_dir / f"{name}.jsonl"
        if max_bytes is not None:
            if max_bytes < 1 or backup_count < 1:
                raise ValueError("log retention bounds must be positive")
            self.file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
        else:
            self.file_handler = logging.FileHandler(log_file)
        self.file_handler.setFormatter(logging.Formatter('%(message)s'))

        # Setup console handler
        self.console_handler = logging.StreamHandler()
        self.console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        install_redacting_filter(self.logger)
        self.logger.addHandler(self.file_handler)
        self.logger.addHandler(self.console_handler)

    def close(self) -> None:
        """Detach and close only handlers owned by this structured logger."""
        for handler in (self.file_handler, self.console_handler):
            self.logger.removeHandler(handler)
            handler.close()

    def configure_retention(
        self,
        max_bytes: int | None,
        backup_count: int,
        *,
        log_dir: Path | None = None,
    ) -> None:
        """Move logs to configured state storage and apply optional rotation."""
        target_dir = (log_dir or self.log_dir).expanduser().resolve()
        if target_dir == self.log_dir.resolve() and max_bytes is None:
            return
        if max_bytes is not None and (max_bytes < 1 or backup_count < 1):
            raise ValueError("log retention bounds must be positive")
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / f"{self.name}.jsonl"
        replacement = (
            RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            if max_bytes is not None
            else logging.FileHandler(log_file)
        )
        replacement.setFormatter(logging.Formatter('%(message)s'))
        self.logger.removeHandler(self.file_handler)
        self.file_handler.close()
        self.log_dir = target_dir
        self.file_handler = replacement
        self.logger.addHandler(self.file_handler)

    def _log(self, level: str, event_type: str, **kwargs):
        """Log a structured event."""
        kwargs = redact_sensitive_data(kwargs)
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "event_type": event_type,
            "logger": self.name,
            **kwargs
        }
        # Remove None values
        record = {k: v for k, v in record.items() if v is not None}

        log_method = getattr(self.logger, level.lower(), self.logger.info)
        log_method(json.dumps(record))

    def debug(self, event_type: str, **kwargs):
        self._log("DEBUG", event_type, **kwargs)

    def info(self, event_type: str, **kwargs):
        self._log("INFO", event_type, **kwargs)

    def warning(self, event_type: str, **kwargs):
        self._log("WARNING", event_type, **kwargs)

    def error(self, event_type: str, **kwargs):
        self._log("ERROR", event_type, **kwargs)

    # Convenience methods for common events
    def session_started(self, session_id: str, **kwargs):
        self.info("session_started", session_id=session_id, **kwargs)

    def session_ended(self, session_id: str, **kwargs):
        self.info("session_ended", session_id=session_id, **kwargs)

    def agent_created(self, agent_id: str, role: str, **kwargs):
        self.info("agent_created", agent_id=agent_id, role=role, **kwargs)

    def agent_destroyed(self, agent_id: str, **kwargs):
        self.info("agent_destroyed", agent_id=agent_id, **kwargs)

    def task_assigned(self, task_id: str, agent_id: str, **kwargs):
        self.info("task_assigned", task_id=task_id, agent_id=agent_id, **kwargs)

    def task_completed(self, task_id: str, status: str, **kwargs):
        self.info("task_completed", task_id=task_id, status=status, **kwargs)

    def task_failed(self, task_id: str, error: str, **kwargs):
        self.error("task_failed", task_id=task_id, error=error, **kwargs)

    def task_cancelled(self, task_id: str, **kwargs):
        self.info("task_cancelled", task_id=task_id, **kwargs)

    def team_created(self, team_id: str, members: list[str], **kwargs):
        self.info("team_created", team_id=team_id, members=members, **kwargs)

    def team_dissolved(self, team_id: str, **kwargs):
        self.info("team_dissolved", team_id=team_id, **kwargs)

    def brief_created(self, brief_id: str, **kwargs):
        self.info("brief_created", brief_id=brief_id, **kwargs)

    def report_created(self, report_id: str, status: str, **kwargs):
        self.info("report_created", report_id=report_id, status=status, **kwargs)

    def memory_updated(self, agent_id: str, **kwargs):
        self.info("memory_updated", agent_id=agent_id, **kwargs)

    def knowledge_recorded(self, topic: str, **kwargs):
        self.info("knowledge_recorded", topic=topic, **kwargs)

    def process_spawned(self, pid: int, command: str, **kwargs):
        self.info("process_spawned", pid=pid, command=command, **kwargs)

    def process_terminated(self, pid: int, status: str, **kwargs):
        self.info("process_terminated", pid=pid, status=status, **kwargs)


# Global logger instance
_logger: StructuredLogger | None = None


def get_logger(name: str = "agent-team") -> StructuredLogger:
    """Get or create the global logger."""
    global _logger
    if _logger is None:
        _logger = StructuredLogger(name)
    return _logger
