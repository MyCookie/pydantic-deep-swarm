"""Regression coverage for one absolute project deadline."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import (
    ManagerPlan,
    PrincipalDecision,
    ProjectBrief,
    Requirement,
    WorkerOutput,
    WorkerTaskSpec,
)
from agent_team.engine import AgentTeamEngine
from agent_team.runtime.cancellation import (
    HardCanceller,
    ProjectTimeoutError,
    TimeoutEnforcer,
    enforce_worker_lifecycle,
)
from agent_team.runtime.lifecycle import ConcurrencyLimiter, TaskRegistry


@pytest.mark.asyncio
async def test_worker_uses_remaining_absolute_project_deadline():
    registry = TaskRegistry(max_workers=1, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer")
    process_manager = __import__(
        "agent_team.runtime.lifecycle", fromlist=["ProcessManager"]
    ).ProcessManager(registry)
    canceller = HardCanceller(registry, process_manager)
    enforcer = TimeoutEnforcer(
        registry,
        task_timeout_seconds=30,
        team_timeout_seconds=1,
    )
    limiter = ConcurrencyLimiter(max_concurrent=1)
    enforcer.start_team("project")
    try:
        await asyncio.sleep(0.55)
        started = time.monotonic()
        with pytest.raises(ProjectTimeoutError):
            await enforce_worker_lifecycle(
                worker_id,
                asyncio.sleep(0.6),
                canceller,
                enforcer,
                limiter,
                team_id="project",
            )
        assert time.monotonic() - started < 0.9
    finally:
        await enforcer.finish_team("project")


@pytest.mark.asyncio
async def test_project_deadline_is_shared_by_planning_and_workers(tmp_path, monkeypatch):
    brief = ProjectBrief(
        objective="Implement a service",
        requirements=[Requirement(id="r1", description="working service")],
        desired_output="A completion report",
    )
    config = Config(
        runtime=RuntimeConfig(
            state_dir=tmp_path,
            max_workers=1,
            max_concurrent_workers=1,
            worker_timeout_seconds=30,
            team_timeout_seconds=1,
        ),
        memory=MemoryConfig(enabled=False),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )
    engine = AgentTeamEngine(config)

    class Principal:
        async def run(self, prompt, response_format=None):
            if response_format is PrincipalDecision:
                return PrincipalDecision(action="delegate", brief=brief)
            return "rendered"

    class SlowManager:
        async def run(self, prompt, response_format=None):
            await asyncio.sleep(0.55)
            return ManagerPlan(
                tasks=[
                    WorkerTaskSpec(
                        task_id="t1",
                        role="software-engineer",
                        objective="Implement",
                        requirement_ids=["r1"],
                    )
                ]
            )

    class SlowWorker:
        async def run(self, prompt, response_format=None):
            await asyncio.sleep(0.55)
            return WorkerOutput(summary="done")

    engine.principal = Principal()
    engine.manager = SlowManager()

    def worker_factory(role, model, task_description, additional_context=None, tool_executor=None):
        return SlowWorker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", worker_factory)

    result = await engine.run_turn("Implement a service", session_id="deadline")

    assert result.report is not None
    assert result.report.status == "blocked"
    assert result.validation_issues == ["Project timeout exceeded."]
    assert not engine._active_projects
    assert await engine.registry.get_concurrent_count() == 0


@pytest.mark.asyncio
async def test_concurrent_project_deadlines_do_not_reset_each_other():
    enforcer = TimeoutEnforcer(TaskRegistry(), team_timeout_seconds=1)
    first = enforcer.start_team("first")
    await asyncio.sleep(0.05)
    second = enforcer.start_team("second")

    try:
        assert first is not second
        assert first.expires_at < second.expires_at
        assert enforcer.remaining_team_seconds("first") < enforcer.remaining_team_seconds("second")
    finally:
        first_timer = first.timer_task
        second_timer = second.timer_task
        await asyncio.gather(
            enforcer.finish_team("first"),
            enforcer.finish_team("second"),
        )
        assert first_timer is not None and first_timer.done() and first_timer.cancelled()
        assert second_timer is not None and second_timer.done() and second_timer.cancelled()
        assert enforcer.active_team_ids == ()
