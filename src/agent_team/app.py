"""HTTP boundary for the durable Principal/Team Manager runtime."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Config, get_config
from .engine import AgentTeamEngine
from .memory.knowledge import KnowledgeStore
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


def _initialize_runtime() -> None:
    """Initialize the single leased runtime for direct and lifespan callers."""
    global config, engine, session_store, runtime_lease
    if config is not None and engine is not None and session_store is not None:
        return

    loaded_config = get_config(os.getenv("AGENT_TEAM_CONFIG_FILE"))
    state_dir, workspace_dir = ensure_external_runtime_paths(
        loaded_config.runtime.state_dir,
        workspace_dir=getattr(loaded_config.runtime, "workspace_dir", None),
    )
    loaded_config.runtime.state_dir = state_dir
    loaded_config.runtime.workspace_dir = workspace_dir
    loaded_config.runtime.state_dir.mkdir(parents=True, exist_ok=True)
    logger.configure_retention(
        loaded_config.retention.log_max_bytes,
        loaded_config.retention.log_backup_count,
        log_dir=loaded_config.runtime.state_dir / "logs",
    )
    lease = RuntimeLease(loaded_config.runtime.state_dir / "runtime.lock")
    lease.acquire()
    try:
        store = SessionStore(
            loaded_config.runtime.state_dir / "sessions",
            max_messages=loaded_config.retention.max_session_messages,
        )
        store.recover_incomplete()
        knowledge_store = (
            KnowledgeStore(loaded_config.runtime.state_dir / "knowledge.db")
            if loaded_config.memory.enabled and loaded_config.memory.shared_knowledge
            else None
        )
        retention_result = DurableRetention(
            loaded_config.runtime.state_dir,
            loaded_config.retention,
        ).sweep(
            session_store=store,
            knowledge_store=knowledge_store,
        )
        runtime = AgentTeamEngine(loaded_config)
    except Exception:
        lease.release()
        raise
    config = loaded_config
    session_store = store
    engine = runtime
    runtime_lease = lease
    logger.info(
        "retention_sweep",
        sessions=retention_result.sessions,
        projects=retention_result.projects,
        artifacts=retention_result.artifacts,
        memory_scopes=retention_result.memory_scopes,
        knowledge_records=retention_result.knowledge_records,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _initialize_runtime()
    logger.info("startup", service="agent-team", state_dir=str(config.runtime.state_dir))
    global engine, runtime_lease
    try:
        yield
    finally:
        if engine is not None:
            await engine.close()
        engine = None
        if runtime_lease is not None:
            runtime_lease.release()
            runtime_lease = None


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
    if session_store is None or engine is None:
        _initialize_runtime()
    if session_store is None or engine is None:
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
    if runtime_lease is None:
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
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("AGENT_TEAM_HOST", "localhost"),
        port=int(os.getenv("AGENT_TEAM_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
