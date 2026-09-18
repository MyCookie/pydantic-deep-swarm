"""Scope, curator-input, provenance, and supersession boundary tests."""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import ArtifactResult, CompletionReport
from agent_team.engine import AgentTeamEngine
from agent_team.memory.agent_memory import AgentMemory
from agent_team.memory.curator import MemoryCurator
from agent_team.memory.knowledge import KnowledgeRecord, KnowledgeStore


def record(record_id: str, *, supersedes: str | None = None, artifact: str | None = None) -> KnowledgeRecord:
    now = datetime.utcnow()
    return KnowledgeRecord(
        id=record_id,
        topic="deployment",
        summary=record_id,
        details="durable detail",
        source_agent="test",
        tags=["test"],
        confidence=0.9,
        created_at=now,
        updated_at=now,
        supersedes=supersedes,
        source_artifact=artifact,
    )


def test_memory_visibility_is_role_and_scope_separated(tmp_path):
    principal = AgentMemory("principal", tmp_path)
    manager = AgentMemory("manager", tmp_path)
    worker = AgentMemory("worker-software-engineer", tmp_path)
    other_worker = AgentMemory("worker-researcher", tmp_path)
    project_worker = AgentMemory("worker-software-engineer", tmp_path, scope="project-1")

    principal.add_lesson("principal-only", category="boundary")
    manager.add_lesson("manager-only", category="boundary")
    worker.add_lesson("engineer-role-only", category="boundary")
    project_worker.add_lesson("project-only", category="boundary")

    assert manager.get_lessons(category="boundary") == [{
        "id": manager.get_lessons(category="boundary")[0]["id"],
        "lesson": "manager-only",
        "category": "boundary",
        "timestamp": manager.get_lessons(category="boundary")[0]["timestamp"],
    }]
    assert [item["lesson"] for item in principal.get_lessons()] == ["principal-only"]
    assert [item["lesson"] for item in worker.get_lessons()] == ["engineer-role-only"]
    assert [item["lesson"] for item in other_worker.get_lessons()] == []
    assert [item["lesson"] for item in project_worker.get_lessons()] == ["project-only"]


def test_engine_prompt_memory_helpers_enforce_role_boundaries(tmp_path):
    models = {
        name: ModelConfig(model=f"{name}-model", base_url=f"http://{name}")
        for name in ("principal", "manager", "worker", "curator")
    }
    engine = AgentTeamEngine(Config(
        runtime=RuntimeConfig(state_dir=tmp_path),
        models=models,
        memory=MemoryConfig(enabled=True, recent_items=8),
    ))
    engine.principal_memory.add_lesson("principal-only", category="boundary")
    engine.manager_memory.add_lesson("manager-only", category="boundary")
    AgentMemory("worker-software-engineer", tmp_path / "memory").add_lesson(
        "engineer-global", category="boundary"
    )
    AgentMemory("worker-researcher", tmp_path / "memory").add_lesson(
        "researcher-only", category="boundary"
    )
    AgentMemory("worker-software-engineer", tmp_path / "memory", scope="project-1").add_lesson(
        "project-only", category="boundary"
    )

    principal_text = json.dumps(engine._principal_memory_summary())
    manager_text = json.dumps(engine._manager_memory_summary())
    worker_text = json.dumps(engine._worker_memory_summary("software-engineer", "project-1"))

    assert "principal-only" in principal_text
    assert "manager-only" not in principal_text
    assert "engineer-global" not in principal_text
    assert "manager-only" in manager_text
    assert "principal-only" not in manager_text
    assert "researcher-only" not in manager_text
    assert "engineer-global" in worker_text
    assert "project-only" in worker_text
    assert "principal-only" not in worker_text
    assert "manager-only" not in worker_text
    assert "researcher-only" not in worker_text


def test_curator_context_excludes_transcript_shaped_worker_input(tmp_path):
    curator = MemoryCurator(None, KnowledgeStore(tmp_path / "knowledge.db"), tmp_path, enabled=True)
    report = CompletionReport(
        status="complete",
        summary="safe summary",
        important_findings=["durable finding"],
    )

    context = curator._build_curator_context(
        report,
        {"worker-1": "USER: secret transcript\nASSISTANT: hidden answer"},
        "project-1",
    )

    assert "secret transcript" not in context
    assert "hidden answer" not in context
    assert "durable finding" in context


def test_curator_promotion_preserves_project_and_artifact_provenance(tmp_path):
    class CuratorAgent:
        async def run(self, prompt, response_format=None):
            assert "ARTIFACT REFERENCES:" in prompt
            assert "result.txt" in prompt
            return {
                "records": [{
                    "kind": "finding",
                    "topic": "verified result",
                    "summary": "The result was verified.",
                    "details": "Keep the verified result.",
                    "tags": ["finding"],
                    "confidence": 0.95,
                    "artifact_refs": ["result.txt"],
                }]
            }

    store = KnowledgeStore(tmp_path / "knowledge.db")
    curator = MemoryCurator(CuratorAgent(), store, tmp_path, enabled=True)
    report = CompletionReport(
        status="complete",
        summary="safe summary",
        artifacts=[ArtifactResult(
            path="result.txt",
            description="verified result",
            created_by="worker-1",
            sha256="abc",
            size_bytes=3,
            verified=True,
            verification_method="sha256",
        )],
    )

    import asyncio
    asyncio.run(curator.run_curator(report, {"worker-1": "safe summary"}, "project-1"))

    promoted = store.search(topic="verified result")[0]
    assert promoted.project == "project-1"
    assert promoted.source_run == "project-1:curator"
    assert promoted.source_agent == "curator"
    assert promoted.source_artifact == "result.txt"
    assert promoted.source_artifacts == ["result.txt"]


def test_superseded_knowledge_is_hidden_by_default(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    old = record("old")
    new = record("new")
    store.add(old)
    store.add(new)
    store.supersede("old", "new")

    visible = store.search(topic="deployment")
    all_records = store.search(topic="deployment", include_superseded=True)
    text_visible = store.search_text("deployment")

    assert [item.id for item in visible] == ["new"]
    assert {item.id for item in all_records} == {"old", "new"}
    assert [item.id for item in text_visible] == ["new"]
