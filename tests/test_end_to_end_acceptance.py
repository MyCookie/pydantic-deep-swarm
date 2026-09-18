"""End-to-end acceptance coverage across engine, HTTP, and Matrix boundaries."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import (
    ArtifactResult,
    CompletionReport,
    ManagerPlan,
    PrincipalDecision,
    ProjectBrief,
    Requirement,
    WorkerOutput,
    WorkerTaskSpec,
)
from agent_team.engine import AgentTeamEngine, PrincipalTurnResult
from agent_team.persistence import SessionStore
from agent_team.worker_tools import WorkerToolExecutor


MODELS = {
    name: ModelConfig(model=f"{name}-test", base_url=f"http://{name}")
    for name in ("principal", "manager", "worker")
}


def make_brief(objective: str = "Implement a service") -> ProjectBrief:
    return ProjectBrief(
        objective=objective,
        requirements=[Requirement(id="r1", description="working service")],
        desired_output="completion report",
    )


def make_config(tmp_path, *, timeout: float = 30, max_workers: int = 2) -> Config:
    return Config(
        runtime=RuntimeConfig(
            state_dir=tmp_path,
            workspace_dir=tmp_path / "workspace",
            max_workers=max_workers,
            max_concurrent_workers=max_workers,
            worker_timeout_seconds=timeout,
            team_timeout_seconds=timeout,
        ),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models=MODELS,
    )


class FixedApiEngine:
    def __init__(self, result: PrincipalTurnResult):
        self.result = result
        self.cancel_calls: list[str] = []
        self.closed = False

    async def run_turn(self, user_input, session_id=None, conversation=None):
        return self.result

    async def cancel(self, session_id: str) -> bool:
        self.cancel_calls.append(session_id)
        return True

    async def close(self) -> None:
        self.closed = True


@contextmanager
def installed_api(tmp_path, runtime):
    import agent_team.app as app_module

    old = (app_module.config, app_module.engine, app_module.session_store)
    app_module.config = Config(runtime=RuntimeConfig(state_dir=tmp_path), models=MODELS)
    app_module.engine = runtime
    app_module.session_store = SessionStore(tmp_path / "sessions")
    app_module._sessions.clear()
    app_module._session_locks.clear()
    try:
        yield app_module
    finally:
        app_module._sessions.clear()
        app_module._session_locks.clear()
        app_module.config, app_module.engine, app_module.session_store = old


@pytest.mark.asyncio
async def test_http_direct_principal_response_round_trip(tmp_path):
    runtime = FixedApiEngine(PrincipalTurnResult(action="answer", response="The answer is 4."))
    with installed_api(tmp_path, runtime) as app_module:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            session = await client.post("/sessions", json={"client_key": "direct"})
            assert session.status_code == 200
            session_id = session.json()["session_id"]
            response = await client.post(
                f"/sessions/{session_id}/messages",
                json={"message": "What is 2+2?"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["action"] == "answer"
            assert payload["status"] == "complete"
            assert payload["response"] == "The answer is 4."
            session_status = await client.get(f"/sessions/{session_id}")
            assert session_status.json()["status"] == "complete"


@pytest.mark.asyncio
async def test_http_delegated_reviewed_result_preserves_report_and_artifact(tmp_path):
    brief = make_brief()
    report = CompletionReport(
        status="complete",
        summary="Reviewed and complete",
        artifacts=[ArtifactResult(
            path="answer.txt",
            description="verified answer",
            created_by="worker-1",
            sha256="abc",
            size_bytes=3,
            verified=True,
            verification_method="sha256",
        )],
    )
    plan = ManagerPlan(
        review_required=True,
        tasks=[WorkerTaskSpec(
            task_id="t1",
            role="software-engineer",
            objective="Implement and verify",
            requirement_ids=["r1"],
        )],
    )
    result = PrincipalTurnResult(
        action="delegate",
        response="Completed with review.",
        brief=brief,
        plan=plan,
        report=report,
        project_id="project-1",
    )
    runtime = FixedApiEngine(result)
    with installed_api(tmp_path, runtime) as app_module:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            session = await client.post("/sessions", json={"client_key": "delegated"})
            session_id = session.json()["session_id"]
            response = await client.post(
                f"/sessions/{session_id}/messages",
                json={"message": "Build and review this service"},
            )
            payload = response.json()
            assert response.status_code == 200
            assert payload["action"] == "delegate"
            assert payload["report"]["status"] == "complete"
            assert payload["report"]["artifacts"][0]["verified"] is True
            assert payload["plan"]["review_required"] is True
            stored_report = await client.get(f"/sessions/{session_id}/report")
            assert stored_report.json()["summary"] == "Reviewed and complete"


class PrincipalStub:
    def __init__(self, *, decision: PrincipalDecision | None = None, response: str = "Final response"):
        self.decision = decision
        self.response = response

    async def run(self, prompt, response_format=None):
        if response_format is PrincipalDecision:
            return self.decision
        return self.response


class ManagerStub:
    def __init__(self, plan: ManagerPlan | None = None, error: Exception | None = None, delay: float = 0):
        self.plan = plan
        self.error = error
        self.delay = delay

    async def run(self, prompt, response_format=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.plan


@pytest.mark.asyncio
async def test_real_engine_delegated_review_and_verified_artifact(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path, max_workers=2))
    engine.principal = PrincipalStub(decision=PrincipalDecision(action="delegate", brief=make_brief()))
    engine.manager = ManagerStub(plan=ManagerPlan(
        review_required=True,
        tasks=[WorkerTaskSpec(
            task_id="t1", role="software-engineer", objective="Implement", requirement_ids=["r1"]
        )],
    ))

    def worker_factory(role, model, task_description, additional_context=None, tool_executor=None):
        class Worker:
            async def run(self, prompt, response_format=None):
                if role.name == "software-engineer":
                    write = await tool_executor.execute(
                        "write_file", {"path": "answer.txt", "content": "yes"}
                    )
                    assert write.ok
                return WorkerOutput(summary=f"{role.name} finished", evidence=["verified evidence"])
        return Worker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", worker_factory)
    try:
        result = await engine.run_turn("Implement and review this service", session_id="reviewed")
    finally:
        await engine.close()

    assert result.report is not None
    assert result.report.status == "complete"
    assert result.plan is not None
    assert any(task.role == "reviewer" for task in result.plan.tasks)
    assert result.report.artifacts[0].path == "answer.txt"
    assert result.report.artifacts[0].verified is True


@pytest.mark.asyncio
async def test_real_engine_manager_failure_falls_back_and_worker_failure_is_partial(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path, max_workers=1))
    engine.principal = PrincipalStub(decision=PrincipalDecision(action="delegate", brief=make_brief()))
    engine.manager = ManagerStub(error=RuntimeError("manager unavailable"))

    def successful_factory(role, model, task_description, additional_context=None, tool_executor=None):
        class Worker:
            async def run(self, prompt, response_format=None):
                return WorkerOutput(summary="fallback worker finished", evidence=["verified"])
        return Worker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", successful_factory)
    try:
        fallback = await engine.run_turn("Implement a service", session_id="manager-failure")
    finally:
        await engine.close()
    assert fallback.report is not None
    assert fallback.report.status == "complete"
    assert fallback.plan is not None
    assert fallback.plan.tasks

    malformed_engine = AgentTeamEngine(make_config(tmp_path / "malformed", max_workers=1))
    malformed_engine.principal = PrincipalStub(decision=PrincipalDecision(action="delegate", brief=make_brief()))
    malformed_engine.manager = ManagerStub(plan=ManagerPlan(tasks=[WorkerTaskSpec(
        task_id="t1", role="software-engineer", objective="Implement", requirement_ids=["r1"]
    )]))

    def malformed_factory(role, model, task_description, additional_context=None, tool_executor=None):
        class Worker:
            async def run(self, prompt, response_format=None):
                return {"status": "not-a-worker-status"}
        return Worker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", malformed_factory)
    try:
        malformed = await malformed_engine.run_turn("Implement a service", session_id="worker-malformed")
    finally:
        await malformed_engine.close()
    assert malformed.report is not None
    assert malformed.report.status == "partial"
    assert malformed.report.unresolved_items


@pytest.mark.asyncio
async def test_real_engine_clarification_timeout_and_tool_rejection(tmp_path, monkeypatch):
    clarification_engine = AgentTeamEngine(make_config(tmp_path / "clarify"))
    clarification_engine.principal = PrincipalStub(
        decision=PrincipalDecision(action="clarify", response="Which deployment target?")
    )
    try:
        clarification = await clarification_engine.run_turn("Deploy this", session_id="clarify")
    finally:
        await clarification_engine.close()
    assert clarification.action == "clarify"
    assert "deployment target" in clarification.response
    assert not clarification.project_id

    timeout_engine = AgentTeamEngine(make_config(tmp_path / "timeout", timeout=0.05))
    timeout_engine.principal = PrincipalStub(decision=PrincipalDecision(action="delegate", brief=make_brief()))
    timeout_engine.manager = ManagerStub(plan=ManagerPlan(tasks=[]), delay=0.2)
    try:
        timed_out = await timeout_engine.run_turn("Implement a service", session_id="timeout")
    finally:
        await timeout_engine.close()
    assert timed_out.report is not None
    assert timed_out.report.status == "blocked"
    assert "Project timeout exceeded." in timed_out.validation_issues

    rejection_engine = AgentTeamEngine(make_config(tmp_path / "rejection"))
    rejection_engine.principal = PrincipalStub(decision=PrincipalDecision(action="delegate", brief=make_brief()))
    rejection_engine.manager = ManagerStub(plan=ManagerPlan(tasks=[WorkerTaskSpec(
        task_id="t1", role="software-engineer", objective="Implement", requirement_ids=["r1"], tools=["read_file"]
    )]))

    def rejection_factory(role, model, task_description, additional_context=None, tool_executor=None):
        class Worker:
            async def run(self, prompt, response_format=None):
                denied = await tool_executor.execute("write_file", {"path": "no.txt", "content": "no"})
                assert not denied.ok
                return WorkerOutput(
                    status="partial",
                    summary="tool rejected",
                    evidence=[denied.output],
                    unresolved=["write_file was not granted"],
                )
        return Worker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", rejection_factory)
    try:
        rejected = await rejection_engine.run_turn("Implement a service", session_id="tool-rejection")
    finally:
        await rejection_engine.close()
    assert rejected.report is not None
    assert rejected.report.status == "partial"
    assert any("not granted" in item.lower() for item in rejected.report.unresolved_items)


class BlockingApiEngine:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, user_input, session_id=None, conversation=None):
        self.started.set()
        await self.release.wait()
        return PrincipalTurnResult(
            action="delegate",
            response="Cancelled.",
            report=CompletionReport(status="blocked", summary="Cancelled", unresolved_items=["cancelled"]),
            validation_issues=["cancelled"],
        )

    async def cancel(self, session_id: str) -> bool:
        self.release.set()
        return True

    async def close(self) -> None:
        self.release.set()


@pytest.mark.asyncio
async def test_http_cancel_releases_inflight_message(tmp_path):
    runtime = BlockingApiEngine()
    with installed_api(tmp_path, runtime) as app_module:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            session_id = (await client.post("/sessions", json={"client_key": "cancel"})).json()["session_id"]
            message_task = asyncio.create_task(client.post(
                f"/sessions/{session_id}/messages", json={"message": "long task"}
            ))
            await asyncio.wait_for(runtime.started.wait(), timeout=2)
            cancelled = await client.post(f"/sessions/{session_id}/cancel")
            completed = await asyncio.wait_for(message_task, timeout=2)
            assert cancelled.json()["cancelled"] is True
            assert completed.json()["status"] == "blocked"


class Adapter:
    def __init__(self):
        self.sent: list[str] = []
        self.typing_started = 0
        self.typing_stopped = 0

    async def send_typing(self, room):
        self.typing_started += 1

    async def stop_typing(self, room):
        self.typing_stopped += 1

    async def send(self, room, text, **kwargs):
        self.sent.append(text)


class Source:
    platform = "matrix"
    user_id = "@tester:kansara.ca"

    def __init__(self, room):
        self.chat_id = room


class Event:
    internal = False

    def __init__(self, room, text):
        self.source = Source(room)
        self.text = text
        self.message_id = "m1"


class Gateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter


@pytest.mark.asyncio
async def test_matrix_plugin_registers_and_calls_typed_delegation_tool(tmp_path):
    plugin_path = Path.home() / ".hermes/plugins/principal-agent-team/__init__.py"
    spec = importlib.util.spec_from_file_location("principal_agent_team_e2e", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class DelegationRuntime:
        async def run_delegated_brief(self, brief, session_id=None):
            return PrincipalTurnResult(
                action="delegate",
                response="delegated",
                brief=brief,
                report=CompletionReport(status="complete", summary="delegated"),
                project_id="project-tool",
            )

        async def close(self):
            pass

        async def cancel(self, session_id):
            return False

    runtime = DelegationRuntime()
    with installed_api(tmp_path, runtime) as app_module:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            async def post(base_url, path, payload, *, api_token=""):
                headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
                response = await client.post(path, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()

            plugin._post = post
            registrations = []

            class Context:
                def get_config(self, key, default=None):
                    return default

                def register_tool(self, **kwargs):
                    registrations.append(kwargs)

                def register_hook(self, name, callback):
                    raise AssertionError(f"unexpected hook: {name}")

            plugin.register(Context())
            registration = registrations[0]
            result = await registration["handler"](
                make_brief().model_dump(mode="json"),
                session_id="hermes-session",
            )

    payload = json.loads(result)
    assert payload["status"] == "complete"
    assert payload["project_id"] == "project-tool"
    assert payload["summary"] == "delegated"
