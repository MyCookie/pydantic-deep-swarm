"""Failure-path tests for unconditional Agent Team cleanup."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import ManagerPlan, PrincipalDecision, ProjectBrief, Requirement, WorkerOutput, WorkerTaskSpec
from agent_team.engine import AgentTeamEngine
from agent_team.runtime.cancellation import HardCanceller
from agent_team.runtime.lifecycle import ConcurrencyLimiter, ProcessManager, TaskRegistry


def make_config(tmp_path, *, max_workers: int = 1) -> Config:
    return Config(
        runtime=RuntimeConfig(
            state_dir=tmp_path / "state",
            workspace_dir=tmp_path / "workspace",
            max_workers=max_workers,
            max_concurrent_workers=1,
            max_turns=3,
            worker_timeout_seconds=30,
            team_timeout_seconds=30,
        ),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models={
            "principal": ModelConfig(model="test", base_url="http://test"),
            "manager": ModelConfig(model="test", base_url="http://test"),
            "worker": ModelConfig(model="test", base_url="http://test"),
        },
    )


def make_brief() -> ProjectBrief:
    return ProjectBrief(
        objective="Implement a service",
        requirements=[Requirement(id="r1", description="working service")],
        desired_output="completion report",
    )


class PrincipalStub:
    def __init__(self, brief: ProjectBrief):
        self.brief = brief

    async def run(self, prompt, response_format=None):
        if response_format is PrincipalDecision:
            return PrincipalDecision(action="delegate", brief=self.brief)
        return "rendered"


class ManagerStub:
    async def run(self, prompt, response_format=None):
        return ManagerPlan(
            tasks=[
                WorkerTaskSpec(
                    task_id="t1",
                    role="software-engineer",
                    objective="Implement the service",
                    requirement_ids=["r1"],
                )
            ]
        )


@pytest.mark.asyncio
async def test_cancel_worker_preserves_completed_terminal_state():
    registry = TaskRegistry(max_workers=1, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer")
    task_id = await registry.register_task(worker_id)
    assert await registry.start_task(task_id)
    assert await registry.complete_task(task_id, {"status": "complete"})

    await registry.cancel_worker(worker_id)

    assert registry._workers[worker_id].state.value == "complete"
    assert registry._tasks[task_id].state.value == "complete"
    assert await registry.get_concurrent_count() == 0


@pytest.mark.asyncio
async def test_manager_exit_cleanup_drains_registry():
    registry = TaskRegistry(max_workers=1, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer")
    task_id = await registry.register_task(worker_id)
    assert await registry.start_task(task_id)
    canceller = HardCanceller(registry, ProcessManager(registry))

    await canceller.cleanup_on_manager_exit()

    assert await registry.is_empty()


@pytest.mark.asyncio
async def test_concurrency_release_is_idempotent():
    limiter = ConcurrencyLimiter(max_concurrent=1)
    await limiter.acquire()
    await limiter.release()
    await limiter.release()

    await limiter.acquire()
    blocked = asyncio.create_task(limiter.acquire())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(blocked, timeout=0.05)
    finally:
        if not blocked.done():
            blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        await limiter.release()


@pytest.mark.asyncio
async def test_engine_cancellation_reaps_project_registry(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path))
    engine.principal = PrincipalStub(make_brief())
    engine.manager = ManagerStub()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    def worker_factory(role, model, task_description, additional_context=None):
        class BlockingWorker:
            async def run(self, prompt, response_format=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return WorkerOutput(summary="unreachable")

        return BlockingWorker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", worker_factory)
    root = asyncio.create_task(engine.run_turn("Implement a service", session_id="cancel"))
    await asyncio.wait_for(started.wait(), timeout=2)

    assert await engine.cancel("cancel")
    result = await asyncio.wait_for(root, timeout=2)

    assert cancelled.is_set()
    assert result.report is not None
    assert result.report.status == "blocked"
    assert not engine._active_projects
    assert not engine._run_tasks
    assert not engine.registry._workers
    assert not engine.registry._tasks
    assert await engine.registry.get_concurrent_count() == 0
    assert engine.concurrency_limiter.current_count == 0
    assert not engine.timeout_enforcer._task_start_times
    assert engine.timeout_enforcer.active_team_ids == ()


@pytest.mark.asyncio
async def test_engine_close_cleans_active_project_and_is_idempotent(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path))
    engine.principal = PrincipalStub(make_brief())
    engine.manager = ManagerStub()
    started = asyncio.Event()

    def worker_factory(role, model, task_description, additional_context=None):
        class BlockingWorker:
            async def run(self, prompt, response_format=None):
                started.set()
                await asyncio.Event().wait()

        return BlockingWorker()

    monkeypatch.setattr("agent_team.engine.create_worker_agent", worker_factory)
    root = asyncio.create_task(engine.run_turn("Implement a service", session_id="shutdown"))
    await asyncio.wait_for(started.wait(), timeout=2)

    await engine.close()
    await engine.close()
    await asyncio.wait_for(root, timeout=2)

    assert not engine._active_projects
    assert not engine._run_tasks
    assert not engine.registry._workers
    assert not engine.registry._tasks
    assert await engine.registry.get_concurrent_count() == 0
    assert engine.concurrency_limiter.current_count == 0
    assert engine.timeout_enforcer.active_team_ids == ()


@pytest.mark.asyncio
async def test_engine_close_cleans_during_manager_planning(tmp_path):
    engine = AgentTeamEngine(make_config(tmp_path))
    engine.principal = PrincipalStub(make_brief())
    started = asyncio.Event()

    class BlockingManager:
        async def run(self, prompt, response_format=None):
            started.set()
            await asyncio.Event().wait()

    engine.manager = BlockingManager()
    root = asyncio.create_task(engine.run_turn("Implement a service", session_id="planning"))
    await asyncio.wait_for(started.wait(), timeout=2)

    await engine.close()
    result = await asyncio.wait_for(root, timeout=2)

    assert result.report is not None
    assert result.report.status == "blocked"
    assert not engine._active_projects
    assert not engine._run_tasks
    assert await engine.registry.is_empty()
    assert engine.timeout_enforcer.active_team_ids == ()


@pytest.mark.asyncio
async def test_overlapping_project_cleanup_is_scoped_to_each_project(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path, max_workers=2))
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    class Principal:
        async def run(self, prompt, response_format=None):
            objective = "first" if "first" in prompt else "second"
            if response_format is PrincipalDecision:
                return PrincipalDecision(action="delegate", brief=ProjectBrief(
                    objective=objective,
                    requirements=[Requirement(id="r1", description="working service")],
                    desired_output="report",
                ))
            return "rendered"

    class Manager:
        async def run(self, prompt, response_format=None):
            objective = "first" if "first" in prompt else "second"
            return ManagerPlan(tasks=[WorkerTaskSpec(
                task_id="t1",
                role="software-engineer",
                objective=objective,
                requirement_ids=["r1"],
            )])

    def worker_factory(role, model, task_description, additional_context=None):
        class Worker:
            async def run(self, prompt, response_format=None):
                if "second" in prompt:
                    second_started.set()
                    await release_second.wait()
                else:
                    await asyncio.sleep(0.05)
                return WorkerOutput(summary="done", evidence=["verified"])

        return Worker()

    engine.principal = Principal()
    engine.manager = Manager()
    monkeypatch.setattr("agent_team.engine.create_worker_agent", worker_factory)
    first = asyncio.create_task(engine.run_turn("first", session_id="first"))
    second = asyncio.create_task(engine.run_turn("second", session_id="second"))
    await asyncio.wait_for(second_started.wait(), timeout=2)

    first_result = await asyncio.wait_for(first, timeout=2)
    second_project = engine._active_projects["second"].project_id
    active_records = engine.registry.snapshot_workers()
    assert first_result.report is not None
    assert all(record.project_id == second_project for record in active_records.values())
    assert not second.done()

    release_second.set()
    await asyncio.wait_for(second, timeout=2)
    assert await engine.registry.is_empty()


@pytest.mark.asyncio
async def test_persistence_failure_still_cleans_worker_registry(tmp_path, monkeypatch):
    engine = AgentTeamEngine(make_config(tmp_path))
    engine.principal = PrincipalStub(make_brief())
    engine.manager = ManagerStub()
    original_write = engine.project_store.write_json

    def failing_write(project_id, name, payload):
        if name == "completion_report":
            raise OSError("completion report storage failed")
        return original_write(project_id, name, payload)

    monkeypatch.setattr(engine.project_store, "write_json", failing_write)

    result = await engine.run_turn("Implement a service", session_id="persist-failure")

    assert result.report is not None
    assert result.report.status == "failed"
    assert "storage failed" in " ".join(result.report.unresolved_items)
    assert not engine._active_projects
    assert not engine.registry._workers
    assert not engine.registry._tasks
    assert await engine.registry.get_concurrent_count() == 0
    assert engine.timeout_enforcer.active_team_ids == ()


@pytest.mark.asyncio
async def test_tracked_process_is_reaped_and_unregistered_without_worker_command_tool(tmp_path):
    registry = TaskRegistry(max_workers=1, max_concurrent=1)
    worker_id = await registry.register_worker("software-engineer")
    task_id = await registry.register_task(worker_id)
    assert await registry.start_task(task_id)
    manager = ProcessManager(registry)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    handle = await manager.track_process(
        worker_id,
        task_id,
        process,
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )

    await handle.reap(hard=True)
    await manager.release_process(handle, "cancelled")

    assert handle.process is not None
    assert handle.process.returncode is not None
    assert registry._workers[worker_id].child_processes == []
    await registry.cancel_task(task_id)
    assert await registry.get_concurrent_count() == 0
