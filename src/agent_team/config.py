"""Configuration management for the Agent Team runtime."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from dataclasses import dataclass
from urllib.parse import urlsplit

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
    def from_env(cls, env_prefix: str = "", *, fallback_model: str | None = None) -> "Config":
        """Resolve canonical configuration; deployment fallbacks are not authority."""
        return resolve_configuration().config

    @classmethod
    def from_file(cls, path: Path | str) -> "Config":
        return resolve_configuration(path).config

    @classmethod
    def load(cls, path: Path | str | None = None, *, fallback_model: str | None = None) -> "Config":
        return resolve_configuration(path).config

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


def get_config(config_path: str | None = None, *, fallback_model: str | None = None) -> Config:
    """Return the canonical YAML-first configuration."""
    return resolve_configuration(config_path).config


@dataclass(frozen=True)
class ConfigSelection:
    config: Config
    config_file: Path
    config_source: str
    config_input_path: Path | None = None


ROLES = ("principal", "manager", "worker", "curator")


def validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("model endpoint must be an HTTP(S) URL with a host")


def effective_models(raw: dict[str, Any]) -> dict[str, ModelConfig]:
    if not isinstance(raw, dict) or set(raw) - {*ROLES, "default"}:
        raise ValueError("models must contain only default and runtime roles")

    def fields(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("model role must be a mapping")
        return value

    default = fields(raw.get("default"))
    values: dict[str, ModelConfig] = {}
    for role in ROLES:
        item = fields(raw.get(role))
        parent = values["worker"].model_dump() if role == "curator" else default
        selection = item.get("model") if item.get("model") is not None else parent.get("model")
        endpoint = item.get("base_url") if item.get("base_url") is not None else parent.get("base_url")
        selection = "auto" if selection is None else selection
        if not isinstance(selection, str) or not selection.strip():
            raise ValueError(f"{role} model must be nonempty")
        if endpoint is not None:
            if not isinstance(endpoint, str) or not endpoint.strip():
                raise ValueError(f"{role} endpoint must be nonempty")
            validate_endpoint(endpoint)
        values[role] = ModelConfig(model=selection, base_url=endpoint)
    return values


def resolve_configuration(config_file: Path | str | None = None, *, state_dir: Path | str | None = None,
                          workspace_dir: Path | str | None = None, allow_missing_named: bool = False) -> ConfigSelection:
    from .runtime_boundary import ensure_external_runtime_paths

    def absolute(value):
        return Path(value).expanduser().resolve()

    environment_state = os.getenv("AGENT_TEAM_STATE_DIR")
    compatibility_home = os.getenv("AGENT_TEAM_HOME")
    bootstrap_state = absolute(state_dir if state_dir is not None else environment_state or
                               (str(Path(compatibility_home) / ".agent-team") if compatibility_home else Path.home() / ".agent-team"))
    selected_name = config_file if config_file is not None else os.getenv("AGENT_TEAM_CONFIG_FILE")
    path = absolute(selected_name) if selected_name is not None else bootstrap_state / "config/config.yaml"
    exists = path.exists()
    if not exists and path.is_symlink():
        raise ValueError("configuration is a dangling symlink")
    if exists and not path.is_file():
        raise ValueError("configuration must be a regular file")
    if not exists and selected_name is not None and not allow_missing_named:
        raise ValueError("explicitly selected configuration file is missing")
    if exists:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise ValueError("configuration must be a mapping")
        data = Config._expand_env_vars(data)
    else:
        data = {}
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be a mapping")
    runtime = dict(runtime)
    for key in ("state_dir", "workspace_dir"):
        value = runtime.get(key)
        if key == "state_dir" and key in runtime and value is None:
            raise ValueError("null state_dir is invalid")
        if value is not None and (not isinstance(value, str) or not value or not Path(value).is_absolute()):
            raise ValueError(f"YAML {key} must expand to an absolute path")
    final_state = absolute(state_dir if state_dir is not None else runtime.get("state_dir") or bootstrap_state)
    source_location = Path(selected_name).expanduser().absolute() if selected_name is not None else path
    if state_dir is None and source_location.is_relative_to(bootstrap_state) and (environment_state or compatibility_home) and final_state != bootstrap_state:
        raise ValueError("environment-selected state directory conflicts with YAML state_dir; use an explicit state flag or repair configuration")
    final_workspace = absolute(workspace_dir if workspace_dir is not None else runtime.get("workspace_dir") or
                               os.getenv("AGENT_TEAM_WORKSPACE_DIR") or final_state / "workspace")
    final_state, final_workspace = ensure_external_runtime_paths(final_state, workspace_dir=final_workspace)
    runtime.update(state_dir=final_state, workspace_dir=final_workspace)
    data["runtime"] = runtime
    if not exists:
        models = {"default": {"model": os.getenv("LLM_MODEL", "auto"), "base_url": os.getenv("LLM_BASE_URL")}}
        for role in ROLES:
            models[role] = {key: os.environ[name] for key, name in (("model", role.upper()+"_MODEL"), ("base_url", role.upper()+"_BASE_URL")) if name in os.environ}
        data["models"] = models
    data["models"] = effective_models(data.get("models", {}))
    input_path = Path(selected_name).expanduser().absolute() if selected_name is not None else path
    return ConfigSelection(Config(**data), path, "yaml" if exists else "environment", input_path)


def active_roles(config: Config) -> tuple[str, ...]:
    return ROLES if config.memory.enabled and config.memory.shared_knowledge and config.memory.curator_enabled else ROLES[:3]
