"""Independent SQL/history oracles for canonical preparation and crash recovery."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import shutil
import stat
import time
from pathlib import Path

import pytest

from agent_team.memory.preparation import FAULT_SITES, KnowledgePreparationError, inspect_knowledge, prepare_knowledge
from agent_team.persistence import RuntimeLease, RuntimeLeaseInspection

# Deliberately independent of production DDL and record/model serialization.
DDL = """CREATE TABLE knowledge_records (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, summary TEXT NOT NULL,
 details TEXT NOT NULL, source_agent TEXT NOT NULL, project TEXT,
 tags TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, supersedes TEXT, source_run TEXT, source_artifact TEXT,
 source_artifacts TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY (supersedes) REFERENCES knowledge_records(id))"""
ROW = ("old", "topic", "summary", "secret-shaped-original", "worker", "project", '[ "a" ]', 4.25, "2020-01-01T01:02:03", "2021-02-03T04:05:06+00:00", None, "run", "source.txt", '["source.txt", "other.txt"]')


def database(path: Path, rows: list[tuple] | None = None, old: bool = False, wal: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(DDL.replace(" source_artifacts TEXT NOT NULL DEFAULT '[]',", "") if old else DDL)
    for row in rows or []:
        values = row[:-1] if old else row
        connection.execute("INSERT INTO knowledge_records VALUES (" + ",".join("?" for _ in values) + ")", values)
    connection.commit()
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    else:
        connection.close()
    return connection if wal else None


def raw(path: Path):
    connection = sqlite3.connect(path)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(knowledge_records)")]
    names = ",".join(columns)
    rows = connection.execute(f"SELECT {names}," + ",".join(f"typeof({name})" for name in columns) + " FROM knowledge_records ORDER BY id").fetchall()
    connection.close()
    return rows


def inventory(root: Path):
    return {str(path.relative_to(root)): (path.stat().st_mode & 0o777, path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest()) for path in root.rglob("*") if path.is_file()}


def physical_evidence(root: Path):
    """Safe persisted inventory: names, metadata and hashes, never file contents."""
    manifest = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "nonregular"
        manifest[str(path.relative_to(root))] = {"kind": kind, "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino, "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else None}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"manifest_digest": hashlib.sha256(encoded).hexdigest(), "entry_count": len(manifest), "manifest": manifest}


def history_evidence(path: Path, expected_rows: list[tuple] | None = None):
    """Independent raw SQLite oracle on a private complete bundle only."""
    if not path.exists() or path.is_symlink() or not path.is_file():
        return {"present": False, "valid": None, "count": None, "digest": None, "raw_value_type_match": None}
    try:
        with tempfile.TemporaryDirectory(prefix="knowledge-proof-") as temporary:
            private = Path(temporary) / "knowledge.db"
            for suffix in ("", "-wal", "-shm", "-journal"):
                source = Path(str(path) + suffix)
                if source.exists():
                    shutil.copyfile(source, Path(str(private) + suffix))
                    os.chmod(Path(str(private) + suffix), 0o600)
            connection = sqlite3.connect(private)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                foreign_keys = not connection.execute("PRAGMA foreign_key_check").fetchall()
                columns = [row[1] for row in connection.execute("PRAGMA table_info(knowledge_records)")]
                names = ",".join(columns)
                rows = connection.execute(f"SELECT {names}," + ",".join(f"typeof({name})" for name in columns) + " FROM knowledge_records ORDER BY id").fetchall()
            finally:
                connection.close()
            # Normalize only the old column's literal SQL default, independently
            # of production digest code and model/redaction APIs.
            normalized = []
            for row in rows:
                count = len(columns)
                values, types = list(row[:count]), list(row[count:])
                if count == 13:
                    values.append("[]"); types.append("text")
                normalized.append({"values": values, "types": types})
            digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            expected = []
            if expected_rows is not None:
                for row in sorted(expected_rows, key=lambda item: item[0]):
                    expected.append({"values": list(row), "types": ["null" if value is None else "text" if isinstance(value, str) else "real" if isinstance(value, float) else "integer" if isinstance(value, int) else "blob" for value in row]})
            proof = {"present": True, "valid": integrity and foreign_keys, "integrity_check": integrity, "foreign_key_check": foreign_keys, "schema_columns": len(columns), "count": len(rows), "digest": digest, "raw_value_type_match": normalized == expected if expected_rows is not None else None}
        proof["private_copy_cleaned"] = not Path(temporary).exists()
        return proof
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        return {"present": True, "valid": False, "count": None, "digest": None, "raw_value_type_match": False, "reason": type(error).__name__}


def archive_evidence(root: Path, expected_by_source: dict[str, list[tuple]] | None = None):
    proofs = {}
    for path in sorted((root / "knowledge" / "backups").glob("*/*/snapshot.db")):
        proof = history_evidence(path, expected_by_source.get(path.parent.name) if expected_by_source is not None else None)
        proof["self_contained"] = not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        proof["mode"] = stat.S_IMODE(path.stat().st_mode)
        proofs[str(path.relative_to(root))] = proof
    return proofs


def result_evidence(result):
    return {key: getattr(result, key) for key in ("status", "reason", "phase", "history_count", "history_digest", "recovered", "upgraded", "warnings")}


def record_evidence(request, value):
    request.node.user_properties.append(("knowledge_evidence", {"schema_version": 1, **value}))


def fault_source_oracles(branch):
    expected = {"legacy-root": [ROW]} if branch != "fresh" else {}
    if branch in ("retained", "retained_wal"):
        expected["canonical-nested"] = [ROW]
    elif branch == "replacement":
        expected["canonical-nested"] = []
    return expected


@pytest.fixture(autouse=True)
def retained_case_manifest(request, tmp_path):
    """Retain safe filesystem proof for each ordinary storage case as well."""
    before = physical_evidence(tmp_path)
    yield
    if request.node.originalname in ("test_every_fault_site_and_occurrence", "test_real_process_crash_at_every_fault_site"):
        return
    after = physical_evidence(tmp_path)
    parameters = getattr(getattr(request.node, "callspec", None), "params", {})
    request.node.user_properties.append(("knowledge_case_manifest", {"schema_version": 1, "id": "knowledge." + request.node.name.removeprefix("test_").replace("[", ".").replace("]", ""), "case_parameters": parameters, "before": before, "after": after, "evidence_scope": "ordinary_case_filesystem", "snapshot_proofs": archive_evidence(tmp_path)}))


def run(root: Path, **kwargs):
    with RuntimeLease(root / "runtime.lock") as lease:
        return prepare_knowledge(root, lease=lease, **kwargs)


@pytest.mark.parametrize("branch,expected", [
    ("fresh", "created"), ("legacy", "migrated"), ("canonical", "canonical"),
    ("both_empty", "reconciled"), ("legacy_populated", "reconciled"),
    ("canonical_populated", "reconciled"), ("identical", "reconciled"),
    ("legacy_old", "migrated"), ("canonical_old", "canonical"), ("old_equal", "reconciled"),
])
def test_selection_and_raw_history(tmp_path, branch, expected, request):
    legacy = tmp_path / "knowledge.db"
    canonical = tmp_path / "knowledge" / "knowledge.db"
    populated = branch not in ("fresh", "both_empty")
    wanted = tuple(ROW[:-1]) + (("[]",) if branch in ("legacy_old", "canonical_old", "old_equal") else (ROW[-1],))
    if branch in ("legacy", "legacy_populated", "identical", "legacy_old", "old_equal"):
        database(legacy, [wanted], old=branch in ("legacy_old", "old_equal"))
    elif branch in ("both_empty", "canonical_populated"):
        database(legacy)
    if branch in ("canonical", "canonical_populated", "identical", "canonical_old", "old_equal"):
        database(canonical, [wanted], old=branch == "canonical_old")
    elif branch in ("both_empty", "legacy_populated"):
        database(canonical)
    expected_snapshots = {}
    if legacy.exists():
        expected_snapshots["legacy-root"] = [wanted] if branch in ("legacy", "legacy_populated", "identical", "legacy_old", "old_equal") else []
    if canonical.exists():
        expected_snapshots["canonical-nested"] = [wanted] if branch in ("canonical", "canonical_populated", "identical", "canonical_old", "old_equal") else []
    inode = canonical.stat().st_ino if canonical.exists() and branch in ("canonical_populated", "identical", "old_equal", "both_empty") else None
    result = run(tmp_path)
    assert result.result.status == expected
    assert result.store.db_path == canonical
    assert not legacy.exists()
    records = raw(canonical)
    assert len(records) == (1 if populated else 0)
    if populated:
        assert tuple(records[0][:14]) == wanted
    if inode is not None:
        assert canonical.stat().st_ino == inode
    if branch in ("both_empty", "legacy_populated", "canonical_populated", "identical", "old_equal"):
        archive = Path(result.result.archive_path)
        assert (archive / "legacy-root" / "snapshot.db").exists()
        assert (archive / "canonical-nested" / "snapshot.db").exists()
    repeat = run(tmp_path)
    assert repeat.result.status == "canonical"
    proof = history_evidence(canonical, [wanted] if populated else [])
    assert proof["valid"] and proof["raw_value_type_match"]
    snapshots = archive_evidence(tmp_path, expected_snapshots)
    assert all(snapshot["valid"] and snapshot["self_contained"] and snapshot["raw_value_type_match"] for snapshot in snapshots.values())
    record_evidence(request, {"id": f"knowledge.selection_and_raw_history.{branch}-{expected}", "expected": {"status": [expected], "count": int(populated), "repeat_status": "canonical"}, "observed": result_evidence(result.result), "repeat": result_evidence(repeat.result), "history": proof, "authority": "canonical", "retained_inode_verified": inode is None or canonical.stat().st_ino == inode, "snapshots": snapshots, "proofs_passed": True})


@pytest.mark.parametrize("field", range(14))
def test_every_persisted_field_diverges(tmp_path, field):
    original = tuple(ROW[:-1]) + ("[]",)
    changed = list(original)
    if field == 7:
        changed[field] = 0.1
    elif field in (6, 13):
        changed[field] = '["different"]'
    elif field in (8, 9):
        changed[field] = "2024-01-01T00:00:00"
    elif field == 10:
        changed[field] = "other"
    else:
        changed[field] = "different"
    peer = list(original); peer[0] = "other"
    database(tmp_path / "knowledge.db", [original, tuple(peer)])
    database(tmp_path / "knowledge" / "knowledge.db", [tuple(changed), tuple(peer)])
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status == "ambiguous"
    assert (tmp_path / "knowledge.db").exists()


@pytest.mark.parametrize("problem", ["zero", "corrupt", "unknown_table", "unknown_index", "wrong_index", "trigger", "view", "unknown_column", "version", "constraints", "foreign_key", "null_id", "json", "timestamp", "tags_type", "artifact_type", "required_type"])
def test_invalid_candidates_never_hidden_by_empty_peer(tmp_path, problem):
    legacy = tmp_path / "knowledge.db"
    database(tmp_path / "knowledge" / "knowledge.db")
    if problem in ("zero", "corrupt"):
        legacy.write_bytes(b"" if problem == "zero" else b"garbage")
    else:
        database(legacy, [ROW])
        connection = sqlite3.connect(legacy)
        statements = {"unknown_table": "CREATE TABLE alien(x)", "unknown_index": "CREATE INDEX alien ON knowledge_records(topic)", "wrong_index": "CREATE INDEX idx_topic ON knowledge_records(project)", "trigger": "CREATE TRIGGER alien AFTER INSERT ON knowledge_records BEGIN SELECT 1; END", "view": "CREATE VIEW alien AS SELECT * FROM knowledge_records", "unknown_column": "ALTER TABLE knowledge_records ADD COLUMN alien TEXT", "version": "PRAGMA user_version=999", "foreign_key": "UPDATE knowledge_records SET supersedes='absent'", "null_id": "UPDATE knowledge_records SET id=NULL", "json": "UPDATE knowledge_records SET tags='bad'", "timestamp": "UPDATE knowledge_records SET created_at='yesterday'", "tags_type": "UPDATE knowledge_records SET tags='{}'", "artifact_type": "UPDATE knowledge_records SET source_artifacts='[1]'", "required_type": "UPDATE knowledge_records SET topic=x'0102'"}
        if problem == "constraints":
            connection.execute("DROP TABLE knowledge_records")
            connection.execute(DDL.replace("topic TEXT NOT NULL", "topic TEXT NOT NULL CHECK(length(topic)>0)"))
        else:
            connection.execute(statements[problem])
        connection.commit(); connection.close()
    before = inventory(tmp_path)
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status in ("invalid", "unsupported_schema")
    after = inventory(tmp_path)
    assert {k: v for k, v in after.items() if k != "runtime.lock"} == before


@pytest.mark.parametrize("unsafe", ["symlink_database", "symlink_directory", "hardlink", "fifo", "orphan_wal", "orphan_shm", "orphan_journal"])
def test_unsafe_bundles(tmp_path, unsafe):
    source = tmp_path / "knowledge.db"
    if unsafe == "symlink_database":
        database(tmp_path / "outside.db")
        source.symlink_to(tmp_path / "outside.db")
    elif unsafe == "symlink_directory":
        (tmp_path / "other").mkdir(); (tmp_path / "knowledge").symlink_to(tmp_path / "other", target_is_directory=True)
    elif unsafe == "hardlink":
        database(source); (tmp_path / "knowledge").mkdir(); os.link(source, tmp_path / "knowledge" / "knowledge.db")
    elif unsafe == "fifo":
        os.mkfifo(source)
    else:
        Path(str(source) + "-" + unsafe.removeprefix("orphan_")).write_bytes(b"evidence")
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status in ("unsafe_path", "orphan_sidecar")


def test_wal_history_snapshot_and_retained_inode(tmp_path, request):
    legacy = tmp_path / "knowledge.db"; canonical = tmp_path / "knowledge" / "knowledge.db"
    database(legacy, [ROW])
    connection = database(canonical, [], wal=True)
    connection.execute("INSERT INTO knowledge_records VALUES (" + ",".join("?" for _ in ROW) + ")", ROW); connection.commit()
    before = inventory(tmp_path)
    result = run(tmp_path)
    after = inventory(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        name = "knowledge/knowledge.db" + suffix
        assert after[name] == before[name]
    assert raw(Path(result.result.archive_path) / "canonical-nested" / "snapshot.db")[0][:14] == ROW
    proof = history_evidence(canonical, [ROW])
    assert proof["valid"] and proof["raw_value_type_match"]
    record_evidence(request, {"id": "knowledge.wal_history_snapshot_and_retained_inode", "expected": {"status": ["reconciled"], "count": 1}, "observed": result_evidence(result.result), "history": proof, "snapshots": archive_evidence(tmp_path, {"legacy-root": [ROW], "canonical-nested": [ROW]}), "retained_bundle_unchanged": True, "proofs_passed": True})
    connection.close()


def test_hot_journal_is_recovered_only_privately(tmp_path, request):
    source = tmp_path / "knowledge.db"
    database(source, [ROW])
    code = "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE knowledge_records SET details=printf('%.*c', 200000, 'x')\"); os._exit(0)"
    subprocess.run([sys.executable, "-c", code, str(source)], check=True)
    assert Path(str(source) + "-journal").exists()
    with RuntimeLease(tmp_path / "runtime.lock"):
        pass
    before = inventory(tmp_path)
    assert inspect_knowledge(tmp_path).result.status == "migration_needed"
    assert inventory(tmp_path) == before
    result = run(tmp_path)
    assert raw(result.store.db_path)[0][:14] == ROW
    assert (Path(result.result.archive_path) / "legacy-root" / "original.db-journal").exists()
    proof = history_evidence(result.store.db_path, [ROW])
    assert proof["valid"] and proof["raw_value_type_match"]
    record_evidence(request, {"id": "knowledge.hot_journal_is_recovered_only_privately", "expected": {"status": ["migrated"], "count": 1}, "observed": result_evidence(result.result), "history": proof, "snapshots": archive_evidence(tmp_path, {"legacy-root": [ROW]}), "doctor_nonmutation": True, "original_journal_preserved": True, "proofs_passed": True})


@pytest.mark.parametrize("state", ["missing", "fresh", "legacy", "canonical", "busy", "expired"])
def test_doctor_nonmutation_and_guard(tmp_path, state, request):
    if state in ("legacy", "busy"):
        database(tmp_path / "knowledge.db", [ROW])
    if state == "canonical":
        database(tmp_path / "knowledge" / "knowledge.db", [ROW])
    lease = RuntimeLease(tmp_path / "runtime.lock")
    if state != "missing":
        lease.acquire()
        if state != "busy":
            lease.release()
    before = inventory(tmp_path)
    result = inspect_knowledge(tmp_path, deadline=time.monotonic() - 1 if state == "expired" else None)
    assert result.result.status == {"missing": "inspection_unavailable", "fresh": "needs_initialization", "legacy": "migration_needed", "canonical": "canonical", "busy": "inspection_deferred", "expired": "inspection_unavailable"}[state]
    assert inventory(tmp_path) == before
    record_evidence(request, {"id": f"knowledge.doctor_nonmutation_and_guard.{state}", "expected": {"status": [{"missing": "inspection_unavailable", "fresh": "needs_initialization", "legacy": "migration_needed", "canonical": "canonical", "busy": "inspection_deferred", "expired": "inspection_unavailable"}[state]]}, "observed": result_evidence(result.result), "lease_status": result.lease_status, "nonmutation": True, "proofs_passed": True})
    lease.release()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "damaged_audit_missing", "damaged_audit_valid", "empty_backup"])
def test_completed_authority_never_restores(tmp_path, damage):
    canonical = tmp_path / "knowledge" / "knowledge.db"
    if damage == "empty_backup":
        (tmp_path / "knowledge" / "backups" / "empty").mkdir(parents=True)
        assert run(tmp_path).result.status == "created"
        return
    result = run(tmp_path)
    connection = sqlite3.connect(canonical)
    connection.execute("INSERT INTO knowledge_records VALUES (" + ",".join("?" for _ in ROW) + ")", ROW); connection.commit(); connection.close()
    if damage.startswith("damaged_audit"):
        (Path(result.result.archive_path) / "manifest.json").write_text("broken")
    if damage in ("missing", "damaged_audit_missing"):
        canonical.unlink(); database(tmp_path / "knowledge.db", [ROW])
    elif damage == "corrupt":
        canonical.write_bytes(b"corrupt")
    if damage == "damaged_audit_valid":
        again = run(tmp_path)
        assert again.result.warnings == ("historical_audit_unverified",)
        assert raw(canonical)[0][:14] == ROW
    else:
        with pytest.raises(KnowledgePreparationError) as failure:
            run(tmp_path)
        assert failure.value.result.status in ("invalid", "recovery_required")


def fault_fixture(root: Path, branch: str):
    database(root / "knowledge.db", [ROW] if branch != "fresh" else []) if branch != "fresh" else None
    if branch == "retained":
        database(root / "knowledge" / "knowledge.db", [ROW])
    elif branch == "replacement":
        database(root / "knowledge" / "knowledge.db")
    elif branch in ("wal", "hot", "retained_wal"):
        source = root / "knowledge.db"
        if branch == "retained_wal":
            source = root / "knowledge" / "knowledge.db"
            database(source, [ROW])
        if branch in ("wal", "retained_wal"):
            code = "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); c.execute(\"UPDATE knowledge_records SET summary='summary'\"); c.commit(); os._exit(0)"
        else:
            code = "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE knowledge_records SET details=printf('%.*c', 200000, 'x')\"); os._exit(0)"
        subprocess.run([sys.executable, "-c", code, str(source)], check=True)


@pytest.mark.parametrize("site", FAULT_SITES)
def test_every_fault_site_and_occurrence(tmp_path, site, request):
    coverage = []
    for branch in ("fresh", "legacy", "retained", "replacement", "wal", "hot", "retained_wal"):
        reference = tmp_path / (branch + "-reference")
        fault_fixture(reference, branch)
        hits = []
        run(reference, fault_hook=lambda current, context: hits.append((current, context)))
        occurrences = [context for current, context in hits if current == site]
        for index, context in enumerate(occurrences):
            case = tmp_path / f"{branch}-{index}"
            fault_fixture(case, branch)
            before = physical_evidence(case)
            original = {name: path.read_bytes() for name in ("knowledge.db", "knowledge/knowledge.db") if (path := case / name).exists()}
            seen = 0
            def crash(current, current_context):
                nonlocal seen
                if current == site:
                    if seen == index:
                        raise RuntimeError("injected crash")
                    seen += 1
            with pytest.raises(RuntimeError, match="injected crash"):
                run(case, fault_hook=crash)
            assert seen == index
            interrupted = physical_evidence(case)
            pending = case / "knowledge" / "migration-pending.json"
            provable_pending = pending.exists()
            expected_rows = [] if branch == "fresh" else [ROW]
            disposition = None
            history = None
            # A proven pending operation rolls forward. An incomplete pre-journal
            # archive blocks instead; neither branch silently chooses blank state.
            try:
                recovered = run(case)
            except KnowledgePreparationError as error:
                assert error.result.status == "recovery_required"
                assert not provable_pending, f"Proven journal did not roll forward at {branch}/{site}/{index}: {error}"
                assert any((case / name).exists() for name in original) or branch == "fresh"
                disposition = result_evidence(error.result)
                expected_status = ["recovery_required"]
                expected_reason = "evidence_unverified"
                assert error.result.reason == expected_reason
                history = history_evidence(case / "knowledge.db", expected_rows)
                if branch != "fresh":
                    assert history["valid"] and history["raw_value_type_match"]
            else:
                assert recovered.store.db_path.exists()
                rows = raw(recovered.store.db_path)
                assert len(rows) == (0 if branch == "fresh" else 1)
                if rows:
                    assert rows[0][:14] == ROW
                disposition = result_evidence(recovered.result)
                expected_status = ["created", "canonical"] if branch == "fresh" else ["migrated", "canonical"] if branch in ("legacy", "wal", "hot") else ["reconciled", "canonical"]
                assert recovered.result.status in expected_status
                expected_reason = None
                history = history_evidence(recovered.store.db_path, expected_rows)
                assert history["valid"] and history["raw_value_type_match"]
            after = physical_evidence(case)
            snapshots = archive_evidence(case, fault_source_oracles(branch))
            if provable_pending:
                assert all(proof["valid"] and proof["self_contained"] and proof["raw_value_type_match"] for proof in snapshots.values())
            expected_phase = ["COMPLETED"] if provable_pending else [None, "COMPLETED"]
            assert disposition["phase"] in expected_phase
            record_evidence(request, {
                "id": f"knowledge.fault.{branch}.{site}.{index}", "site": site, "branch": branch, "occurrence": index,
                "expected": {"status": expected_status, "reason": expected_reason, "count": len(expected_rows), "phase": expected_phase, "pending_requires_roll_forward": provable_pending},
                "observed": disposition, "authority": "canonical" if disposition["status"] != "recovery_required" else None,
                "before": before, "interrupted": interrupted, "after": after,
                "history": history, "snapshots": snapshots,
                "snapshot_validation": "not_required_no_sources" if branch == "fresh" else "verified" if snapshots and all(proof["valid"] and proof["self_contained"] and proof["raw_value_type_match"] for proof in snapshots.values()) else "not_established_before_preparation",
                "proofs_passed": True, "private_proofs_cleaned": history.get("private_copy_cleaned", True),
            })
            coverage.append((branch, index, context))
    assert coverage, f"Actual fault site {site} was not exercised"


@pytest.mark.parametrize("damage", ["version", "path", "snapshot", "candidate", "missing_snapshot", "changed_retained", "changed_source", "unknown_pending", "fingerprint_shape"])
def test_unprovable_recovery_is_preserved(tmp_path, damage):
    fault_fixture(tmp_path, "retained" if damage == "changed_retained" else "legacy")
    def crash(site, context):
        if site == "journal.prepared.after":
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        run(tmp_path, fault_hook=crash)
    pending = tmp_path / "knowledge" / "migration-pending.json"
    value = json.loads(pending.read_text())
    if damage == "version": value["version"] = 9
    elif damage == "path": value["candidate"] = "../escape.db"
    elif damage == "fingerprint_shape": value["candidate_fingerprint"] = {}
    elif damage == "snapshot": (tmp_path / value["snapshots"]["legacy"]["path"]).write_bytes(b"corrupt")
    elif damage == "missing_snapshot": (tmp_path / value["snapshots"]["legacy"]["path"]).unlink()
    elif damage == "candidate": (tmp_path / value["candidate"]).write_bytes(b"corrupt")
    elif damage == "changed_source": (tmp_path / "knowledge.db").write_bytes(b"different")
    elif damage == "changed_retained": (tmp_path / "knowledge" / "knowledge.db").write_bytes(b"different")
    elif damage == "unknown_pending": (pending.parent / "other-pending.json").write_text("{}")
    if damage in ("version", "path", "fingerprint_shape"):
        pending.write_text(json.dumps(value))
    before = inventory(tmp_path)
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status == "recovery_required"
    assert {k: v for k, v in inventory(tmp_path).items() if k != "runtime.lock"} == {k: v for k, v in before.items() if k != "runtime.lock"}
    assert inspect_knowledge(tmp_path).result.status == "recovery_required"


def test_deadline_and_lease_generation(tmp_path):
    with RuntimeLease(tmp_path / "runtime.lock") as lease:
        with pytest.raises(KnowledgePreparationError) as failure:
            prepare_knowledge(tmp_path, lease=lease, deadline=time.monotonic() - 1)
        assert failure.value.result.reason == "deadline_exceeded"
        prepared = prepare_knowledge(tmp_path, lease=lease)
        lease.mark_stopping()
        with pytest.raises(RuntimeError, match="write generation"):
            prepared.store._check_write()
    with pytest.raises(KnowledgePreparationError, match="runtime_lease_required"):
        prepare_knowledge(tmp_path, lease=lease)


def test_real_sqlite_writer_contention(tmp_path):
    database(tmp_path / "knowledge.db", [ROW])
    code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); sys.stdin.read()"
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "knowledge.db")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(KnowledgePreparationError) as failure:
            run(tmp_path)
        assert failure.value.result.reason == "sqlite_contention"
    finally:
        process.communicate(""); process.wait(timeout=5)


@pytest.mark.parametrize("difference", ["json_spelling", "timestamp_spelling", "nullable_field"])
def test_semantic_equivalence_does_not_replace_exact_sql_identity(tmp_path, difference):
    row = list(ROW)
    if difference == "json_spelling": row[6] = '["a"]'
    elif difference == "timestamp_spelling": row[9] = "2021-02-03T04:05:06Z"
    else: row[11] = None
    database(tmp_path / "knowledge.db", [ROW]); database(tmp_path / "knowledge" / "knowledge.db", [tuple(row)])
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status == "ambiguous"


@pytest.mark.parametrize("failure", ["disk_full", "permission", "changed_source", "deadline_during_copy"])
def test_io_failures_preserve_sources_and_durable_phase(tmp_path, failure, monkeypatch):
    import agent_team.memory.preparation as module
    database(tmp_path / "knowledge.db", [ROW])
    before = (tmp_path / "knowledge.db").read_bytes()
    def hook(site, context):
        if site == "bundle.copy.before":
            if failure == "disk_full": raise OSError(28, "SECRET disk full")
            if failure == "permission": raise PermissionError("SECRET permission detail")
            if failure == "changed_source":
                with (tmp_path / "knowledge.db").open("ab") as handle: handle.write(b"changed")
            if failure == "deadline_during_copy":
                monkeypatch.setattr(module.time, "monotonic", lambda: 1e30)
    with pytest.raises(KnowledgePreparationError) as error:
        run(tmp_path, fault_hook=hook)
    assert error.value.result.status == "migration_failed"
    assert "SECRET" not in str(error.value)
    if failure != "changed_source": assert (tmp_path / "knowledge.db").read_bytes() == before
    assert error.value.result.reason == {"disk_full": "disk_full", "permission": "permission_denied", "changed_source": "changed_source", "deadline_during_copy": "deadline_exceeded"}[failure]


@pytest.mark.parametrize("finding", ["ambiguous", "invalid", "unsupported_schema", "unsafe_path", "orphan_sidecar", "recovery_required"])
def test_doctor_definite_blockers_are_nonmutating(tmp_path, finding, request):
    with RuntimeLease(tmp_path / "runtime.lock"): pass
    if finding == "ambiguous":
        changed = list(ROW); changed[1] = "other"
        database(tmp_path / "knowledge.db", [ROW]); database(tmp_path / "knowledge" / "knowledge.db", [tuple(changed)])
    elif finding == "invalid": (tmp_path / "knowledge.db").write_bytes(b"broken")
    elif finding == "unsupported_schema":
        database(tmp_path / "knowledge.db")
        connection = sqlite3.connect(tmp_path / "knowledge.db"); connection.execute("CREATE TABLE alien(x)"); connection.close()
    elif finding == "unsafe_path":
        database(tmp_path / "elsewhere.db"); (tmp_path / "knowledge.db").symlink_to(tmp_path / "elsewhere.db")
    elif finding == "orphan_sidecar": (tmp_path / "knowledge.db-wal").write_bytes(b"evidence")
    else:
        evidence = tmp_path / "knowledge" / "backups" / "incomplete"
        evidence.mkdir(parents=True); (evidence / "candidate.db").write_bytes(b"evidence")
    before = inventory(tmp_path)
    observation = inspect_knowledge(tmp_path)
    assert observation.result.status == finding
    assert inventory(tmp_path) == before
    record_evidence(request, {"id": f"knowledge.doctor_definite_blockers_are_nonmutating.{finding}", "expected": {"status": [finding]}, "observed": result_evidence(observation.result), "lease_status": observation.lease_status, "nonmutation": True, "proofs_passed": True})


def test_pending_doctor_privacy_cleanup_and_stale_metadata(tmp_path, monkeypatch, request):
    import agent_team.memory.preparation as module
    database(tmp_path / "knowledge.db", [ROW])
    def crash(site, context):
        if site == "journal.prepared.after": raise RuntimeError("crash")
    with pytest.raises(RuntimeError): run(tmp_path, fault_hook=crash)
    (tmp_path / "runtime-owner.json").write_text(json.dumps({"pid": 999999, "started_at": "secret-shape", "owner_token": "secret-token"}))
    paths = []
    original_connect = module.sqlite3.connect
    def guarded_connect(path, *args, **kwargs):
        assert not str(path).startswith(str(tmp_path)), "Doctor opened original SQLite"
        if path != ":memory:": paths.append(Path(path))
        return original_connect(path, *args, **kwargs)
    monkeypatch.setattr(module.sqlite3, "connect", guarded_connect)
    before = inventory(tmp_path)
    result = inspect_knowledge(tmp_path)
    assert result.result.status == "recovery_pending"
    assert result.owner == {"pid": 999999}
    assert paths and all(not path.exists() for path in paths)
    assert inventory(tmp_path) == before
    record_evidence(request, {"id": "knowledge.pending_doctor_privacy_cleanup_and_stale_metadata", "expected": {"status": ["recovery_pending"], "phase": ["PREPARED"]}, "observed": result_evidence(result.result), "lease_status": result.lease_status, "nonmutation": True, "source_sqlite_never_opened": True, "private_copies_cleaned": all(not path.exists() for path in paths), "safe_advisory_owner": result.owner, "proofs_passed": True})


def test_archive_permissions_never_broaden_and_retention_excludes_backups(tmp_path, request):
    from agent_team.config import RetentionConfig
    from agent_team.persistence import SessionStore
    from agent_team.retention import DurableRetention
    database(tmp_path / "knowledge.db", [ROW]); os.chmod(tmp_path / "knowledge.db", 0o640)
    with RuntimeLease(tmp_path / "runtime.lock") as lease:
        prepared = prepare_knowledge(tmp_path, lease=lease)
        archive = Path(prepared.result.archive_path)
        before = inventory(archive)
        assert all((mode & ~0o640) == 0 for mode, *_ in before.values())
        result = DurableRetention(tmp_path, RetentionConfig(knowledge_max_age_days=1)).sweep(session_store=SessionStore(tmp_path / "sessions"), knowledge_store=prepared.store)
        assert result.knowledge_records == 1
        assert inventory(archive) == before
        assert raw(archive / "legacy-root" / "snapshot.db")[0][:14] == ROW
        proof = history_evidence(archive / "legacy-root" / "snapshot.db", [ROW])
        assert proof["valid"] and proof["raw_value_type_match"]
        record_evidence(request, {"id": "knowledge.archive_permissions_never_broaden_and_retention_excludes_backups", "expected": {"migration_count": 1, "retention_count": 1}, "observed": {"migration_count": prepared.result.history_count, "retention_count": result.knowledge_records}, "history": proof, "archive_nonmutation": True, "permissions_never_broadened": True, "proofs_passed": True})


@pytest.mark.parametrize("schema_case", ["analyze", "missing_indexes", "constraint_default", "foreign_action", "unique_index", "partial_index", "inline_foreign_key", "parenthesized_default", "generated_column", "descending_primary_key"])
def test_schema_constraints_indexes_and_statistics(tmp_path, schema_case, request):
    source = tmp_path / "knowledge.db"
    if schema_case == "constraint_default":
        source.parent.mkdir(exist_ok=True)
        connection = sqlite3.connect(source); connection.execute(DDL.replace("DEFAULT '[]'", "DEFAULT '{}'")); connection.commit(); connection.close()
    elif schema_case == "foreign_action":
        connection = sqlite3.connect(source); connection.execute(DDL.replace("REFERENCES knowledge_records(id)", "REFERENCES knowledge_records(id) ON DELETE CASCADE")); connection.commit(); connection.close()
    elif schema_case in ("inline_foreign_key", "parenthesized_default", "descending_primary_key"):
        shape = DDL
        if schema_case == "inline_foreign_key":
            shape = shape.replace("supersedes TEXT,", "supersedes TEXT REFERENCES knowledge_records(id),").replace(",\n FOREIGN KEY (supersedes) REFERENCES knowledge_records(id)", "")
        elif schema_case == "parenthesized_default": shape = shape.replace("DEFAULT '[]'", "DEFAULT ('[]')")
        else: shape = shape.replace("id TEXT PRIMARY KEY", "id TEXT PRIMARY KEY DESC")
        connection = sqlite3.connect(source); connection.execute(shape); connection.commit(); connection.close()
    else:
        database(source, [ROW]); connection = sqlite3.connect(source)
        if schema_case == "analyze": connection.execute("ANALYZE")
        elif schema_case == "unique_index": connection.execute("CREATE UNIQUE INDEX idx_topic ON knowledge_records(topic)")
        elif schema_case == "partial_index": connection.execute("CREATE INDEX idx_topic ON knowledge_records(topic) WHERE topic IS NOT NULL")
        elif schema_case == "generated_column": connection.execute("ALTER TABLE knowledge_records ADD COLUMN extra TEXT GENERATED ALWAYS AS (topic) VIRTUAL")
        connection.commit(); connection.close()
    if schema_case in ("analyze", "missing_indexes", "inline_foreign_key", "parenthesized_default"):
        result = run(tmp_path)
        assert result.result.status == "migrated"
        expected_rows = [ROW] if schema_case in ("analyze", "missing_indexes") else []
        proof = history_evidence(result.store.db_path, expected_rows)
        assert proof["valid"] and proof["raw_value_type_match"]
        record_evidence(request, {"id": f"knowledge.schema_constraints_indexes_and_statistics.{schema_case}", "expected": {"status": ["migrated"], "count": len(expected_rows)}, "observed": result_evidence(result.result), "history": proof, "snapshots": archive_evidence(tmp_path, {"legacy-root": expected_rows}), "proofs_passed": True})
    else:
        with pytest.raises(KnowledgePreparationError) as error: run(tmp_path)
        assert error.value.result.status == "unsupported_schema"
        record_evidence(request, {"id": f"knowledge.schema_constraints_indexes_and_statistics.{schema_case}", "expected": {"status": ["unsupported_schema"]}, "observed": result_evidence(error.value.result), "proofs_passed": True})


@pytest.mark.parametrize("unsafe", ["missing", "symlink", "hardlink", "directory"])
def test_existing_lease_guard_refuses_unsafe_lock(tmp_path, unsafe):
    lock = tmp_path / "runtime.lock"
    if unsafe == "symlink":
        (tmp_path / "real.lock").write_bytes(b""); lock.symlink_to(tmp_path / "real.lock")
    elif unsafe == "hardlink":
        lock.write_bytes(b""); os.link(lock, tmp_path / "alias.lock")
    elif unsafe == "directory": lock.mkdir()
    before = inventory(tmp_path)
    with RuntimeLeaseInspection(lock) as guard: assert guard.status == "unavailable"
    assert inventory(tmp_path) == before


def test_fork_cannot_commit_or_release_parent_lease(tmp_path):
    code = """import os,sys
