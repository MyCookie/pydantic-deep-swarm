"""Retention policy boundaries for prompts, errors, reports, and durable state."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_team.config import Config, MemoryConfig, ModelConfig, RetentionConfig, RuntimeConfig
from agent_team.memory.agent_memory import AgentMemory
from agent_team.memory.knowledge import KnowledgeRecord, KnowledgeStore
from agent_team.observability import StructuredLogger
from agent_team.persistence import SessionStore
from agent_team.retention import DurableRetention


def test_retention_defaults_preserve_existing_behavior_and_validate_positive_bounds(tmp_path):
    defaults = RetentionConfig()
    assert defaults.max_session_messages == 200
    assert defaults.session_max_age_days is None
    assert defaults.project_max_age_days is None
    assert defaults.knowledge_max_age_days is None
    assert defaults.memory_max_age_days is None
    assert defaults.log_max_bytes is None
    assert defaults.log_backup_count == 5

    path = tmp_path / "config.yaml"
    path.write_text(
        """retention:
  max_session_messages: 12
  session_max_age_days: 7
  project_max_age_days: 14
  knowledge_max_age_days: 30
  memory_max_age_days: 45
  log_max_bytes: 4096
  log_backup_count: 3
""",
        encoding="utf-8",
    )
    configured = Config.from_file(path).retention
    assert configured.max_session_messages == 12
    assert configured.session_max_age_days == 7
    assert configured.project_max_age_days == 14
    assert configured.knowledge_max_age_days == 30
    assert configured.memory_max_age_days == 45
    assert configured.log_max_bytes == 4096
    assert configured.log_backup_count == 3

    for field in (
        "max_session_messages",
        "session_max_age_days",
        "project_max_age_days",
        "knowledge_max_age_days",
        "memory_max_age_days",
        "log_max_bytes",
        "log_backup_count",
    ):
        with pytest.raises(ValidationError):
            RetentionConfig(**{field: 0})


def test_session_message_retention_keeps_only_the_newest_configured_messages(tmp_path):
    store = SessionStore(tmp_path / "sessions", max_messages=2)
    session = store.create()

    for index in range(4):
        store.append_message(
            session["session_id"],
            {"role": "user", "content": f"message-{index}"},
        )

    saved = store.get(session["session_id"])
    assert saved is not None
    assert [item["content"] for item in saved["messages"]] == [
        "message-2",
        "message-3",
    ]


def test_retention_sweep_removes_expired_durable_state_but_preserves_active_work(
    tmp_path,
):
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    old = now - timedelta(days=10)
    recent = now - timedelta(hours=1)
    state_dir = tmp_path / "state"
    sessions = SessionStore(state_dir / "sessions")

    expired = sessions.create(client_key="expired")
    sessions.update(expired["session_id"], status="complete")
    processing = sessions.create(client_key="processing")
    sessions.update(processing["session_id"], status="processing")
    current = sessions.create(client_key="current")
    sessions.update(current["session_id"], status="complete")

    for session_id, timestamp in (
        (expired["session_id"], old),
        (processing["session_id"], old),
        (current["session_id"], recent),
    ):
        path = state_dir / "sessions" / f"{session_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = timestamp.isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")

    def create_family(root_name: str, item_id: str, timestamp: datetime) -> Path:
        directory = state_dir / root_name / item_id
        directory.mkdir(parents=True)
        payload = directory / "data.json"
        payload.write_text("{}", encoding="utf-8")
        epoch = timestamp.timestamp()
        os.utime(payload, (epoch, epoch))
        os.utime(directory, (epoch, epoch))
        return directory

    for root_name in ("projects", "artifacts", "memory"):
        create_family(root_name, "expired-project", old)
        create_family(root_name, "current-project", recent)
    workspace_file = create_family("workspace", "expired-project", old) / "artifact.txt"
    workspace_file.write_text("keep", encoding="utf-8")

    knowledge = KnowledgeStore(state_dir / "knowledge.db")
    for record_id in ("expired-knowledge", "current-knowledge"):
        knowledge.add(KnowledgeRecord(
            id=record_id,
            topic=record_id,
            summary="summary",
            details="details",
            source_agent="worker",
            tags=[],
            created_at=now,
            updated_at=now,
        ))
    with sqlite3.connect(knowledge.db_path) as connection:
        connection.execute(
            "UPDATE knowledge_records SET updated_at=? WHERE id=?",
            (old.isoformat(), "expired-knowledge"),
        )

    result = DurableRetention(
        state_dir,
        RetentionConfig(
            session_max_age_days=7,
            project_max_age_days=7,
            knowledge_max_age_days=7,
            memory_max_age_days=7,
        ),
    ).sweep(session_store=sessions, knowledge_store=knowledge, now=now)

    assert sessions.get(expired["session_id"]) is None
    assert sessions.get(processing["session_id"]) is not None
    assert sessions.get(current["session_id"]) is not None
    for root_name in ("projects", "artifacts", "memory"):
        assert not (state_dir / root_name / "expired-project").exists()
        assert (state_dir / root_name / "current-project").is_dir()
    assert workspace_file.read_text(encoding="utf-8") == "keep"
    assert knowledge.get("expired-knowledge") is None
    assert knowledge.get("current-knowledge") is not None
    assert result.sessions == 1
    assert result.projects == 1
    assert result.artifacts == 1
    assert result.memory_scopes == 1
    assert result.knowledge_records == 1


def test_memory_retention_prunes_actual_agent_memory_layout(tmp_path):
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    old = now - timedelta(days=10)
    recent = now - timedelta(hours=1)
    state_dir = tmp_path / "state"
    sessions = SessionStore(state_dir / "sessions")
    expired = AgentMemory("expired-role", state_dir / "memory")
    current = AgentMemory("current-role", state_dir / "memory")
    expired.set_preference("status", "old")
    current.set_preference("status", "current")

    for memory, timestamp in ((expired, old), (current, recent)):
        epoch = timestamp.timestamp()
        for path in [*memory.memory_dir.iterdir(), memory.memory_dir]:
            os.utime(path, (epoch, epoch))

    result = DurableRetention(
        state_dir,
        RetentionConfig(memory_max_age_days=7),
    ).sweep(session_store=sessions, now=now)

    assert not expired.memory_dir.exists()
    assert current.memory_file.is_file()
    assert result.memory_scopes == 1


def test_rotating_logs_are_bounded_and_redacted(tmp_path, monkeypatch):
    secret = "retention-secret-value"
    monkeypatch.setenv("AGENT_TEAM_API_TOKEN", secret)
    logger = StructuredLogger(
        "retention-test",
        log_dir=tmp_path,
        max_bytes=220,
        backup_count=2,
    )
    try:
        for index in range(20):
            logger.info(
                "retention_event",
                index=index,
                prompt=f"prompt-{index} token={secret} " + ("x" * 80),
            )
    finally:
        logger.close()

    logs = sorted(tmp_path.glob("retention-test.jsonl*"))
    assert 1 <= len(logs) <= 3
    assert all(secret not in path.read_text(encoding="utf-8") for path in logs)


@pytest.mark.asyncio
async def test_runtime_startup_recovers_then_applies_configured_retention(
    tmp_path,
    monkeypatch,
):
    import agent_team.app as app_module

    state_dir = tmp_path / "state"
    seed = SessionStore(state_dir / "sessions")
    expired = seed.create()
    seed.update(expired["session_id"], status="complete")
    interrupted = seed.create()
    seed.update(interrupted["session_id"], status="processing")
    for session_id in (expired["session_id"], interrupted["session_id"]):
        path = state_dir / "sessions" / f"{session_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(days=10)
        ).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")

    models = {
        role: ModelConfig(model="test-model", base_url="http://model")
        for role in ("principal", "manager", "worker", "curator")
    }
    config = Config(
        runtime=RuntimeConfig(state_dir=state_dir),
        models=models,
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        retention=RetentionConfig(
            max_session_messages=3,
            session_max_age_days=7,
        ),
    )
    old_state = (
        app_module.config,
        app_module.engine,
        app_module.session_store,
        app_module.runtime_lease,
        app_module.logger,
    )
    startup_logger = StructuredLogger("startup-retention-test", log_dir=tmp_path / "initial-logs")
    app_module.config = None
    app_module.engine = None
    app_module.session_store = None
    app_module.runtime_lease = None
    app_module.logger = startup_logger
    monkeypatch.setattr(app_module, "get_config", lambda _path=None: config)

    try:
        app_module._initialize_runtime()
        assert app_module.session_store is not None
        assert app_module.session_store.max_messages == 3
        assert startup_logger.log_dir == state_dir / "logs"
        assert (state_dir / "logs" / "startup-retention-test.jsonl").is_file()
        assert app_module.session_store.get(expired["session_id"]) is None
        recovered = app_module.session_store.get(interrupted["session_id"])
        assert recovered is not None
        assert recovered["status"] == "blocked"
    finally:
        startup_logger.close()
        if app_module.engine is not None:
            await app_module.engine.close()
        if app_module.runtime_lease is not None:
            app_module.runtime_lease.release()
        (
            app_module.config,
            app_module.engine,
            app_module.session_store,
            app_module.runtime_lease,
            app_module.logger,
        ) = old_state
