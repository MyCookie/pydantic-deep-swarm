"""Concurrency, migration, and integrity tests for AgentMemory."""

from __future__ import annotations

import json
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_team.memory.agent_memory import AgentMemory


def test_thread_writes_reload_under_lock_without_lost_updates(tmp_path):
    first = AgentMemory("agent", tmp_path)
    second = AgentMemory("agent", tmp_path)

    def add(memory, lesson):
        memory.add_lesson(lesson, category="concurrency")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(add, first, "lesson-a"),
            pool.submit(add, second, "lesson-b"),
        ]
        for future in futures:
            future.result()

    lessons = AgentMemory("agent", tmp_path).get_lessons(category="concurrency", limit=100)
    assert {item["lesson"] for item in lessons} == {"lesson-a", "lesson-b"}


def _process_add_lesson(memory_dir: str, barrier, lesson: str) -> None:
    memory = AgentMemory("agent", Path(memory_dir))
    barrier.wait(timeout=5)
    memory.add_lesson(lesson, category="process")


def test_process_writes_reload_under_lock_without_lost_updates(tmp_path):
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("requires a process model with inherited test fixtures")
    context = mp.get_context("fork")
    barrier = context.Barrier(2)
    processes = [
        context.Process(target=_process_add_lesson, args=(str(tmp_path), barrier, "process-a")),
        context.Process(target=_process_add_lesson, args=(str(tmp_path), barrier, "process-b")),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    assert all(process.exitcode == 0 for process in processes)

    lessons = AgentMemory("agent", tmp_path).get_lessons(category="process", limit=100)
    assert {item["lesson"] for item in lessons} == {"process-a", "process-b"}


def test_corrupt_memory_is_backed_up_and_recovered(tmp_path):
    memory_dir = tmp_path / "agent"
    memory_dir.mkdir(parents=True)
    memory_file = memory_dir / "memory.json"
    memory_file.write_text("{not valid json", encoding="utf-8")

    memory = AgentMemory("agent", tmp_path)

    assert memory.get_lessons() == []
    recovered = json.loads(memory_file.read_text(encoding="utf-8"))
    assert recovered["schema_version"] == AgentMemory.CURRENT_SCHEMA_VERSION
    assert list(memory_dir.glob("memory.json.corrupt.*"))


def test_legacy_memory_is_migrated_in_place(tmp_path):
    memory_dir = tmp_path / "agent"
    memory_dir.mkdir(parents=True)
    (memory_dir / "memory.json").write_text(
        json.dumps({
            "agent_id": "agent",
            "scope": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "decisions": [{"id": "d1", "decision": "keep evidence"}],
        }),
        encoding="utf-8",
    )

    memory = AgentMemory("agent", tmp_path)

    assert memory.get_decisions()[0]["decision"] == "keep evidence"
    migrated = json.loads((memory_dir / "memory.json").read_text(encoding="utf-8"))
    assert migrated["schema_version"] == AgentMemory.CURRENT_SCHEMA_VERSION
    assert migrated["preferences"] == {}
    assert migrated["context"] == {}
    assert migrated["lessons"] == []


def test_memory_reads_are_immutable_snapshots(tmp_path):
    memory = AgentMemory("agent", tmp_path)
    memory.set_context("nested", {"values": [1]})
    memory.add_decision("decision", {"nested": {"value": 1}})

    context = memory.get_context("nested")
    context["values"].append(2)
    decisions = memory.get_decisions()
    decisions[0]["context"]["nested"]["value"] = 99
    summary = memory.get_summary()
    summary["recent_lessons"].append({"lesson": "fake"})

    assert memory.get_context("nested") == {"values": [1]}
    assert memory.get_decisions()[0]["context"]["nested"]["value"] == 1
    assert all(item.get("lesson") != "fake" for item in memory.get_summary()["recent_lessons"])


def test_memory_maps_are_bounded(tmp_path):
    memory = AgentMemory("agent", tmp_path)
    for index in range(105):
        memory.set_preference(f"preference-{index}", index)
        memory.set_context(f"context-{index}", index)

    raw = json.loads(memory.memory_file.read_text(encoding="utf-8"))
    assert len(raw["preferences"]) <= AgentMemory.MAX_MAP_ENTRIES
    assert len(raw["context"]) <= AgentMemory.MAX_MAP_ENTRIES


def test_agent_and_scope_identifiers_are_validated(tmp_path):
    with pytest.raises(ValueError):
        AgentMemory("../escape", tmp_path)
    with pytest.raises(ValueError):
        AgentMemory("agent", tmp_path, scope="../escape")
    with pytest.raises(ValueError):
        AgentMemory("agent with spaces", tmp_path)
