"""Acceptance tests for the room-scoped Principal/Team Manager boundary."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_team.contracts import (  # noqa: E402
    AcceptanceCriterion,
    CompletionReport,
    ManagerPlan,
    PrincipalDecision,
    ProjectBrief,
    Requirement,
    RequirementResult,
    WorkerOutput,
    WorkerResult,
    WorkerTaskSpec,
)
from agent_team.engine import AgentTeamEngine  # noqa: E402
from agent_team.config import Config, ModelConfig, RuntimeConfig  # noqa: E402
from agent_team.persistence import SessionStore  # noqa: E402
from agent_team.runtime.lifecycle import TaskRegistry  # noqa: E402


def brief(objective="Build a small service"):
    return ProjectBrief(
        objective=objective,
        requirements=[Requirement(id="r1", description="A working service")],
        acceptance_criteria=[],
        desired_output="A concise completion report",
    )


def test_new_contracts_are_typed_and_nested():
    decision = PrincipalDecision(action="delegate", brief=brief())
    plan = ManagerPlan(tasks=[WorkerTaskSpec(
        task_id="t1", role="software-engineer", objective="Implement the service",
        requirement_ids=["r1"], deliverable="Code and tests",
    )])
    result = WorkerResult(worker_id="w1", task_id="t1", role="software-engineer", status="complete", summary="done")
    assert decision.brief.objective == "Build a small service"
    assert plan.tasks[0].requirement_ids == ["r1"]
    assert result.status == "complete"


def test_report_validation_rejects_missing_required_requirement():
    engine = object.__new__(AgentTeamEngine)
    report = CompletionReport(status="complete", summary="claimed", requirement_results=[])
    valid, issues = engine.validate_report(brief(), report)
    assert not valid
    assert any("r1" in issue for issue in issues)


@pytest.mark.asyncio
async def test_task_registry_cleanup_is_idempotent_and_does_not_underflow():
    registry = TaskRegistry(max_workers=2, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer")
    task_id = await registry.register_task(worker_id)
    await registry.start_task(task_id)
    await registry.complete_task(task_id, {"ok": True})
    await registry.complete_task(task_id, {"ok": True})
    assert await registry.get_concurrent_count() == 0
    assert registry._tasks[task_id].state.value == "complete"


@pytest.mark.asyncio
async def test_session_store_survives_reload(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(client_key="matrix:room:user", metadata={"room_id": "room"})
    store.append_message(record["session_id"], {"role": "user", "content": "hello"})
    reloaded = SessionStore(tmp_path / "sessions")
    loaded = reloaded.get(record["session_id"])
    assert loaded["client_key"] == "matrix:room:user"
    assert loaded["messages"][0]["content"] == "hello"
    assert reloaded.get_by_client_key("matrix:room:user")["session_id"] == record["session_id"]


@pytest.mark.asyncio
async def test_engine_direct_turn_does_not_create_workers(tmp_path):
    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path, max_workers=3, max_concurrent_workers=2),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = object.__new__(AgentTeamEngine)
    engine.config = config
    engine.principal_memory = None
    engine.manager_memory = None
    engine.knowledge_store = None
    engine._active_projects = {}
    engine.principal = AsyncMock()
    engine.manager = AsyncMock()
    result = await engine._run_direct_turn("What is 2+2?", "s1", [])
    assert result.action == "answer"
    assert engine.manager.run.await_count == 0


def test_principal_plugin_registers_tool_for_normal_agent(tmp_path):
    plugin_path = Path.home() / ".hermes/plugins/principal-agent-team/__init__.py"
    if not plugin_path.exists():
        pytest.skip("plugin is installed after implementation")
    spec = importlib.util.spec_from_file_location("principal_agent_team_test", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Context:
        def __init__(self):
            self.tools = []

        def get_config(self, key, default=None):
            return default

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, name, callback):
            raise AssertionError(f"unexpected hook: {name}")

    context = Context()
    module.register(context)
    assert [tool["name"] for tool in context.tools] == ["delegate_to_agent_team"]


class _PrincipalStub:
    def __init__(self, project_brief):
        self.project_brief = project_brief
        self.prompts = []

    async def run(self, prompt, response_format=None):
        self.prompts.append(prompt)
        if response_format is PrincipalDecision:
            return PrincipalDecision(action="delegate", brief=self.project_brief)
        return "Principal final response"


class _ManagerStub:
    def __init__(self, plan):
        self.plan = plan

    async def run(self, prompt, response_format=None):
        return self.plan


class _WorkerStub:
    def __init__(self, role, on_run, block=False):
        self.role = role
        self.on_run = on_run
        self.block = block

    async def run(self, prompt, response_format=None):
        self.on_run(self.role, prompt)
        if self.block:
            await asyncio.Event().wait()
        return WorkerOutput(summary=f"{self.role} finished", evidence=[f"evidence from {self.role}"])


@pytest.mark.asyncio
async def test_manager_executes_independent_workers_in_parallel(tmp_path, monkeypatch):
    from agent_team.worker_factory import get_role_by_name

    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path, max_workers=4, max_concurrent_workers=2),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = AgentTeamEngine(config)
    project = brief("Build and review two independent components")
    engine.principal = _PrincipalStub(project)
    engine.manager = _ManagerStub(ManagerPlan(tasks=[
        WorkerTaskSpec(task_id="t1", role="software-engineer", objective="Implement component one", requirement_ids=["r1"]),
        WorkerTaskSpec(task_id="t2", role="researcher", objective="Research component two", requirement_ids=["r1"]),
    ]))
    active = 0
    max_active = 0
    prompts = []

    async def fake_sleep_worker(role, prompt):
        nonlocal active, max_active
        prompts.append(prompt)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    def fake_factory(role, model, task_description, additional_context=None):
        class Agent:
            async def run(self, prompt, response_format=None):
                await fake_sleep_worker(role.name, prompt)
                return WorkerOutput(summary=f"{role.name} finished", evidence=["verified"])
        return Agent()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", fake_factory)
    result = await engine.run_turn("Build and review two independent components", session_id="parallel")
    assert result.report is not None
    assert result.report.status == "complete"
    assert max_active == 2
    assert len(prompts) == 2
    assert "full user conversation" not in prompts[0].lower()
    assert "Principal final response" == result.response
    assert await engine.registry.get_concurrent_count() == 0


@pytest.mark.asyncio
async def test_worker_failure_is_reported_and_cleaned(tmp_path, monkeypatch):
    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path, max_workers=2, max_concurrent_workers=2),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = AgentTeamEngine(config)
    engine.principal = _PrincipalStub(brief("Implement a service"))
    engine.manager = _ManagerStub(ManagerPlan(tasks=[
        WorkerTaskSpec(task_id="t1", role="software-engineer", objective="Implement", requirement_ids=["r1"]),
    ]))

    def fake_factory(role, model, task_description, additional_context=None):
        class Agent:
            async def run(self, prompt, response_format=None):
                raise RuntimeError("worker tool failed")
        return Agent()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", fake_factory)
    result = await engine.run_turn("Implement a service", session_id="failure")
    assert result.report is not None
    assert result.report.status in {"partial", "blocked"}
    assert result.report.unresolved_items
    assert await engine.registry.get_concurrent_count() == 0


@pytest.mark.asyncio
async def test_cancel_active_project_cancels_worker_and_root(tmp_path, monkeypatch):
    from agent_team.contracts import WorkerOutput

    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path, max_workers=2, max_concurrent_workers=1),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = AgentTeamEngine(config)
    engine.principal = _PrincipalStub(brief("Implement a long-running service"))
    engine.manager = _ManagerStub(ManagerPlan(tasks=[
        WorkerTaskSpec(task_id="t1", role="software-engineer", objective="Implement", requirement_ids=["r1"]),
    ]))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    def fake_factory(role, model, task_description, additional_context=None):
        class Agent:
            async def run(self, prompt, response_format=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return WorkerOutput(summary="never")
        return Agent()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", fake_factory)
    root = asyncio.create_task(engine.run_turn("Implement a long-running service", session_id="cancel"))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await engine.cancel("cancel")
    result = await asyncio.wait_for(root, timeout=2)
    assert cancelled.is_set()
    assert result.report is not None
    assert result.report.status == "blocked"
    assert not engine._active_projects
    assert await engine.registry.get_concurrent_count() == 0
