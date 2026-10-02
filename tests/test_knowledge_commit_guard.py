"""Real SQLite commit boundaries under the originating runtime lease."""
from __future__ import annotations

import signal
import sqlite3
import time

import pytest

from agent_team.memory import knowledge as knowledge_module
from agent_team.memory.preparation import prepare_knowledge
from agent_team.persistence import RuntimeLease
from test_knowledge_preparation import ROW, archive_evidence, database, physical_evidence, raw


@pytest.fixture(autouse=True)
def commit_case_manifest(request, tmp_path):
    before = physical_evidence(tmp_path)
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise AssertionError("knowledge commit case exceeded its 20-second deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 20)
    try:
        yield
        assert time.monotonic() - started < 20
        request.node.user_properties.append(("knowledge_case_manifest", {
            "schema_version": 1,
            "id": "knowledge." + request.node.name.removeprefix("test_").replace("[", ".").replace("]", ""),
            "case_parameters": getattr(getattr(request.node, "callspec", None), "params", {}),
            "before": before,
            "after": physical_evidence(tmp_path),
            "evidence_scope": "real_lease_sqlite_commit_boundary",
            "snapshot_proofs": archive_evidence(tmp_path),
            "deadline_seconds": 20,
            "elapsed_seconds": time.monotonic() - started,
            "cleanup": "SQLite connections closed; runtime OS lease released by context exit.",
        }))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def history_store(tmp_path, lease):
    path = tmp_path / "knowledge" / "knowledge.db"
    replacement = ("new", *ROW[1:])
    superseded = (*ROW[:10], "new", *ROW[11:])
    database(path, [replacement, superseded])
    prepared = prepare_knowledge(tmp_path, lease=lease)
    assert prepared.result.status == "canonical"
    assert prepared.result.verification == "passed"
    assert prepared.result.selected_source == "canonical"
    assert prepared.result.state_dir == str(tmp_path.resolve())
    return prepared.store, path


def test_delete_rolls_back_after_real_lease_stops_before_commit(tmp_path, monkeypatch, request):
    lock = tmp_path / "runtime.lock"
    with RuntimeLease(lock) as lease:
        store, path = history_store(tmp_path, lease)
        before = raw(path)
        real_connect = sqlite3.connect
        connections = []

        class StoppingConnection(sqlite3.Connection):
            deleted = False
            rolled_back = False
            transaction_closed = False
            connection_closed = False

            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql.startswith("DELETE FROM knowledge_records"):
                    assert cursor.rowcount == 1
                    assert self.in_transaction
                    assert super().execute("SELECT COUNT(*) FROM knowledge_records").fetchone()[0] == 1
                    self.deleted = True
                    lease.mark_stopping()
                return cursor

            def rollback(self):
                super().rollback()
                self.rolled_back = True
                self.transaction_closed = not self.in_transaction

            def close(self):
                super().close()
                self.connection_closed = True

        def connect(*args, **kwargs):
            connection = real_connect(*args, factory=StoppingConnection, **kwargs)
            connections.append(connection)
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(knowledge_module.sqlite3, "connect", connect)
            with pytest.raises(RuntimeError, match="active write generation"):
                store.delete("old")
        assert len(connections) == 1
        assert connections[0].deleted and connections[0].rolled_back
        assert connections[0].transaction_closed and connections[0].connection_closed
        assert lease.is_held
        assert raw(path) == before
    assert not lease.is_held
    request.node.user_properties.append(("knowledge_evidence", {
        "schema_version": 1,
        "id": "knowledge.delete_rolls_back_after_real_lease_stops_before_commit",
        "delete_executed": True,
        "rollback_completed": True,
        "raw_values_and_sql_types_preserved": True,
        "originating_lease_held_until_cleanup": True,
        "lease_released": True,
        "proofs_passed": True,
    }))


@pytest.mark.parametrize("generation", ["stopping", "stale"], ids=["stopping", "stale"])
def test_delete_rejects_inactive_originating_generation(tmp_path, monkeypatch, generation, request):
    with RuntimeLease(tmp_path / "runtime.lock") as lease:
        store, path = history_store(tmp_path, lease)
        before = raw(path)
        original = lease.generation
        if generation == "stopping":
            lease.mark_stopping()
        else:
            lease.release()
            lease.acquire()
            assert lease.generation != original
        connections = []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connections.append(args)
            return real_connect(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(knowledge_module.sqlite3, "connect", connect)
            with pytest.raises(RuntimeError, match="active write generation"):
                store.delete("old")
        assert not connections
        assert lease.is_held
        assert raw(path) == before
    assert not lease.is_held
    request.node.user_properties.append(("knowledge_evidence", {
        "schema_version": 1,
        "id": f"knowledge.delete_rejects_inactive_originating_generation.{generation}",
        "sqlite_not_opened": True,
        "raw_values_and_sql_types_preserved": True,
        "lease_released": True,
        "proofs_passed": True,
    }))


@pytest.mark.parametrize("record_id,removed", [("old", True), ("absent", False)], ids=["existing", "missing"])
def test_delete_commits_under_live_originating_generation(tmp_path, record_id, removed, request):
    with RuntimeLease(tmp_path / "runtime.lock") as lease:
        store, path = history_store(tmp_path, lease)
        before = raw(path)
        assert store.delete(record_id) is removed
        expected = [row for row in before if row[0] != record_id]
        assert raw(path) == expected
        lease.check_generation(lease.generation)
    assert not lease.is_held
    request.node.user_properties.append(("knowledge_evidence", {
        "schema_version": 1,
        "id": "knowledge.delete_commits_under_live_originating_generation." + ("existing" if removed else "missing"),
        "removed": removed,
        "remaining_raw_values_and_sql_types_preserved": True,
        "lease_released": True,
        "proofs_passed": True,
    }))
