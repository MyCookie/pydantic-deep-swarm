"""Fail-closed tests for secret redaction at every durable boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx

from agent_team.artifacts import ArtifactStore
from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import CompletionReport, ManagerPlan, PrincipalDecision, ProjectBrief
from agent_team.engine import AgentTeamEngine
from agent_team.engine import PrincipalTurnResult
from agent_team.memory.agent_memory import AgentMemory
from agent_team.memory.knowledge import KnowledgeRecord, KnowledgeStore
from agent_team.observability import StructuredLogger
from agent_team.persistence import ProjectStore, SessionStore
from agent_team.redaction import redact_sensitive_text
from agent_team.worker_tools import WorkerToolExecutor


SECRET = "test-secret-value-123"
SECRET_TEXT = f"LLM_API_KEY={SECRET}"
MODELS = {
    name: ModelConfig(model=f"{name}-test", base_url=f"http://{name}")
    for name in ("principal", "manager", "worker")
}


def assert_redacted(value) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    assert SECRET not in rendered
    assert "[redacted]" in rendered


def test_raw_json_credential_assignments_are_redacted_without_breaking_json():
    payload = json.dumps({"password": SECRET, "nested": {"api_key": SECRET}})

    redacted = redact_sensitive_text(payload)

    assert SECRET not in redacted
    assert json.loads(redacted) == {
        "password": "[redacted]",
        "nested": {"api_key": "[redacted]"},
    }


def test_worker_file_output_is_redacted_before_model_evidence(tmp_path):
    (tmp_path / "secret.txt").write_text(
        f"{SECRET_TEXT}\nAuthorization: Bearer {SECRET}\n",
        encoding="utf-8",
    )
    executor = WorkerToolExecutor(tmp_path)

    result = asyncio.run(executor.execute("read_file", {"path": "secret.txt"}))

    assert result.ok
    assert_redacted(result.output)
    assert_redacted(executor.results[0].output)


def test_artifact_manifest_redacts_description_before_persistence(tmp_path):
    store = ArtifactStore(tmp_path / "workspace", tmp_path / "manifests")
    executor = WorkerToolExecutor(
        tmp_path / "workspace",
        artifact_store=store,
        project_id="project-1",
    )

    result = asyncio.run(
        executor.execute(
            "write_file",
            {
                "path": "result.txt",
                "content": "safe content",
                "description": SECRET_TEXT,
            },
        )
    )
    manifest = json.loads(
        (tmp_path / "manifests" / "project-1" / "artifacts.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.ok
    assert_redacted(result.artifact.model_dump(mode="json"))
    assert_redacted(manifest)


def test_session_and_project_stores_redact_nested_values(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    record = sessions.create(metadata={"nested": {"credential": SECRET_TEXT}})
    sessions.append_message(
        record["session_id"],
        {"role": "user", "content": f"Authorization: Bearer {SECRET}"},
    )
    stored_session = sessions.get(record["session_id"])

    projects = ProjectStore(tmp_path / "projects")
    projects.write_json(
        "project-1",
        "completion_report",
        {"summary": SECRET_TEXT, "nested": [f"token={SECRET}"]},
    )
    stored_project = projects.read_json("project-1", "completion_report")

    assert_redacted(stored_session)
    assert_redacted(stored_project)


def test_memory_and_knowledge_store_redact_before_persistence(tmp_path):
    memory = AgentMemory("principal", tmp_path / "memory")
    memory.set_preference("credential", SECRET_TEXT)
    memory.add_decision(f"Authorization: Bearer {SECRET}", {"token": SECRET})

    knowledge = KnowledgeStore(tmp_path / "knowledge.db")
    record = KnowledgeRecord(
        id="secret-record",
        topic="credential handling",
        summary=SECRET_TEXT,
        details=f"Authorization: Bearer {SECRET}",
        source_agent="test",
        tags=[f"token={SECRET}"],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    knowledge.add(record)
    stored = knowledge.get("secret-record")

    assert_redacted(memory.get_preference("credential"))
    assert_redacted(memory.get_decisions())
    assert stored is not None
    assert_redacted(stored.model_dump(mode="json"))


def test_structured_and_standard_engine_logs_redact_secrets(tmp_path, caplog):
    structured = StructuredLogger("secret-boundary-test", log_dir=tmp_path)
    structured.info(
        "credential_event",
        message=SECRET_TEXT,
        nested={"authorization": f"Bearer {SECRET}"},
    )
    structured.file_handler.flush()
    assert_redacted((tmp_path / "secret-boundary-test.jsonl").read_text(encoding="utf-8"))

    engine_logger = logging.getLogger("agent-team-engine")
    with caplog.at_level(logging.ERROR, logger="agent-team-engine"):
        engine_logger.error("engine failure: %s", SECRET_TEXT)
        try:
            raise RuntimeError(SECRET_TEXT)
        except RuntimeError:
            engine_logger.exception("engine exception")
    assert_redacted(caplog.text)


def test_http_boundary_redacts_request_response_and_durable_session(tmp_path):
    import agent_team.app as app_module

    class Runtime:
        received = None

        async def run_turn(self, user_input, session_id=None, conversation=None):
            self.received = user_input
            return PrincipalTurnResult(
                action="answer",
                response=f"Authorization: Bearer {SECRET}",
                report=CompletionReport(status="complete", summary=SECRET_TEXT),
            )

        async def cancel(self, session_id):
            return False

        async def close(self):
            return None

    runtime = Runtime()
    old = (
        app_module.config,
        app_module.engine,
        app_module.session_store,
        app_module.runtime_lease,
    )
    app_module.config = Config(runtime=RuntimeConfig(state_dir=tmp_path), models=MODELS)
    app_module.engine = runtime
    app_module.session_store = SessionStore(tmp_path / "sessions")
    app_module.runtime_lease = None
    app_module._session_locks.clear()

    async def request():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent-team",
        ) as client:
            session = await client.post("/sessions", json={"client_key": "redaction"})
            session_id = session.json()["session_id"]
            response = await client.post(
                f"/sessions/{session_id}/messages",
                json={"message": SECRET_TEXT},
            )
            return session_id, response

    try:
        session_id, response = asyncio.run(request())
        stored = app_module.session_store.get(session_id)
    finally:
        app_module._session_locks.clear()
        (
            app_module.config,
            app_module.engine,
            app_module.session_store,
            app_module.runtime_lease,
        ) = old

    assert response.status_code == 200
    assert_redacted(runtime.received)
    assert_redacted(response.json())
    assert_redacted(stored)


def test_engine_redacts_direct_input_before_principal_prompt_and_output(tmp_path):
    class Principal:
        prompts = []

        async def run(self, prompt, response_format=None):
            self.prompts.append(prompt)
            if response_format is PrincipalDecision:
                return PrincipalDecision(
                    action="answer",
                    response=f"token={SECRET}",
                )
            return f"Authorization: Bearer {SECRET}"

    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models=MODELS,
    )
    engine = AgentTeamEngine(config)
    principal = Principal()
    engine.principal = principal

    async def run():
        try:
            return await engine.run_turn(SECRET_TEXT, session_id="redacted-direct")
        finally:
            await engine.close()

    result = asyncio.run(run())

    assert principal.prompts
    assert_redacted(principal.prompts)
    assert_redacted(result.response)


def test_engine_redacts_typed_brief_before_manager_prompt(tmp_path):
    class Manager:
        prompts = []

        async def run(self, prompt, response_format=None):
            self.prompts.append(prompt)
            return ManagerPlan(tasks=[])

    class Principal:
        prompts = []

        async def run(self, prompt, response_format=None):
            self.prompts.append(prompt)
            return "complete"

    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models=MODELS,
    )
    engine = AgentTeamEngine(config)
    manager = Manager()
    principal = Principal()
    engine.manager = manager
    engine.principal = principal
    brief = ProjectBrief(objective=SECRET_TEXT, desired_output="report")

    async def run():
        try:
            return await engine.run_delegated_brief(brief, session_id="redacted-brief")
        finally:
            await engine.close()

    result = asyncio.run(run())

    assert manager.prompts
    assert_redacted(manager.prompts)
    assert_redacted(principal.prompts)
    assert_redacted(result.to_dict())
