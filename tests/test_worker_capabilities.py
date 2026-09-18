"""Explicit worker capability policy: no implicit Hermes-native tools."""

from __future__ import annotations

import asyncio
import sys

from agent_team.config import ToolsConfig
from agent_team.worker_factory import PROFESSIONAL_ROLES, create_worker_agent
from agent_team.worker_tools import HERMES_NATIVE_CAPABILITIES, WorkerToolExecutor


class Model:
    async def run(self, messages):
        return "{}"


def test_hermes_native_capabilities_are_disabled_and_not_exposed(tmp_path):
    executor = WorkerToolExecutor(tmp_path)

    assert HERMES_NATIVE_CAPABILITIES == frozenset()
    assert ToolsConfig().skills is False
    assert ToolsConfig().mcp is False
    assert {item["name"] for item in executor.available_descriptions()} == {
        "list_files", "read_file", "write_file"
    }

    async def run_denied_calls():
        for name in ("terminal", "load_skill", "mcp_call", "connector_call"):
            result = await executor.execute(name, {})
            assert not result.ok
            assert "unknown" in result.output.lower()

    asyncio.run(run_denied_calls())


def test_worker_system_prompt_does_not_claim_hermes_tools(tmp_path):
    executor = WorkerToolExecutor(tmp_path)
    agent = create_worker_agent(
        PROFESSIONAL_ROLES["software-engineer"],
        Model(),
        "Implement the bounded assignment",
        tool_executor=executor,
    )

    assert "AVAILABLE TOOLS:" not in agent.system_prompt
    assert "Hermes-native" in agent.system_prompt
    assert "MCP" in agent.system_prompt


def test_sensitive_environment_scrubber_preserves_only_safe_values(tmp_path, monkeypatch):
    from agent_team.subprocess_env import sanitized_subprocess_env

    monkeypatch.setenv("LLM_API_KEY", "test-bearer-credential")
    monkeypatch.setenv("DEPLOYMENT_TOKEN", "test-unrelated-token")
    monkeypatch.setenv("AGENT_TEAM_SAFE_MARKER", "preserved")
    environment = sanitized_subprocess_env()

    assert "LLM_API_KEY" not in environment
    assert "DEPLOYMENT_TOKEN" not in environment
    assert environment["AGENT_TEAM_SAFE_MARKER"] == "preserved"


def test_worker_command_execution_is_not_exposed_even_when_requested(tmp_path):
    executor = WorkerToolExecutor(
        tmp_path,
    )

    assert "run_command" not in {
        item["name"] for item in executor.available_descriptions()
    }

    result = asyncio.run(
        executor.execute(
            "run_command",
            {"command": [sys.executable, "-c", "raise SystemExit(99)"]},
        )
    )

    assert not result.ok
    assert "unknown worker tool" in result.output.lower()
