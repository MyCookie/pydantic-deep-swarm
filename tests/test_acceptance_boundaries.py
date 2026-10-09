"""Acceptance coverage for the refactored Agent Team boundaries."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_team.artifacts import ArtifactStore
from agent_team.config import Config
from agent_team.contracts import (
    AcceptanceCriterion,
    AcceptanceResult,
    ArtifactResult,
    CompletionReport,
    ManagerPlan,
    PrincipalDecision,
    ProjectBrief,
    Requirement,
    RequirementResult,
    SpecialistRole,
    WorkerTaskSpec,
    WorkerOutput,
)
from agent_team.engine import AgentTeamEngine
from agent_team.memory.agent_memory import AgentMemory
from agent_team.worker_factory import PROFESSIONAL_ROLES
from agent_team.worker_tools import WorkerToolExecutor, ToolAwareWorkerAgent


def test_worker_tools_write_and_read_are_workspace_bounded(tmp_path):
    async def run():
        store = ArtifactStore(tmp_path, tmp_path / "manifests")
        executor = WorkerToolExecutor(tmp_path, store, project_id="p", worker_id="w")
        written = await executor.execute("write_file", {"path": "out.txt", "content": "hello"})
        assert written.ok
        assert written.artifact is not None and written.artifact.verified
        read = await executor.execute("read_file", {"path": "out.txt"})
        assert read.ok and read.output == "hello"
        escaped = await executor.execute("read_file", {"path": "../outside.txt"})
        assert not escaped.ok
        manifest = json.loads((tmp_path / "manifests" / "p" / "artifacts.json").read_text())
        assert manifest["artifacts"][0]["sha256"] == written.artifact.sha256

    asyncio.run(run())


def test_tool_aware_worker_executes_tool_before_structured_output(tmp_path):
    calls = []

    def respond(messages, info):
        calls.append(len(messages))
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "answer.txt", "content": "done"})])
        assert Path(tmp_path, "answer.txt").read_text() == "done"
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "status": "complete",
            "summary": "wrote answer",
            "artifacts": [{"path": "answer.txt", "description": "answer", "created_by": "worker"}],
        })])

    async def run():
        executor = WorkerToolExecutor(tmp_path)
        agent = ToolAwareWorkerAgent(FunctionModel(respond), "worker", executor)
        result = await agent.run("write the answer", response_format=WorkerOutput)
        assert result.status == "complete"
        assert Path(tmp_path, "answer.txt").read_text() == "done"
        assert len(calls) == 2

    asyncio.run(run())


def test_tool_aware_worker_repairs_missing_acceptance_results(tmp_path):
    calls = []

    def respond(messages, info):
        calls.append(len(messages))
        payload = {"status": "complete", "summary": "done"}
        if len(calls) == 2:
            payload["acceptance_results"] = [{
                "criterion_id": "a1",
                "status": "passed",
                "evidence": ["pytest: 1 passed"],
                "verification_method": "pytest",
            }]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    async def run():
        executor = WorkerToolExecutor(
            tmp_path,
            acceptance_criteria=[
                {"id": "a1", "description": "tests pass", "verification_method": "pytest"}
            ],
        )
        result = await ToolAwareWorkerAgent(FunctionModel(respond), "worker", executor).run(
            "finish",
            response_format=WorkerOutput,
        )
        assert result.acceptance_results[0].criterion_id == "a1"
        assert len(calls) == 2

    asyncio.run(run())


def test_principal_validation_requires_acceptance_and_verified_artifact():
    brief = ProjectBrief(
        objective="Build a service",
        requirements=[Requirement(id="r1", description="working service")],
        acceptance_criteria=[AcceptanceCriterion(id="a1", description="tests pass", verification_method="pytest")],
        desired_output="report",
    )
    report = CompletionReport(
        status="complete",
        summary="claimed",
        requirement_results=[RequirementResult(requirement_id="r1", status="satisfied", evidence=["claim"])],
        artifacts=[ArtifactResult(path="missing.txt", description="missing", created_by="worker")],
    )
    valid, issues = AgentTeamEngine.validate_report(object.__new__(AgentTeamEngine), brief, report)
    assert not valid
    assert any("acceptance criterion" in issue.lower() for issue in issues)
    assert any("artifact" in issue.lower() for issue in issues)


def test_yaml_configuration_wins_over_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  max_workers: 2\nmodels:\n  principal:\n    model: yaml-model\n    base_url: http://yaml\n"
    )
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("PRINCIPAL_MODEL", "env-model")
    config = Config.from_env()
    assert config.runtime.max_workers == 2
    assert config.models["principal"].model == "yaml-model"


def test_memory_summary_contains_learning_and_isolated_scope(tmp_path):
    global_memory = AgentMemory("principal", tmp_path)
    global_memory.add_lesson("reuse verified artifacts", category="execution")
    assert global_memory.get_summary()["recent_lessons"][0]["lesson"] == "reuse verified artifacts"
    project_memory = AgentMemory("principal", tmp_path, scope="project-1")
    assert project_memory.get_lessons() == []
    assert global_memory.memory_file != project_memory.memory_file


def test_dynamic_role_is_validated_and_bounded(tmp_path):
    config = Config()
    config.runtime.state_dir = tmp_path
    config.runtime.max_dynamic_roles = 1
    engine = object.__new__(AgentTeamEngine)
    engine.config = config
    brief = ProjectBrief(objective="special task", requirements=[Requirement(id="r1", description="do it")], desired_output="report")
    plan = ManagerPlan(
        specialist_roles=[
            SpecialistRole(name="domain-specialist", title="Domain Specialist", objective="solve domain problem"),
            SpecialistRole(name="second-specialist", title="Second", objective="should be dropped"),
        ],
        tasks=[WorkerTaskSpec(task_id="t1", role="domain-specialist", objective="solve", requirement_ids=["r1"])],
    )
    sanitized = engine._sanitize_plan(plan, brief)
    assert len(sanitized.specialist_roles) == 1
    assert sanitized.tasks[0].role == "domain-specialist"
    assert engine._role_catalog(sanitized)["domain-specialist"].title == "Domain Specialist"


def test_review_required_injects_reviewer_with_dependencies(tmp_path):
    config = Config()
    config.runtime.state_dir = tmp_path
    engine = object.__new__(AgentTeamEngine)
    engine.config = config
    brief = ProjectBrief(objective="review task", requirements=[Requirement(id="r1", description="do it")], desired_output="report")
    plan = ManagerPlan(
        review_required=True,
        tasks=[WorkerTaskSpec(task_id="t1", role="software-engineer", objective="do", requirement_ids=["r1"])],
    )
    sanitized = engine._sanitize_plan(plan, brief)
    reviewer = [task for task in sanitized.tasks if task.role == "reviewer"]
    assert len(reviewer) == 1
    assert reviewer[0].dependencies == ["t1"]


def test_acceptance_result_can_mark_complete():
    report = CompletionReport(
        status="complete",
        summary="done",
        requirement_results=[RequirementResult(requirement_id="r1", status="satisfied", evidence=["pytest"])],
        acceptance_results=[AcceptanceResult(criterion_id="a1", status="passed", evidence=["pytest"], verification_method="pytest")],
    )
    assert report.acceptance_results[0].status == "passed"


def test_clarification_terminates_at_principal_without_team_creation(tmp_path):
    class Principal:
        async def run(self, prompt, response_format=None):
            return PrincipalDecision(action="clarify", response="Which deployment target should I use?")

    async def run():
        config = Config.from_env()
        config.runtime.state_dir = tmp_path
        engine = object.__new__(AgentTeamEngine)
        engine.config = config
        engine.principal = Principal()
        engine.principal_memory = None
        engine._run_tasks = {}
        result = await engine.run_turn("Deploy this", session_id="clarify")
        assert result.action == "clarify"
        assert "deployment target" in result.response

    asyncio.run(run())


def test_removed_command_tool_never_registers_a_process(tmp_path):
    async def run():
        from agent_team.runtime.lifecycle import ProcessManager, TaskRegistry
        registry = TaskRegistry(max_workers=1, max_concurrent=1)
        worker_id = await registry.register_worker("software-engineer")
        task_id = await registry.register_task(worker_id)
        await registry.start_task(task_id)
        manager = ProcessManager(registry)
        executor = WorkerToolExecutor(
            tmp_path,
            worker_id=worker_id,
        )
        result = await executor.execute(
            "run_command",
            {"command": [sys.executable, "-c", "print('must not run')"]},
        )
        assert not result.ok
        assert "unknown worker tool" in result.output.lower()
        assert registry._workers[worker_id].child_processes == []

    asyncio.run(run())


def test_matrix_plugin_registers_typed_tool_without_gateway_bypass():
    plugin_path = Path(__file__).resolve().parents[1] / "integrations/hermes/principal-agent-team/__init__.py"
    spec = importlib.util.spec_from_file_location("principal_agent_team_acceptance", plugin_path)
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
            raise AssertionError(f"unexpected gateway hook: {name}")

    context = Context()
    module.register(context)
    assert len(context.tools) == 1
    registration = context.tools[0]
    assert registration["name"] == "delegate_to_agent_team"
    assert registration["toolset"] == "agent_team"
    assert registration["is_async"] is True
    assert "objective" in registration["schema"]["parameters"]["properties"]
