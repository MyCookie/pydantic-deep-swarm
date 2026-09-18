"""Boundaries for dynamic staffing, tool capabilities, and worker caps."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from agent_team.config import Config, RuntimeConfig
from agent_team.contracts import ManagerPlan, ProjectBrief, SpecialistRole, WorkerTaskSpec
from agent_team.engine import AgentTeamEngine
from agent_team.worker_tools import WorkerToolExecutor


def valid_role(name: str = "domain-specialist", **kwargs) -> SpecialistRole:
    return SpecialistRole(
        name=name,
        title="Domain Specialist",
        objective="Solve the bounded domain problem",
        **kwargs,
    )


def valid_task(task_id: str = "task-1", **kwargs) -> WorkerTaskSpec:
    payload = {
        "task_id": task_id,
        "role": "software-engineer",
        "objective": "Implement the bounded assignment",
    }
    payload.update(kwargs)
    return WorkerTaskSpec(**payload)


def test_duplicate_specialist_names_are_rejected():
    with pytest.raises(ValidationError, match="Specialist role names"):
        ManagerPlan(specialist_roles=[valid_role(), valid_role()])


def test_invalid_role_tools_are_rejected():
    with pytest.raises(ValidationError, match="tool"):
        valid_role(tools=["delete_everything"])


def test_invalid_worker_task_tools_are_rejected():
    with pytest.raises(ValidationError, match="tool"):
        valid_task(tools=["network_shell"])


def test_blank_task_fields_and_duplicate_task_ids_are_rejected():
    with pytest.raises(ValidationError):
        WorkerTaskSpec(task_id="", role="software-engineer", objective="do work")
    with pytest.raises(ValidationError, match="task ids"):
        ManagerPlan(tasks=[valid_task(), valid_task()])


def test_dynamic_roles_and_tasks_are_both_capped(tmp_path):
    config = Config(runtime=RuntimeConfig(state_dir=tmp_path, max_workers=2, max_dynamic_roles=1))
    engine = object.__new__(AgentTeamEngine)
    engine.config = config
    brief = ProjectBrief(objective="bounded task", desired_output="report")
    plan = ManagerPlan(
        specialist_roles=[valid_role()],
        tasks=[
            valid_task("task-1", role="domain-specialist"),
            valid_task("task-2", role="domain-specialist"),
            valid_task("task-3", role="domain-specialist"),
        ],
    )

    sanitized = engine._sanitize_plan(plan, brief)

    assert len(sanitized.specialist_roles) <= 1
    assert len(sanitized.tasks) <= 2


def test_worker_executor_enforces_assignment_tool_allowlist(tmp_path):
    async def run():
        executor = WorkerToolExecutor(tmp_path, allowed_tools=["read_file"])
        assert [item["name"] for item in executor.available_descriptions()] == ["read_file"]
        denied = await executor.execute("write_file", {"path": "out.txt", "content": "no"})
        assert not denied.ok
        assert "not allowed" in denied.output

    asyncio.run(run())
