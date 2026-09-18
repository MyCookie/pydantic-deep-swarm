"""High-level reconciliation and verification for the Agent Team swarm."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol
import time

from pydantic import BaseModel, Field

from .api import AgentTeamAPIClient
from .configuration import SwarmConfigReconciler, extract_config_model, extract_runfile_model
from .discovery import VLLMModelDiscovery


ROLES = ("principal", "manager", "worker", "curator")


class Restartable(Protocol):
    def restart(self) -> None: ...


class SwarmStatus(BaseModel):
    """Comparable state across vLLM, source, live s6, and Agent Team API."""

    expected_model: str
    advertised_models: list[str] = Field(default_factory=list)
    config_model: str | None = None
    source_model: str | None = None
    live_model: str | None = None
    api_models: dict[str, str | None] = Field(default_factory=dict)
    health_ok: bool = False
    ready_ok: bool = False
    drift: list[str] = Field(default_factory=list)

    @property
    def synchronized(self) -> bool:
        return not self.drift


class SwarmReconcileResult(BaseModel):
    """Result of one model reconciliation attempt."""

    model: str
    changed_files: list[str] = Field(default_factory=list)
    restarted: bool = False
    verified: bool = False
    status: SwarmStatus


class SwarmManager:
    """Single entry point for detect → reconcile → restart → verify."""

    def __init__(
        self,
        discovery: VLLMModelDiscovery,
        api: AgentTeamAPIClient,
        config: SwarmConfigReconciler,
        service: Restartable,
        *,
        verification_attempts: int = 20,
        verification_interval: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if verification_attempts < 1:
            raise ValueError("verification_attempts must be at least 1")
        if verification_interval < 0:
            raise ValueError("verification_interval cannot be negative")
        self.discovery = discovery
        self.api = api
        self.config = config
        self.service = service
        self.verification_attempts = verification_attempts
        self.verification_interval = verification_interval
        self.sleep = sleep

    @staticmethod
    def _read_model(path: Path, extractor) -> str | None:
        try:
            return extractor(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None

    def inspect(self, *, expected_model: str | None = None) -> SwarmStatus:
        advertised = self.discovery.list_models()
        expected = expected_model or self.discovery.require_single(advertised)
        drift: list[str] = []

        config_model = self._read_model(
            self.config.source_config_path,
            extract_config_model,
        )
        source_model = self._read_model(
            self.config.source_runfile_path,
            extract_runfile_model,
        )
        live_model = self._read_model(
            self.config.live_runfile_path,
            extract_runfile_model,
        )

        if advertised != [expected]:
            drift.append(f"vLLM advertises {advertised!r}; expected [{expected!r}]")
        for label, value in (
            ("Python config", config_model),
            ("source runfile", source_model),
            ("live runfile", live_model),
        ):
            if value != expected:
                drift.append(f"{label} has {value!r}; expected {expected!r}")

        api_models: dict[str, str | None] = {}
        try:
            raw_models: dict[str, Any] = self.api.models()
            api_models = {
                role: (
                    raw_models.get(role, {}).get("model")
                    if isinstance(raw_models.get(role), dict)
                    else None
                )
                for role in ROLES
            }
        except Exception as exc:
            drift.append(f"Agent Team /models unavailable: {exc}")
        for role, value in api_models.items():
            if value != expected:
                drift.append(f"API role {role} has {value!r}; expected {expected!r}")

        health_ok = False
        try:
            health = self.api.health()
            health_ok = health.get("status") == "ok"
        except Exception as exc:
            drift.append(f"Agent Team /health unavailable: {exc}")
        if not health_ok:
            drift.append("Agent Team liveness is not healthy")

        ready_ok = False
        try:
            ready = self.api.ready()
            ready_ok = ready.get("status") == "ok" and ready.get("ready") is True
        except Exception as exc:
            drift.append(f"Agent Team /ready unavailable: {exc}")
        if not ready_ok:
            drift.append("Agent Team readiness is not ready")

        return SwarmStatus(
            expected_model=expected,
            advertised_models=advertised,
            config_model=config_model,
            source_model=source_model,
            live_model=live_model,
            api_models=api_models,
            health_ok=health_ok,
            ready_ok=ready_ok,
            drift=drift,
        )

    def _wait_for_convergence(self, expected_model: str) -> SwarmStatus:
        """Poll until the restarted API reflects the reconciled model."""
        status = self.inspect(expected_model=expected_model)
        for attempt in range(self.verification_attempts):
            if status.synchronized:
                return status
            if attempt + 1 >= self.verification_attempts:
                break
            self.sleep(self.verification_interval)
            status = self.inspect(expected_model=expected_model)
        return status

    def reconcile(
        self,
        model: str | None = None,
        *,
        restart: bool = True,
        verify: bool = True,
        dry_run: bool = False,
    ) -> SwarmReconcileResult:
        advertised = self.discovery.list_models()
        target = model or self.discovery.require_single(advertised)
        if target not in advertised:
            raise ValueError(
                f"requested model {target!r} is not advertised by vLLM: {advertised!r}"
            )

        before = self.inspect(expected_model=target)
        changed_files = self.config.apply(target, dry_run=dry_run)
        restarted = False
        if not dry_run and restart and (changed_files or not before.synchronized):
            self.service.restart()
            restarted = True

        status = (
            self._wait_for_convergence(target)
            if verify and not dry_run and restarted
            else self.inspect(expected_model=target)
            if verify and not dry_run
            else before
        )
        return SwarmReconcileResult(
            model=target,
            changed_files=changed_files,
            restarted=restarted,
            verified=status.synchronized if verify and not dry_run else False,
            status=status,
        )
