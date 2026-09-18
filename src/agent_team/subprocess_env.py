"""Credential-safe environment construction for trusted subprocesses."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTHORIZATION|BEARER|CREDENTIALS?|PASSWORD|PASSWD|PRIVATE_?KEY|SECRETS?|TOKENS?)(?:_|$)",
    re.IGNORECASE,
)


def is_sensitive_env_name(name: str) -> bool:
    """Return whether an environment or structured field name is credential-shaped."""
    return _SENSITIVE_ENV_NAME.search(str(name)) is not None


def sanitized_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without credential-shaped variable names."""
    environment = {
        str(name): str(value)
        for name, value in (os.environ if source is None else source).items()
        if not is_sensitive_env_name(str(name))
    }
    for name, value in (overrides or {}).items():
        if is_sensitive_env_name(str(name)):
            raise ValueError(f"refusing sensitive subprocess environment override: {name}")
        environment[str(name)] = str(value)
    return environment
