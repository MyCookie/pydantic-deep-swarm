"""Deterministic secret redaction for prompts, logs, and durable state."""

from __future__ import annotations

import logging
import os
import re
import traceback
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel

from .subprocess_env import is_sensitive_env_name


REDACTED = "[redacted]"
TModel = TypeVar("TModel", bound=BaseModel)

_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>(?P<key_quote>[\"']?)\b"
    r"(?:[A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*"
    r"|AUTHORIZATION)\b(?P=key_quote)\s*[:=]\s*)"
    r"(?:Bearer\s+)?(?P<value>\[redacted\]|\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PROVIDER_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
)


def _known_secret_values() -> list[str]:
    values = {
        str(value)
        for name, value in os.environ.items()
        if is_sensitive_env_name(name) and isinstance(value, str) and len(value) >= 4
    }
    return sorted(values, key=len, reverse=True)


def _redact_assignment(match: re.Match[str]) -> str:
    """Preserve quoted assignment syntax while replacing only the value."""
    value = match.group("value")
    if value.startswith('"') and value.endswith('"'):
        replacement = f'"{REDACTED}"'
    elif value.startswith("'") and value.endswith("'"):
        replacement = f"'{REDACTED}'"
    else:
        replacement = REDACTED
    return match.group("prefix") + replacement


def redact_sensitive_text(value: Any) -> str:
    """Redact credential-shaped assignments, bearer tokens, and known secrets."""
    text = str(value or "")
    for secret in _known_secret_values():
        text = text.replace(secret, REDACTED)
    text = _ASSIGNMENT_RE.sub(_redact_assignment, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    for pattern in _PROVIDER_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_sensitive_data(value: Any) -> Any:
    """Recursively copy and redact strings plus credential-shaped mapping keys."""
    if isinstance(value, BaseModel):
        return redact_sensitive_data(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if is_sensitive_env_name(str(key))
                else redact_sensitive_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, set):
        return {redact_sensitive_data(item) for item in value}
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def redact_model(model: TModel) -> TModel:
    """Return a validated copy of a Pydantic model with sensitive text removed."""
    return type(model).model_validate(redact_sensitive_data(model))


class RedactingFilter(logging.Filter):
    """Redact a standard logging record before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.msg)
        record.args = redact_sensitive_data(record.args)
        if record.exc_info:
            record.exc_text = redact_sensitive_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        return True


def install_redacting_filter(logger: logging.Logger) -> logging.Logger:
    """Install one redacting filter on a logger and return it."""
    if not any(isinstance(item, RedactingFilter) for item in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
