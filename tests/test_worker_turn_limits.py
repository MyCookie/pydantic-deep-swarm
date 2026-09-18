"""Behavioral tests for bounded worker model/tool execution."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig, ToolsConfig
from agent_team.contracts import ManagerPlan, PrincipalDecision, ProjectBrief, Requirement, WorkerOutput, WorkerTaskSpec
from agent_team.engine import AgentTeamEngine
from agent_team.runtime.cancellation import ProjectTimeoutError, TimeoutEnforcer, WorkerTurnBudget
from agent_team.runtime.lifecycle import ProcessManager, TaskRegistry
from agent_team.worker_tools import ToolAwareWorkerAgent, WorkerToolExecutor


class RepeatingToolModel:
    """Model that never produces a final answer."""

    def __init__(self, count: int = 1):
        self.calls = 0
        self.count = count

    async def run(self, messages):
        self.calls += 1
        calls = [
            {
                "name": "write_file",
                "arguments": {"path": f"attempt-{self.calls}.txt", "content": "partial"},
            }
            for _ in range(self.count)
        ]
        return json.dumps({"tool_calls": calls})


@pytest.mark.asyncio
async def test_worker_counts_model_and_tool_iterations_and_returns_partial(tmp_path):
    model = RepeatingToolModel()
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=20)
    agent = ToolAwareWorkerAgent(model, "worker", executor, max_turns=3)

    result = await agent.run("finish the assignment", response_format=WorkerOutput)

    assert isinstance(result, WorkerOutput)
    assert result.status == "partial"
    assert model.calls == 2  # model, tool, model; the next tool is refused
    assert len(executor.results) == 1
    assert agent.turn_usage.model_turns == 2
    assert agent.turn_usage.tool_calls == 1
    assert agent.turn_usage.total == 3
    assert result.artifacts[0].path == "attempt-1.txt"
    assert any("turn limit" in item.lower() for item in result.unresolved)


@pytest.mark.asyncio
async def test_repeated_tool_batch_cannot_bypass_tool_call_limit(tmp_path):
    model = RepeatingToolModel(count=3)
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=1)
    agent = ToolAwareWorkerAgent(model, "worker", executor, max_turns=10)

    result = await agent.run("finish the assignment", response_format=WorkerOutput)

    assert isinstance(result, WorkerOutput)
    assert result.status == "partial"
    assert model.calls == 1
    assert len(executor.results) == 1
    assert any("tool-call limit" in item.lower() for item in result.unresolved)


@pytest.mark.asyncio
async def test_project_deadline_is_checked_before_turn_budget(tmp_path):
    registry = TaskRegistry(max_workers=1, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer", max_turns=1)
    process_manager = ProcessManager(registry)
    enforcer = TimeoutEnforcer(
        registry,
        max_turns=1,
        task_timeout_seconds=30,
        team_timeout_seconds=0.01,
    )
    enforcer.start_team("project")
    model = RepeatingToolModel()
    budget = WorkerTurnBudget(
        max_turns=1,
        worker_id=worker_id,
        before_turn=lambda: enforcer.check_team_timeout("project"),
    )
    agent = ToolAwareWorkerAgent(
        model,
        "worker",
        WorkerToolExecutor(tmp_path, max_tool_calls=10),
        turn_budget=budget,
    )

    try:
        await asyncio.sleep(0.03)
        with pytest.raises(ProjectTimeoutError):
            await agent.run("finish the assignment", response_format=WorkerOutput)
        assert model.calls == 0
    finally:
        await enforcer.finish_team("project")


@pytest.mark.asyncio
async def test_engine_records_actual_worker_model_and_tool_turns(tmp_path):
    config = Config(
        runtime=RuntimeConfig(
            state_dir=tmp_path / "state",
            workspace_dir=tmp_path / "workspace",
            max_workers=1,
            max_concurrent_workers=1,
            max_turns=3,
            worker_timeout_seconds=30,
            team_timeout_seconds=30,
        ),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        tools=ToolsConfig(max_tool_calls=20),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = AgentTeamEngine(config)
    brief = ProjectBrief(
        objective="Implement a bounded worker",
        requirements=[Requirement(id="r1", description="produce a result")],
        desired_output="report",
    )

    class Principal:
        async def run(self, prompt, response_format=None):
            if response_format is PrincipalDecision:
                return PrincipalDecision(action="delegate", brief=brief)
            return "rendered"

    class Manager:
        async def run(self, prompt, response_format=None):
            return ManagerPlan(
                tasks=[
                    WorkerTaskSpec(
                        task_id="t1",
                        role="software-engineer",
                        objective="keep working until stopped",
                        requirement_ids=["r1"],
                    )
                ]
            )

    engine.principal = Principal()
    engine.manager = Manager()
    model = RepeatingToolModel()
    engine.worker_model = model
    usage = {"model": 0, "tool": 0}
    original_increment = engine.timeout_enforcer.increment_turn

    def record_turn(worker_id, kind="model"):
        usage[kind] += 1
        return original_increment(worker_id, kind=kind)

    engine.timeout_enforcer.increment_turn = record_turn

    result = await engine.run_turn("Implement a bounded worker", session_id="turns")

    assert result.report is not None
    assert result.report.status == "partial"
    assert model.calls == 2
    assert usage == {"model": 2, "tool": 1}
    assert await engine.registry.is_empty()
    assert await engine.registry.get_concurrent_count() == 0
