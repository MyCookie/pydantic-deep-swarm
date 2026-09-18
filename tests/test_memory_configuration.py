"""Acceptance tests for memory and curator configuration boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import CompletionReport, ManagerPlan, ProjectBrief
from agent_team.engine import AgentTeamEngine
from agent_team.memory.curator import MemoryCurator
from agent_team.memory.knowledge import KnowledgeStore


def make_config(tmp_path, *, memory: MemoryConfig | None = None) -> Config:
    model_configs = {
        "principal": ModelConfig(model="principal-model", base_url="http://principal"),
        "manager": ModelConfig(model="manager-model", base_url="http://manager"),
        "worker": ModelConfig(model="worker-model", base_url="http://worker"),
        "curator": ModelConfig(model="curator-model", base_url="http://curator"),
    }
    return Config(
        runtime=RuntimeConfig(state_dir=tmp_path),
        models=model_configs,
        memory=memory or MemoryConfig(),
    )


def make_report(*, finding: str = "Use verified artifacts") -> CompletionReport:
    return CompletionReport(
        status="complete",
        summary="The project completed successfully.",
        important_findings=[finding],
        decisions=["Keep the implementation bounded"],
    )


def make_brief() -> ProjectBrief:
    return ProjectBrief(objective="Implement the requested project", desired_output="report")


@pytest.mark.asyncio
async def test_memory_disabled_creates_no_memory_files_or_records(tmp_path):
    engine = AgentTeamEngine(
        make_config(tmp_path, memory=MemoryConfig(enabled=False, shared_knowledge=True, curator_enabled=True))
    )

    assert engine.principal_memory is None
    assert engine.manager_memory is None
    assert engine.knowledge_store is None
    assert engine.curator is None

    await engine._record_memory(
        "project-1", make_brief(), ManagerPlan(), make_report(), []
    )

    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "knowledge.db").exists()


@pytest.mark.asyncio
async def test_curator_disabled_does_not_invoke_curator(tmp_path):
    engine = AgentTeamEngine(
        make_config(tmp_path, memory=MemoryConfig(enabled=True, shared_knowledge=True, curator_enabled=False))
    )

    assert engine.curator is None
    with patch.object(
        MemoryCurator,
        "run_curator",
        new=AsyncMock(side_effect=AssertionError("curator must not be invoked")),
    ):
        await engine._record_memory(
            "project-1", make_brief(), ManagerPlan(), make_report(), []
        )



def test_enabled_curator_uses_configured_curator_model(tmp_path):
    engine = AgentTeamEngine(make_config(tmp_path))

    assert engine.curator is not None
    assert engine.curator.curator_agent is not None
    assert engine.curator.curator_agent.model.model_name == "curator-model"
    assert engine.curator.curator_agent.model.base_url == "http://curator"


@pytest.mark.asyncio
async def test_curator_failure_does_not_change_completed_project_result(tmp_path):
    engine = AgentTeamEngine(make_config(tmp_path))
    assert engine.curator is not None
    engine.curator.run_curator = AsyncMock(side_effect=RuntimeError("curator unavailable"))
    report = make_report()

    await engine._record_memory("project-1", make_brief(), ManagerPlan(), report, [])

    assert report.status == "complete"
    assert report.summary == "The project completed successfully."


@pytest.mark.asyncio
async def test_duplicate_curator_promotion_is_idempotent(tmp_path):
    class FakeCuratorAgent:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, response_format=None):
            self.calls += 1
            return {
                "records": [
                    {
                        "kind": "finding",
                        "topic": "verified-artifacts",
                        "summary": "Use verified artifacts",
                        "details": "Artifact verification is required before reporting completion.",
                        "tags": ["finding"],
                        "confidence": 0.9,
                    }
                ]
            }

    store = KnowledgeStore(tmp_path / "knowledge.db")
    agent = FakeCuratorAgent()
    curator = MemoryCurator(agent, store, tmp_path, enabled=True)
    report = make_report()

    first = await curator.run_curator(report, {}, "project-1")
    second = await curator.run_curator(report, {}, "project-1")

    assert agent.calls == 2
    assert len(first) == 1
    assert len(second) == 1
    assert store.count() == 1
    assert store.search(topic="verified-artifacts")[0].source_run == "project-1:curator"


@pytest.mark.asyncio
async def test_engine_memory_recording_is_idempotent_for_same_project(tmp_path):
    engine = AgentTeamEngine(make_config(tmp_path))
    engine.curator = None
    report = make_report(finding="Keep reports evidence-backed")

    await engine._record_memory("project-1", make_brief(), ManagerPlan(), report, [])
    await engine._record_memory("project-1", make_brief(), ManagerPlan(), report, [])

    assert engine.knowledge_store is not None
    assert engine.knowledge_store.count() == 1
