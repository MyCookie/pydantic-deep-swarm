"""Configuration management for the Agent Team runtime."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Configuration for a single model role."""

    model: str | None = None
    base_url: str | None = None


class RuntimeConfig(BaseModel):
    """Runtime configuration with hard lifecycle bounds."""

    state_dir: Path = Field(default_factory=lambda: Path.home() / ".agent-team")
    workspace_dir: Path | None = None
    max_workers: int = Field(default=6, ge=1)
    max_concurrent_workers: int = Field(default=3, ge=1)
    nesting_depth: int = Field(default=0, ge=0)
    max_turns: int = Field(default=10, ge=1)
    worker_timeout_seconds: float = 300
    team_timeout_seconds: float = 1800
    max_dynamic_roles: int = Field(default=3, ge=0)
    report_repair_attempts: int = 1
    terminal_retention_seconds: int = 3600


class MemoryConfig(BaseModel):
    """Memory and knowledge configuration."""

    enabled: bool = True
    shared_knowledge: bool = True
    curator_enabled: bool = True
    recent_items: int = 8


class ToolsConfig(BaseModel):
    """Worker tool configuration and safety limits."""

    skills: bool = False
    mcp: bool = False
    enabled: bool = True
    max_file_bytes: int = 1_000_000
    max_tool_calls: int = 8


class RetentionConfig(BaseModel):
    """Optional durable-state expiry and bounded-history controls."""

    max_session_messages: int = Field(default=200, ge=1)
    session_max_age_days: float | None = Field(default=None, gt=0)
    project_max_age_days: float | None = Field(default=None, gt=0)
    knowledge_max_age_days: float | None = Field(default=None, gt=0)
    memory_max_age_days: float | None = Field(default=None, gt=0)
    log_max_bytes: int | None = Field(default=None, gt=0)
    log_backup_count: int = Field(default=5, ge=1)


class Config(BaseModel):
    """Full Agent Team configuration."""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    @classmethod
    def from_env(cls, env_prefix: str = "") -> "Config":
        """Load the YAML configuration when present, otherwise use environment defaults.

        Environment variables remain a bootstrap fallback for installations without
        a config file. Once the documented YAML file exists, its role-specific
        model and runtime values are authoritative.
        """
        def environment_value(name: str) -> str | None:
            return os.getenv(f"{env_prefix}{name}") or os.getenv(name)

        state_override = environment_value("AGENT_TEAM_STATE_DIR")
        home_override = environment_value("AGENT_TEAM_HOME")
        default_state_dir = (
            Path(state_override).expanduser()
            if state_override
            else (
                Path(home_override).expanduser() / ".agent-team"
                if home_override
                else Path.home() / ".agent-team"
            )
        )
        explicit_path = environment_value("AGENT_TEAM_CONFIG_FILE")
        default_path = default_state_dir / "config" / "config.yaml"
        config_path = Path(explicit_path).expanduser() if explicit_path else default_path
        if config_path.exists():
            return cls.from_file(config_path)

        workspace_override = environment_value("AGENT_TEAM_WORKSPACE_DIR")
        runtime_values: dict[str, Any] = {}
        if state_override:
            runtime_values["state_dir"] = Path(state_override).expanduser()
        elif home_override:
            runtime_values["state_dir"] = Path(home_override).expanduser() / ".agent-team"
        if workspace_override:
            runtime_values["workspace_dir"] = Path(workspace_override).expanduser()

        base_url = os.getenv(f"{env_prefix}LLM_BASE_URL", "http://model-service:8000/v1")
        model_name = os.getenv(f"{env_prefix}LLM_MODEL", "auto")
        principal_model = os.getenv(f"{env_prefix}PRINCIPAL_MODEL", model_name)
        manager_model = os.getenv(f"{env_prefix}MANAGER_MODEL", model_name)
        worker_model = os.getenv(f"{env_prefix}WORKER_MODEL", model_name)
        curator_model = os.getenv(f"{env_prefix}CURATOR_MODEL", model_name)
        return cls(
            runtime=RuntimeConfig(**runtime_values),
            models={
                "principal": ModelConfig(model=principal_model, base_url=base_url),
                "manager": ModelConfig(model=manager_model, base_url=base_url),
                "worker": ModelConfig(model=worker_model, base_url=base_url),
                "curator": ModelConfig(model=curator_model, base_url=base_url),
            }
        )

    @classmethod
    def from_file(cls, path: Path | str) -> "Config":
        """Load and validate an Agent Team YAML configuration."""
        path = Path(path).expanduser()
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls(**cls._expand_env_vars(data))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        """Canonical startup loader used by the HTTP service."""
        if path is not None:
            candidate = Path(path).expanduser()
            return cls.from_file(candidate) if candidate.exists() else cls.from_env()
        return cls.from_env()

    @classmethod
    def _expand_env_string(
        cls,
        value: str,
        *,
        depth: int = 0,
        stack: tuple[str, ...] = (),
    ) -> str:
        """Expand a bounded ``${VAR}``/``${VAR:-default}`` grammar."""
        if depth > 16:
            raise ValueError("environment expansion exceeded maximum nesting depth")

        output: list[str] = []
        cursor = 0
        while True:
            start = value.find("${", cursor)
            if start < 0:
                output.append(value[cursor:])
                break
            output.append(value[cursor:start])

            level = 1
            index = start + 2
            while index < len(value) and level:
                if value.startswith("${", index):
                    level += 1
                    index += 2
                    continue
                if value[index] == "}":
                    level -= 1
                    if level == 0:
                        break
                index += 1
            if level:
                raise ValueError("unterminated environment expression")

            expression = value[start + 2 : index]
            nested = 0
            separator = -1
            offset = 0
            while offset < len(expression) - 1:
                if expression.startswith("${", offset):
                    nested += 1
                    offset += 2
                    continue
                if expression[offset] == "}" and nested:
                    nested -= 1
                elif nested == 0 and expression.startswith(":-", offset):
                    separator = offset
                    break
                offset += 1

            if separator >= 0:
                name = expression[:separator]
                default = expression[separator + 2 :]
            else:
                name = expression
                default = None
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError(f"invalid environment variable name: {name!r}")
            if name in stack:
                cycle = " -> ".join((*stack, name))
                raise ValueError(f"cyclic environment expansion: {cycle}")

            configured = os.getenv(name)
            selected = (
                default or ""
                if configured is None or (default is not None and configured == "")
                else configured
            )
            output.append(
                cls._expand_env_string(
                    selected,
                    depth=depth + 1,
                    stack=(*stack, name),
                )
            )
            cursor = index + 1

        return os.path.expanduser("".join(output))

    @classmethod
    def _expand_env_vars(cls, data: Any) -> Any:
        """Expand bounded environment expressions recursively through containers."""
        if isinstance(data, str):
            return cls._expand_env_string(data)
        if isinstance(data, dict):
            return {key: cls._expand_env_vars(value) for key, value in data.items()}
        if isinstance(data, list):
            return [cls._expand_env_vars(item) for item in data]
        return data


def get_config(config_path: str | None = None) -> Config:
    """Return the canonical YAML-first configuration."""
    return Config.load(config_path)
