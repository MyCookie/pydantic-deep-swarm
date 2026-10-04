"""Every leased readiness phase composes the one real foreground deadline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

import pytest

from test_foreground_contract import (
    OWNER, configuration, endpoint, evidence, finite_case_deadline, manifest, owner, successor,
)


PHASE_OWNER = r'''
import errno, json, os, sys, time
import agent_team.app as boundary
import agent_team.memory.preparation as preparation
mode = os.environ["FIXTURE_MODE"]
def phase_barrier(phase, deadline):
    print(json.dumps({"event":"fixture_barrier","name":phase,"pid":os.getpid()}),flush=True)
    if phase == "recovery":
        # Delay the real watchdog thread across expiry so the caller must check
        # a late recovery return itself. Only this owner process changes scheduling.
        while time.monotonic() < deadline - .1:
            time.sleep(.01)
        sys.setswitchinterval(1)
        while time.monotonic() < deadline + .05:
            pass
        return
    while time.monotonic() < deadline + .05:
        time.sleep(.01)
if mode in {"deadline_knowledge", "migration_fault"}:
    prepare = preparation.prepare_knowledge
    def wrapped(state_dir, *, lease, deadline=None, fault_hook=None):
        def inject(site, context):
            if mode == "deadline_knowledge" and site == "journal.prepared.after":
                phase_barrier("knowledge", deadline)
            if mode == "migration_fault" and site == "journal.intent.after":
                print(json.dumps({"event":"fixture_barrier","name":"migration_fault","pid":os.getpid()}),flush=True)
                raise OSError(errno.ENOSPC, "private fixture disk-full detail")
        return prepare(state_dir, lease=lease, deadline=deadline, fault_hook=inject)
    preparation.prepare_knowledge = wrapped
if mode == "deadline_recovery":
    recover = boundary.SessionStore.recover_incomplete
    def wrapped(self, *, deadline=None):
        result = recover(self, deadline=deadline)
        phase_barrier("recovery", deadline)
        return result
    boundary.SessionStore.recover_incomplete = wrapped
if mode == "deadline_retention":
    sweep = boundary.DurableRetention.sweep
    def wrapped(self, **kwargs):
        assert self.write_guard is not None
        phase_barrier("retention", kwargs["deadline"])
        return sweep(self, **kwargs)
    boundary.DurableRetention.sweep = wrapped
''' + OWNER


# Independent raw SQLite schema and row, deliberately outside KnowledgeStore APIs.
DDL = """CREATE TABLE knowledge_records (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, summary TEXT NOT NULL,
 details TEXT NOT NULL, source_agent TEXT NOT NULL, project TEXT,
 tags TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, supersedes TEXT, source_run TEXT, source_artifact TEXT,
 source_artifacts TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY (supersedes) REFERENCES knowledge_records(id))"""
ROW = ("retained-id", "fixture", "history", "preserved raw text", "worker", None,
       '[ "tag" ]', 0.75, "2020-01-01T00:00:00", "2020-01-01T00:00:00", None,
       "original-run", "original-artifact", '["original-artifact"]')


def seed_legacy(state):
    state.mkdir(parents=True, exist_ok=True)
    path = state / "knowledge.db"
    with sqlite3.connect(path) as connection:
        connection.execute(DDL)
        connection.execute("INSERT INTO knowledge_records VALUES (" + ",".join("?" for _ in ROW) + ")", ROW)
    return path


def private_history(path):
    with tempfile.TemporaryDirectory(prefix="phase-history-") as temporary:
        private = Path(temporary) / "knowledge.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                shutil.copyfile(candidate, Path(str(private) + suffix))
        with sqlite3.connect(private) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(knowledge_records)")]
            return connection.execute("SELECT " + ",".join(columns)
                                      + "," + ",".join(f"typeof({column})" for column in columns)
                                      + " FROM knowledge_records ORDER BY id").fetchall()


@pytest.mark.parametrize("phase", ["models", "knowledge", "recovery", "retention"])
def test_each_leased_phase_obeys_total_startup_deadline(tmp_path, request, phase):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url, knowledge=phase == "knowledge")
        expected_history = None
        if phase == "knowledge":
            legacy = seed_legacy(state)
            expected_history = private_history(legacy)
            source_before = manifest(state)["knowledge.db"]
        if phase == "models":
            catalog.catalog_release.clear()
        with owner(config, tmp_path / "home", mode=f"deadline_{phase}", program=PHASE_OWNER,
                   startup=5, shutdown=5) as process:
            if phase == "models":
                assert catalog.catalog_entered.wait(8)
            else:
                process.wait("fixture_barrier", barrier=phase)
            assert (state / "runtime-owner.json").is_file()
            failed = process.wait("serve_failed", timeout=8)
            assert (failed["reason"], failed["phase"]) == ("startup_timeout", phase)
            proof = process.finish(1, timeout=12)
            assert not any(event["event"] == "serve_ready" for event in process.events)
            assert not (state / "runtime-owner.json").exists()
            if phase == "knowledge":
                assert failed["knowledge"]["status"] == "migration_failed"
                assert failed["knowledge"]["cause"] == "deadline_exceeded"
                assert failed["knowledge"]["phase"] == "PREPARED"
                assert failed["knowledge"]["verification"] == "unknown"
                pending = json.loads((state / "knowledge" / "migration-pending.json").read_text())
                assert pending["phase"] == "PREPARED"
                assert manifest(state)["knowledge.db"] == source_before
                assert private_history(legacy) == expected_history
            catalog.catalog_release.set()
        reused = successor(config, tmp_path / "successor-home")
        if phase == "knowledge":
            assert not (state / "knowledge" / "migration-pending.json").exists()
            assert private_history(state / "knowledge" / "knowledge.db") == expected_history
        evidence(request, f"lifecycle.deadline.{phase}", [proof, reused],
                 startup_budget=5, shutdown_budget=5, lease_reused=True,
                 mutation_quiesced_before_ownership_release=True,
                 pending_migration_preserved=phase == "knowledge", raw_history_preserved=phase == "knowledge")


def test_foreground_migration_fault_preserves_journal_and_resumes(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        legacy = seed_legacy(state)
        before = manifest(state)
        history = private_history(legacy)
        with owner(config, tmp_path / "home", mode="migration_fault", program=PHASE_OWNER) as process:
            process.wait("fixture_barrier", barrier="migration_fault")
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"], failed["knowledge_reason"]) == (
                "runtime_initialization_failed", "knowledge", "disk_full")
            assert failed["knowledge"]["status"] == "migration_failed"
            assert failed["knowledge"]["selected_source"] == "legacy"
            assert failed["knowledge"]["phase"] == "RETIRING"
            assert failed["knowledge"]["verification"] == "unknown"
            assert failed["knowledge"]["history_count"] == 1
            assert len(failed["knowledge"]["history_digest"]) == 64
            proof = process.finish(1)
            assert not any(event["event"] == "serve_ready" for event in process.events)
            pending = state / "knowledge" / "migration-pending.json"
            journal = json.loads(pending.read_text())
            assert journal["phase"] == "RETIRING"
            assert any(move["intent"] and not move["done"] for move in journal["moves"])
            assert manifest(state)["knowledge.db"] == before["knowledge.db"]
            assert private_history(legacy) == history
            journal_digest = hashlib.sha256(pending.read_bytes()).hexdigest()
        reused = successor(config, tmp_path / "successor-home")
        assert not pending.exists()
        assert private_history(state / "knowledge" / "knowledge.db") == history
        assert list((state / "knowledge" / "backups").glob("*/manifest.json"))
        evidence(request, "lifecycle.fault.interrupted-migration", [proof, reused],
                 fault_site="journal.intent.after", pending_phase="RETIRING", journal_digest=journal_digest,
                 original_preserved_until_resumption=True, all_history_raw_types_preserved=True,
                 lease_reused=True, recovery_resumed_by_successor=True)