from pathlib import Path
from agent_team.persistence import RuntimeLease,RuntimeAlreadyRunningError
lease=RuntimeLease(Path(sys.argv[1])/'runtime.lock').acquire()
child=os.fork()
if child==0:
    try: lease.check_generation(lease.generation)
    except RuntimeError: pass
    else: os._exit(4)
    lease.release()
    os._exit(0)
_,status=os.waitpid(child,0)
assert status==0
assert lease.is_held
try: RuntimeLease(Path(sys.argv[1])/'runtime.lock').acquire()
except RuntimeAlreadyRunningError: pass
else: raise AssertionError('Child released parent lease')
lease.release()
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=5)


def test_fault_registry_matches_every_actual_site_and_occurrence(tmp_path):
    from collections import Counter
    registry = json.loads(Path(__file__).with_name("knowledge_cases.json").read_text())
    assert registry["schema_version"] == 1
    assert registry["fault_sites"] == list(FAULT_SITES)
    expected = Counter((case["branch"], case["site"]) for case in registry["cases"] if "site" in case)
    actual = Counter()
    for branch in ("fresh", "legacy", "retained", "replacement", "wal", "hot", "retained_wal"):
        root = tmp_path / branch
        fault_fixture(root, branch)
        run(root, fault_hook=lambda site, context: actual.update([(branch, site)]))
    assert actual == expected
    assert sum(actual.values()) == registry["fault_instance_count"]


