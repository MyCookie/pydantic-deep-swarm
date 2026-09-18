"""Concurrency and injection-resistance tests for the knowledge store."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import pytest

from agent_team.memory.knowledge import KnowledgeRecord, KnowledgeStore
from agent_team.memory.curator import MemoryCurator
from agent_team.contracts import CompletionReport


def make_record(record_id: str, summary: str | None = None) -> KnowledgeRecord:
    now = datetime.utcnow()
    return KnowledgeRecord(
        id=record_id,
        topic="deployment",
        summary=summary or record_id,
        details="durable detail",
        source_agent="test",
        tags=["test"],
        confidence=0.9,
        created_at=now,
        updated_at=now,
    )


def add_process_batch(args: tuple[str, int]) -> int:
    path, batch = args
    store = KnowledgeStore(path)
    for index in range(8):
        store.add(make_record(f"batch-{batch}-{index}"))
    return store.count()


def test_store_configures_wal_and_busy_timeout(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")

    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000


def test_add_is_idempotent_without_overwriting_existing_record(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.add(make_record("same", summary="first"))

    assert store.add(make_record("same", summary="second")) == "same"
    assert store.count() == 1
    assert store.get("same").summary == "first"


def test_supersede_is_idempotent_and_keeps_one_visible_record(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.add(make_record("old"))
    store.add(make_record("new"))

    assert store.supersede("old", "new") is True
    assert store.supersede("old", "new") is True
    assert store.get("old").supersedes == "new"
    assert [item.id for item in store.search(topic="deployment")] == ["new"]


@pytest.mark.asyncio
async def test_curator_returns_the_canonical_persisted_record(tmp_path):
    class CuratorAgent:
        async def run(self, prompt, response_format=None):
            return {"records": [{"topic": "durable", "summary": "fact"}]}

    store = KnowledgeStore(tmp_path / "knowledge.db")
    curator = MemoryCurator(CuratorAgent(), store, tmp_path, enabled=True)
    report = CompletionReport(status="complete", summary="done")

    promoted = await curator.run_curator(report, {}, "project-1")

    assert promoted[0].updated_at == store.get(promoted[0].id).updated_at


def test_text_search_treats_sql_syntax_as_literal_text(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.add(make_record("safe"))

    malicious = "' OR 1=1 --"
    assert store.search(topic=malicious) == []
    assert store.search_text(malicious) == []


def test_concurrent_process_writes_preserve_all_records(tmp_path):
    path = str(tmp_path / "knowledge.db")
    with ProcessPoolExecutor(max_workers=4) as executor:
        counts = list(executor.map(add_process_batch, [(path, index) for index in range(4)]))

    assert counts
    store = KnowledgeStore(path)
    assert store.count() == 32
    assert len(store.search(limit=100)) == 32
