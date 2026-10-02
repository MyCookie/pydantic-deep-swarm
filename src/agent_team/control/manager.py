"""High-level reconciliation and verification for the Agent Team swarm."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol
import time

from pydantic import BaseModel, Field

from .api import AgentTeamAPIClient
from .configuration import SwarmConfigReconciler, extract_config_model, extract_runfile_model
from .discovery import OpenAIModelDiscovery
from .service import ServiceControlError


ROLES = ("principal", "manager", "worker", "curator")


class Restartable(Protocol):
    def restart(self) -> None: ...


class SwarmStatus(BaseModel):
    """Comparable state across the model endpoint, deployment, and Agent Team API."""

    expected_model: str
    advertised_models: list[str] = Field(default_factory=list)
    config_model: str | None = None
    source_model: str | None = None
    live_model: str | None = None
    api_models: dict[str, str | None] = Field(default_factory=dict)
    health_ok: bool = False
    ready_ok: bool = False
    drift: list[str] = Field(default_factory=list)
    effective_models: dict[str, str] = Field(default_factory=dict)
    supervisor_state: str = "not_configured"
    supervisor_detail: str | None = None

    @property
    def synchronized(self) -> bool:
        return not self.drift


class SwarmReconcileResult(BaseModel):
    """Result of one model reconciliation attempt."""

    model: str
    changed_files: list[str] = Field(default_factory=list)
    restarted: bool = False
    verified: bool = False
    pending_restart: bool = False
    status: SwarmStatus

    outcome: str = "synchronized"
    restart_required: bool | None = None
    commit_state: str = "not_committed"
    exit_code: int = 0
    errors: list[str] = Field(default_factory=list)
    role_changes: dict[str, str] = Field(default_factory=dict)


class OwnedSwarmStatus(BaseModel):
    expected_model: str | None = None
    config_file: str | None = None
    config_source: str | None = None
    configured_models: dict[str, dict] = Field(default_factory=dict)
    effective_models: dict[str, dict] = Field(default_factory=dict)
    catalogs: dict[str, list[str]] = Field(default_factory=dict)
    observed_models: dict[str, dict] = Field(default_factory=dict)
    advertised_models: list[str] = Field(default_factory=list)
    config_model: str | None = None
    source_model: str | None = None
    live_model: str | None = None
    health_ok: bool = False
    ready_ok: bool = False
    api_available: bool = False
    model_drift: bool = False
    drift: list[str] = Field(default_factory=list)
    supervisor_state: str = "not_configured"

    @property
    def synchronized(self):
        return not self.drift and self.api_available and self.health_ok and self.ready_ok


class OwnedSwarmResult(BaseModel):
    model: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    restarted: bool = False
    verified: bool = False
    pending_restart: bool = False
    restart_required: bool | None = None
    outcome: str
    commit_state: str = "not_committed"
    exit_code: int = 0
    errors: list[str] = Field(default_factory=list)
    role_changes: dict[str, str] = Field(default_factory=dict)
    status: OwnedSwarmStatus


class OwnedSwarmManager:
    """Configuration authority and activation are separate, explicit phases."""

    def __init__(self, selection, api, service=None, *, verification_attempts=20,
                 verification_interval=.25, sleep=time.sleep, discovery_factory=OpenAIModelDiscovery,
                 state_dir=None, workspace_dir=None):
        self.selection = selection
        self.api = api
        self.service = service
        self.discovery_factory = discovery_factory
        self.verification_attempts = verification_attempts
        self.verification_interval = verification_interval
        self.sleep = sleep
        self.state_dir = state_dir
        self.workspace_dir = workspace_dir
        self.effective_models = {r: m.model for r, m in selection.config.models.items()}
        self.config = None  # legacy source/runfile mutations are never used

    def _resolve(self, override=None):
        from ..config import ROLES
        from .discovery import ModelDiscoveryError
        config = self.selection.config.model_copy(deep=True)
        endpoints = {m.base_url.rstrip("/") if m.base_url else None for m in config.models.values()}
        if override is not None and (override == "auto" or len(endpoints) != 1):
            raise ValueError("explicit override requires a non-auto ID and one shared endpoint")
        catalogs = {}
        for role in ROLES:
            item = config.models[role]
            if not item.base_url:
                raise ValueError(f"{role} requires an endpoint")
            endpoint = item.base_url.rstrip("/")
            if endpoint not in catalogs:
                probe = self.discovery_factory(endpoint)
                try:
                    catalogs[endpoint] = probe.list_models()
                finally:
                    close = getattr(probe, "close", None)
                    if close:
                        close()
            selection = override if override is not None else item.model
            value = OpenAIModelDiscovery.require_single(catalogs[endpoint]) if selection == "auto" else selection
            if value not in catalogs[endpoint]:
                raise ModelDiscoveryError(f"{role} explicit selection is not advertised", "model_not_found")
            item.model = value
        return config, catalogs

    def _observe(self, config, catalogs, *, observe=True):
        from .discovery import safe_endpoint
        desired = {r: {"model": m.model, "base_url": safe_endpoint(m.base_url)} for r, m in config.models.items()}
        status = OwnedSwarmStatus(config_file=str(self.selection.config_file), config_source=self.selection.config_source,
                                 configured_models={r: {"model": m.model, "base_url": safe_endpoint(m.base_url or "")} for r,m in self.selection.config.models.items()},
                                 effective_models=desired, catalogs={safe_endpoint(k):v for k,v in catalogs.items()},
                                 expected_model=config.models["principal"].model,
                                 advertised_models=list(dict.fromkeys(x for v in catalogs.values() for x in v)),
                                 supervisor_state="available" if self.service else "not_configured")
        if not observe:
            return status
        try:
            models = self.api.models()
            status.api_available = True
            for role, model in config.models.items():
                actual = models.get(role)
                if not isinstance(actual, dict) or actual.get("model") != model.model or str(actual.get("base_url") or "").rstrip("/") != model.base_url.rstrip("/"):
                    status.model_drift = True
                    status.drift.append(f"API role {role} differs from desired configuration")
                if isinstance(actual, dict):
                    status.observed_models[role] = {"model": actual.get("model"), "base_url": safe_endpoint(str(actual.get("base_url") or ""))}
        except Exception:
            status.drift.append("Agent Team models observation unavailable")
        for name in ("health", "ready"):
            try:
                body = getattr(self.api,name)()
                ok = body.get("status") == "ok" and (name != "ready" or body.get("ready") is True)
            except Exception:
                ok = False
            setattr(status, name+"_ok", ok)
            if not ok:
                status.drift.append(f"Agent Team {name} observation failed")
        return status

    def inspect(self):
        try:
            config, catalogs = self._resolve()
            return self._observe(config, catalogs)
        except Exception as exc:
            from .discovery import safe_endpoint
            return OwnedSwarmStatus(config_file=str(self.selection.config_file), config_source=self.selection.config_source,
                                    configured_models={r:{"model":m.model,"base_url":safe_endpoint(m.base_url or "")} for r,m in self.selection.config.models.items()},
                                    effective_models={r:{"model":None,"base_url":safe_endpoint(m.base_url or "")} for r,m in self.selection.config.models.items()},
                                    drift=[getattr(exc,"reason","configuration_or_discovery_error")])

    def reconcile(self, model=None, *, restart=True, verify=True, dry_run=False):
        from .configuration import OwnedYamlReconciler
        config, catalogs = self._resolve(model)
        desired = {r:m.model for r,m in config.models.items()}
        changes = {r:value for r,value in desired.items() if self.selection.config.models[r].model != value}
        writer = OwnedYamlReconciler(self.selection, state_dir=self.state_dir, workspace_dir=self.workspace_dir)
        status = self._observe(config,catalogs,observe=verify and not dry_run)
        result = OwnedSwarmResult(model=config.models["principal"].model,outcome="planned",status=status,role_changes=changes)
        try:
            result.changed_files = writer.apply(desired,dry_run=dry_run)
        except Exception as exc:
            result.outcome="persistence_failed"
            result.exit_code=1
            result.commit_state=getattr(exc,"commit_state","not_committed")
            result.errors=["configuration persistence failed"]
            return result
        if dry_run:
            result.restart_required=bool(result.changed_files) or None
            return result
        changed=bool(result.changed_files)
        result.commit_state="committed" if changed else "unchanged"
        result.restart_required=True if changed or status.model_drift else False if status.api_available else None
        if restart and self.service and result.restart_required is True:
            try:
                self.service.restart()
                result.restarted=True
            except Exception:
                result.outcome="restart_failed"; result.exit_code=1; result.errors=["restart failed"]
                return result
        if not verify:
            result.outcome="verification_skipped"
            result.restart_required=True if changed else None
            result.pending_restart=result.restart_required is True
            return result
        attempts=self.verification_attempts if result.restarted else 1
        for index in range(attempts):
            status=self._observe(config,catalogs)
            if status.synchronized:
                break
            if index+1<attempts:
                self.sleep(self.verification_interval)
        result.status=status
        if not status.api_available:
            result.outcome="verification_unavailable"; result.exit_code=1
            result.restart_required=True if changed else None
        elif not status.health_ok or not status.ready_ok or (result.restarted and status.model_drift):
            result.outcome="verification_failed"; result.exit_code=1
            result.restart_required=True if changed or status.model_drift else False
        elif status.model_drift:
            result.outcome="restart_required"; result.restart_required=True
        else:
            result.outcome="synchronized"; result.restart_required=False; result.verified=True
        result.pending_restart=result.restart_required is True
        return result



# Public control API now uses the owned YAML authority contract.
SwarmManager = OwnedSwarmManager
