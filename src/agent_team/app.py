"""HTTP boundary for the durable Principal/Team Manager runtime."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Config, get_config
from .engine import AgentTeamEngine
from .observability import get_logger
from .persistence import RuntimeLease, SessionStore
from .redaction import redact_model, redact_sensitive_data, redact_sensitive_text
from .retention import DurableRetention
from .runtime_boundary import ensure_external_runtime_paths
from .contracts import ProjectBrief


logger = get_logger("agent-team-api")
config: Config | None = None
engine: AgentTeamEngine | None = None
session_store: SessionStore | None = None
runtime_lease: RuntimeLease | None = None
_session_locks: dict[str, asyncio.Lock] = {}
# Compatibility cache for callers that imported the original symbol. Durable state is authoritative.
_sessions: dict[str, dict[str, Any]] = {}
_starting = False
_stopping = False
_prepared_store = None
_startup_config = None
_startup_deadline = None
_phase = "preflight"
_startup_failure = None
_shutdown_failure = None


class RuntimeStartupError(RuntimeError):
    def __init__(self, reason, phase, *, knowledge_reason=None):
        super().__init__(reason)
        self.reason, self.phase, self.knowledge_reason = reason, phase, knowledge_reason


def _check_startup(deadline):
    if _stopping:
        raise RuntimeStartupError("runtime_initialization_failed", _phase)
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeStartupError("startup_timeout", _phase)


def mark_stopping():
    global _stopping
    _stopping = True
    if runtime_lease is not None:
        runtime_lease.mark_stopping()


def cancel_active_work():
    """Run on the owner's event loop immediately after stop admission closes."""
    if engine is not None:
        for task in list(engine._run_tasks.values()):
            if not task.done():
                task.cancel()


def _register_engine(runtime):
    global engine
    engine = runtime


class SessionCreateRequest(BaseModel):
    prompt: str = ""
    client_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageRequest(BaseModel):
    message: str


class DelegationRequest(BaseModel):
    brief: ProjectBrief


class SessionResponse(BaseModel):
    session_id: str
    status: str
    created_at: str
    client_key: str | None = None


class HealthResponse(BaseModel):
    status: str


class MessageResponse(BaseModel):
    session_id: str
    status: str
    response: str
    action: str
    project_id: str | None = None
    brief: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    validation_issues: list[str] = Field(default_factory=list)


def _initialize_runtime(loaded_config=None, *, deadline=None) -> None:
    """Initialize the single leased runtime for direct and lifespan callers."""
    global config, engine, session_store, runtime_lease, _starting, _prepared_store, _phase
    if config is not None and engine is not None and session_store is not None:
        return

    loaded_config = loaded_config or get_config(os.getenv("AGENT_TEAM_CONFIG_FILE"))
    state_dir, workspace_dir = ensure_external_runtime_paths(
        loaded_config.runtime.state_dir,
        workspace_dir=getattr(loaded_config.runtime, "workspace_dir", None),
    )
    loaded_config.runtime.state_dir = state_dir
    loaded_config.runtime.workspace_dir = workspace_dir
    _starting = True
    _phase = "lease"
    _check_startup(deadline)
    lease = RuntimeLease(loaded_config.runtime.state_dir / "runtime.lock")
    lease.acquire()
    runtime_lease = lease
    generation = lease.generation
    guard = lambda: lease.check_generation(generation)
    try:
        _phase = "models"
        _check_startup(deadline)
        from .control.discovery import resolve_role_models
        loaded_config, _ = resolve_role_models(loaded_config, deadline=deadline)
        _phase = "knowledge"
        _check_startup(deadline)
        knowledge_store = None
        if loaded_config.memory.enabled and loaded_config.memory.shared_knowledge:
            from .memory.preparation import prepare_knowledge
            prepared = prepare_knowledge(state_dir, lease=lease, deadline=deadline)
            knowledge_store = prepared.store
        _prepared_store = knowledge_store
        _phase = "recovery"
        _check_startup(deadline)
        store = SessionStore(
            loaded_config.runtime.state_dir / "sessions",
            max_messages=loaded_config.retention.max_session_messages,
            write_guard=guard,
        )
        session_store = store
        store.recover_incomplete(deadline=deadline)
        _phase = "retention"
        _check_startup(deadline)
        retention_result = DurableRetention(
            loaded_config.runtime.state_dir,
            loaded_config.retention,
            write_guard=guard,
        ).sweep(
            session_store=store,
            knowledge_store=knowledge_store,
            deadline=deadline,
        )
        _phase = "engine"
        _check_startup(deadline)
        runtime = AgentTeamEngine(loaded_config, knowledge_store=knowledge_store, lease=lease, models_resolved=True, deadline=deadline,
                                  register_resource=_register_engine)
        engine = runtime
        _check_startup(deadline)
        logger.configure_retention(loaded_config.retention.log_max_bytes, loaded_config.retention.log_backup_count, log_dir=state_dir / "logs")
    except BaseException:
        # Lifespan unwinds the registered partial resources before releasing ownership.
        raise
    config = loaded_config
    session_store = store
    engine = runtime
    _starting = False
    logger.info(
        "retention_sweep",
        sessions=retention_result.sessions,
        projects=retention_result.projects,
        artifacts=retention_result.artifacts,
        memory_scopes=retention_result.memory_scopes,
        knowledge_records=retention_result.knowledge_records,
    )
    _phase = "listener"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _stopping, _startup_failure
    if _startup_config is None:
        _stopping = False
    _startup_failure = None
    try:
        _initialize_runtime(_startup_config, deadline=_startup_deadline)
    except BaseException as exc:
        reason = getattr(exc, "reason", "runtime_initialization_failed")
        if exc.__class__.__name__ == "RuntimeAlreadyRunningError":
            reason = "runtime_in_use"
        if _startup_deadline is not None and time.monotonic() >= _startup_deadline:
            reason = "startup_timeout"
        knowledge_reason = getattr(getattr(exc, "result", None), "reason", None)
        _startup_failure = RuntimeStartupError(reason, _phase, knowledge_reason=knowledge_reason)
        try:
            await _shutdown_runtime()
        except BaseException:
            _startup_failure = RuntimeStartupError("shutdown_failed", "shutdown")
        raise _startup_failure from None
    logger.info("startup", service="agent-team", state_dir=str(config.runtime.state_dir))
    try:
        yield
    finally:
        await _shutdown_runtime()


