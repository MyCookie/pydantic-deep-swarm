"""Tests for the session-aware typed delegation adapter handoff."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import ManagerPlan, ProjectBrief, Requirement, WorkerOutput, WorkerTaskSpec
from agent_team.engine import AgentTeamEngine
from agent_team.persistence import SessionStore


ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "integrations" / "hermes" / "principal-agent-team" / "__init__.py"
MODELS = {
    name: ModelConfig(model=f"{name}-test", base_url=f"http://{name}")
    for name in ("principal", "manager", "worker")
}


def make_brief() -> ProjectBrief:
    return ProjectBrief(
        objective="Implement the delegated service",
        requirements=[Requirement(id="r1", description="working service")],
        desired_output="completion report",
    )


@pytest.mark.asyncio
async def test_typed_delegation_bypasses_second_principal_intake(tmp_path, monkeypatch):
    config = Config(
        runtime=RuntimeConfig(state_dir=tmp_path, max_workers=1, max_concurrent_workers=1),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models=MODELS,
    )
    engine = AgentTeamEngine(config)

    class PrincipalMustNotRun:
        async def run(self, prompt, response_format=None):
            raise AssertionError("delegated brief must not re-enter Principal intake")

    class Manager:
        async def run(self, prompt, response_format=None):
            return ManagerPlan(tasks=[WorkerTaskSpec(
                task_id="t1",
                role="software-engineer",
                objective="Implement",
                requirement_ids=["r1"],
            )])

    class Worker:
        async def run(self, prompt, response_format=None):
            return WorkerOutput(summary="delegated complete", evidence=["verified"])

    engine.principal = PrincipalMustNotRun()
    engine.manager = Manager()
    monkeypatch.setattr(
        "agent_team.engine.create_worker_agent",
        lambda role, model, task_description, additional_context=None, tool_executor=None: Worker(),
    )
    try:
        result = await engine.run_delegated_brief(make_brief(), session_id="hermes-session-1")
    finally:
        await engine.close()

    assert result.action == "delegate"
    assert result.report is not None
    assert result.report.status == "complete"


@pytest.mark.asyncio
async def test_api_delegation_endpoint_uses_typed_brief_without_principal(tmp_path):
    import agent_team.app as app_module
    from agent_team.engine import PrincipalTurnResult

    class Runtime:
        async def run_delegated_brief(self, brief, session_id=None):
            assert isinstance(brief, ProjectBrief)
            return PrincipalTurnResult(
                action="delegate",
                response="delegated",
                brief=brief,
                report=__import__("agent_team.contracts", fromlist=["CompletionReport"]).CompletionReport(
                    status="complete", summary="delegated"
                ),
                project_id="project-1",
            )

        async def close(self):
            pass

        async def cancel(self, session_id):
            return False

    old = (app_module.config, app_module.engine, app_module.session_store, app_module.runtime_lease)
    app_module.config = Config(runtime=RuntimeConfig(state_dir=tmp_path), models=MODELS)
    app_module.engine = Runtime()
    app_module.session_store = SessionStore(tmp_path / "sessions")
    app_module.runtime_lease = None
    try:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent-team") as client:
            session = await client.post("/sessions", json={"client_key": "hermes:session-1"})
            session_id = session.json()["session_id"]
            response = await client.post(
                f"/sessions/{session_id}/delegations",
                json={"brief": make_brief().model_dump(mode="json")},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["action"] == "delegate"
            assert payload["report"]["status"] == "complete"
    finally:
        app_module.config, app_module.engine, app_module.session_store, app_module.runtime_lease = old


def test_plugin_registers_typed_delegation_tool_not_gateway_bypass():
    spec = importlib.util.spec_from_file_location("principal_agent_team_routing", PLUGIN_PATH)
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
            raise AssertionError(f"Principal room must not register {name}")

    context = Context()
    module.register(context)
    assert len(context.tools) == 1
    registration = context.tools[0]
    assert registration["name"] == "delegate_to_agent_team"
    assert registration["is_async"] is True
    assert "objective" in registration["schema"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_plugin_propagates_optional_agent_team_control_token(monkeypatch):
    spec = importlib.util.spec_from_file_location("principal_agent_team_auth", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AGENT_TEAM_API_TOKEN", "control-token")
    calls = []

    async def post(base_url, path, payload, *, api_token=""):
        calls.append((base_url, path, payload, api_token))
        if path == "/sessions":
            return {"session_id": "agent-session"}
        return {
            "status": "complete",
            "project_id": "project-auth",
            "report": {"status": "complete", "summary": "done"},
        }

    module._post = post
    registrations = []

    class Context:
        def get_config(self, key, default=None):
            return default

        def register_tool(self, **kwargs):
            registrations.append(kwargs)

        def register_hook(self, name, callback):
            raise AssertionError(f"unexpected hook: {name}")

    module.register(Context())
    result = await registrations[0]["handler"](
        make_brief().model_dump(mode="json"),
        session_id="hermes-session",
    )

    assert __import__("json").loads(result)["status"] == "complete"
    assert [call[3] for call in calls] == ["control-token", "control-token"]