@pytest.mark.parametrize("site", FAULT_SITES)
def test_real_process_crash_at_every_fault_site(tmp_path, site, request):
    branch = "retained" if site.startswith("publication.retained") else "replacement"
    fault_fixture(tmp_path, branch)
    original = physical_evidence(tmp_path)
    code = """import os,sys
from pathlib import Path
from agent_team.persistence import RuntimeLease
from agent_team.memory.preparation import prepare_knowledge
root=Path(sys.argv[1]); wanted=sys.argv[2]
print(os.getpid(),flush=True)
lease=RuntimeLease(root/'runtime.lock').acquire()
def crash(site,context):
    if site==wanted: os._exit(77)
prepare_knowledge(root,lease=lease,fault_hook=crash)
raise AssertionError('Unreached fault site')
"""
    from agent_team.subprocess_env import sanitized_subprocess_env
    with tempfile.TemporaryDirectory(prefix="knowledge-crash-controller-") as private_temp:
        environment = sanitized_subprocess_env(overrides={"TMPDIR": private_temp})
        process = subprocess.run([sys.executable, "-c", code, str(tmp_path), site], env=environment, timeout=5, capture_output=True, text=True)
    assert process.returncode == 77
    pending = tmp_path / "knowledge" / "migration-pending.json"
    was_pending = pending.exists()
    before = inventory(tmp_path)
    interrupted = physical_evidence(tmp_path)
    observation = inspect_knowledge(tmp_path)
    assert inventory(tmp_path) == before
    assert observation.lease_status == "free"
    if was_pending:
        assert observation.result.status == "recovery_pending"
    try:
        prepared = run(tmp_path)
    except KnowledgePreparationError as error:
        assert not was_pending and error.result.status == "recovery_required"
        assert error.result.reason == "evidence_unverified"
        disposition = result_evidence(error.result)
        history = history_evidence(tmp_path / "knowledge.db", [ROW])
        statuses, reason = ["recovery_required"], "evidence_unverified"
    else:
        assert raw(prepared.store.db_path)[0][:14] == ROW
        disposition = result_evidence(prepared.result)
        history = history_evidence(prepared.store.db_path, [ROW])
        statuses, reason = ["reconciled", "canonical"], None
        assert prepared.result.status in statuses
    assert history["valid"] and history["raw_value_type_match"]
    snapshots = archive_evidence(tmp_path, fault_source_oracles(branch))
    if was_pending:
        assert all(proof["valid"] and proof["self_contained"] and proof["raw_value_type_match"] for proof in snapshots.values())
    phases = ["COMPLETED"] if was_pending else [None, "COMPLETED"]
    assert disposition["phase"] in phases
    record_evidence(request, {
        "id": f"knowledge.real_process_crash_at_every_fault_site.{site}", "site": site, "branch": branch, "occurrence": 0,
        "expected": {"status": statuses, "reason": reason, "phase": phases, "count": 1, "process_exit": 77, "pending_requires_roll_forward": was_pending},
        "observed": disposition, "authority": "canonical" if disposition["status"] != "recovery_required" else None,
        "before": original, "interrupted": interrupted, "after": physical_evidence(tmp_path), "history": history, "snapshots": snapshots,
        "doctor": {"status": observation.result.status, "lease_status": observation.lease_status, "nonmutation": True},
        "process": {"pid": int(process.stdout.strip()), "exit": process.returncode, "reaped": True},
        "private_proofs_cleaned": history["private_copy_cleaned"] and not Path(private_temp).exists(), "proofs_passed": True,
    })


def test_retention_sqlite_retry_cannot_extend_outer_deadline(tmp_path):
    from datetime import datetime, timezone
    with RuntimeLease(tmp_path / "runtime.lock") as lease:
        store = prepare_knowledge(tmp_path, lease=lease).store
        code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); sys.stdin.read()"
        process = subprocess.Popen([sys.executable, "-c", code, str(store.db_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline().strip() == "locked"
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                store.prune_before(datetime.now(timezone.utc), deadline=started + 0.03)
            assert time.monotonic() - started < 0.3
        finally:
            process.communicate(""); process.wait(timeout=5)