async def _shutdown_runtime():
    global config, engine, session_store, runtime_lease, _prepared_store, _starting, _shutdown_failure
    mark_stopping()
    try:
        if engine is not None:
            await engine.close()
    except BaseException:
        _shutdown_failure = "shutdown_failed"
        raise RuntimeStartupError("shutdown_failed", "shutdown") from None
    # A failed closer keeps ownership. Foreground OS teardown is the only safe release.
    if engine is not None and any(not task.done() for task in engine._run_tasks.values()):
        _shutdown_failure = "shutdown_failed"
        raise RuntimeStartupError("shutdown_failed", "shutdown")
    try:
        if _prepared_store is not None and hasattr(_prepared_store, "close"):
            _prepared_store.close()
    except BaseException:
        _shutdown_failure = "shutdown_failed"
        raise RuntimeStartupError("shutdown_failed", "shutdown") from None
    if runtime_lease is not None:
        runtime_lease.release()
    config = engine = session_store = runtime_lease = _prepared_store = None
    _starting = False
    _session_locks.clear()
    _sessions.clear()


app = FastAPI(title="Agent Team Runtime", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def optional_bearer_authentication(request: Request, call_next):
    """Require one shared Bearer token only when the operator configures it."""
    expected = os.getenv("AGENT_TEAM_API_TOKEN", "")
    if expected:
        scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
        authorized = (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(credential)
            and secrets.compare_digest(credential, expected)
        )
        if not authorized:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


def _require_store() -> tuple[SessionStore, AgentTeamEngine]:
    if _stopping or _starting or session_store is None or engine is None:
        raise HTTPException(status_code=503, detail="Agent Team service is not ready")
    return session_store, engine


def _session_response(record: dict[str, Any]) -> SessionResponse:
    return SessionResponse(
        session_id=record["session_id"],
        status=record.get("status", "created"),
        created_at=str(record.get("created_at", "")),
        client_key=record.get("client_key"),
    )


@app.post("/sessions", response_model=SessionResponse)
async def create_session(req: SessionCreateRequest):
    store, _ = _require_store()
    record = store.create(
        client_key=req.client_key,
        metadata=redact_sensitive_data(req.metadata),
    )
    _sessions[record["session_id"]] = record
    logger.session_started(
        session_id=record["session_id"],
        user_prompt=redact_sensitive_text(req.prompt)[:100],
    )
    return _session_response(record)


@app.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def send_message(session_id: str, req: MessageRequest):
    store, runtime = _require_store()
    safe_message = redact_sensitive_text(req.message)
    record = store.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        record = store.get(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found")
        prior_messages = redact_sensitive_data(list(record.get("messages") or []))
        store.append_message(session_id, {"role": "user", "content": safe_message})
        store.update(session_id, status="processing", last_error=None)
        logger.info("message_received", session_id=session_id, message=safe_message[:100])
        try:
            turn = await runtime.run_turn(
                safe_message,
                session_id=session_id,
                conversation=prior_messages,
            )
        except Exception as exc:
            safe_error = redact_sensitive_text(exc)
            store.update(session_id, status="failed", last_error=safe_error)
            logger.error("session_error", session_id=session_id, error=safe_error)
            raise HTTPException(status_code=500, detail="Agent Team execution failed") from exc

        response_text = redact_sensitive_text(turn.response)
        store.append_message(session_id, {"role": "assistant", "content": response_text})
        report_data = redact_sensitive_data(turn.report.model_dump()) if turn.report else None
        brief_data = redact_sensitive_data(turn.brief.model_dump()) if turn.brief else None
        plan_data = redact_sensitive_data(turn.plan.model_dump()) if turn.plan else None
        validation_issues = redact_sensitive_data(list(turn.validation_issues))
        status = "complete"
        if turn.report is not None:
            status = turn.report.status
        store.update(
            session_id,
            status=status,
            current_project_id=turn.project_id,
            last_brief=brief_data,
            last_report=report_data,
            validation_issues=validation_issues,
        )
        response = MessageResponse(
            session_id=session_id,
            status=status,
            response=response_text,
            action=turn.action,
            project_id=turn.project_id,
            brief=brief_data,
            plan=plan_data,
            report=report_data,
            validation_issues=validation_issues,
        )
        _sessions[session_id] = store.get(session_id) or {}
        logger.report_created(
            report_id=turn.project_id or uuid.uuid4().hex[:8],
            session_id=session_id,
            status=status,
        )
        logger.session_ended(session_id=session_id)
        return response


@app.post("/sessions/{session_id}/delegations", response_model=MessageResponse)
async def delegate_brief(session_id: str, req: DelegationRequest):
    store, runtime = _require_store()
    safe_brief = redact_model(req.brief)
    record = store.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        if store.get(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        store.append_message(
            session_id,
            {"role": "delegation", "content": safe_brief.objective},
        )
        store.update(session_id, status="processing", last_error=None)
        try:
            turn = await runtime.run_delegated_brief(safe_brief, session_id=session_id)
        except Exception as exc:
            safe_error = redact_sensitive_text(exc)
            store.update(session_id, status="failed", last_error=safe_error)
            logger.error("delegated_session_error", session_id=session_id, error=safe_error)
            raise HTTPException(status_code=500, detail="Agent Team delegation failed") from exc

        response_text = redact_sensitive_text(turn.response)
        store.append_message(session_id, {"role": "assistant", "content": response_text})
        report_data = redact_sensitive_data(turn.report.model_dump()) if turn.report else None
        brief_data = (
            redact_sensitive_data(turn.brief.model_dump())
            if turn.brief
            else redact_sensitive_data(safe_brief.model_dump())
        )
        plan_data = redact_sensitive_data(turn.plan.model_dump()) if turn.plan else None
        validation_issues = redact_sensitive_data(list(turn.validation_issues))
        status = turn.report.status if turn.report is not None else "complete"
        store.update(
            session_id,
            status=status,
            current_project_id=turn.project_id,
            last_brief=brief_data,
            last_report=report_data,
            validation_issues=validation_issues,
        )
        response = MessageResponse(
            session_id=session_id,
            status=status,
            response=response_text,
            action=turn.action,
            project_id=turn.project_id,
            brief=brief_data,
            plan=plan_data,
            report=report_data,
            validation_issues=validation_issues,
        )
        _sessions[session_id] = store.get(session_id) or {}
        logger.report_created(
            report_id=turn.project_id or uuid.uuid4().hex[:8],
            session_id=session_id,
            status=status,
        )
        return response


@app.post("/sessions/{session_id}/reset")
async def reset_session(session_id: str):
    store, runtime = _require_store()
    if store.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await runtime.cancel(session_id)
    record = store.update(
        session_id,
        status="created",
        messages=[],
        current_project_id=None,
        last_brief=None,
        last_report=None,
        validation_issues=[],
        last_error=None,
    )
    return _session_response(record)


@app.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    store, runtime = _require_store()
    if store.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    cancelled = await runtime.cancel(session_id)
    return {"session_id": session_id, "cancelled": cancelled}


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    store, _ = _require_store()
    record = store.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(record)


@app.get("/sessions/{session_id}/report")
async def get_report(session_id: str):
    store, _ = _require_store()
    record = store.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not record.get("last_report"):
        raise HTTPException(status_code=404, detail="No report available")
    return record["last_report"]


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return {"status": "ok"}


@app.get("/ready")
async def ready_check():
    if _stopping or _starting:
        return JSONResponse(status_code=503, content={"status": "not_ready", "ready": False, "reason": "runtime_stopping" if _stopping else "startup_in_progress"})
    if config is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "config_not_loaded"},
        )
    if engine is None or session_store is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "engine_not_initialized"},
        )
    if runtime_lease is None or not runtime_lease.is_held:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "runtime_not_owned"},
        )
    if not config.runtime.state_dir.is_dir():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "state_dir_not_accessible"},
        )
    if not os.access(config.runtime.state_dir, os.W_OK | os.X_OK):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "state_dir_not_writable"},
        )
    return {"status": "ok", "ready": True}


@app.get("/models")
async def list_models():
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not loaded")
    return {
        "models": {
            role: {"model": model.model, "base_url": model.base_url}
            for role, model in config.models.items()
        }
    }


def main():
    from .foreground import run_serve
    raise SystemExit(run_serve())


if __name__ == "__main__":
    main()
