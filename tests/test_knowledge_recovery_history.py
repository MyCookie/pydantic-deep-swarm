"""Prove interrupted fresh publication from concrete private SQL history."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from agent_team.memory import preparation as preparation_module
from agent_team.memory.preparation import KnowledgePreparationError, inspect_knowledge
from agent_team.subprocess_env import sanitized_subprocess_env
from test_knowledge_preparation import history_evidence, physical_evidence, raw, run


EMPTY_HISTORY_DIGEST = hashlib.sha256(b"").hexdigest()
CRASH_PROGRAM = """import os,sys
from pathlib import Path
from agent_team.persistence import RuntimeLease
from agent_team.memory.preparation import prepare_knowledge
root=Path(sys.argv[1]); wanted=sys.argv[2]
print(os.getpid(),flush=True)
lease=RuntimeLease(root/'runtime.lock').acquire()
def crash(site,context):
    if site==wanted: os._exit(77)
prepare_knowledge(root,lease=lease,fault_hook=crash)
raise AssertionError('Unreached fresh publication fault site')
"""


def case_id(request):
    return "knowledge." + request.node.name.removeprefix("test_").replace("[", ".").replace("]", "")


@pytest.fixture(autouse=True)
def recovery_case_manifest(request, tmp_path):
    before = physical_evidence(tmp_path)
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise AssertionError("knowledge recovery history case exceeded its 20-second deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 20)
    try:
        yield
        assert time.monotonic() - started < 20
        request.node.user_properties.append(("knowledge_case_manifest", {
            "schema_version": 1, "id": case_id(request),
            "case_parameters": getattr(getattr(request.node, "callspec", None), "params", {}),
            "before": before, "after": physical_evidence(tmp_path),
            "evidence_scope": "real_fresh_publication_crash_and_private_history_verification",
            "deadline_seconds": 20, "elapsed_seconds": time.monotonic() - started,
            "cleanup": "Actual crashed child reaped; SQLite connections closed; private verification directories removed; runtime lease released.",
        }))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def crash_fresh(root, site):
    with tempfile.TemporaryDirectory(prefix="fresh-history-crash-") as directory:
        environment = sanitized_subprocess_env(overrides={"TMPDIR": directory})
        completed = subprocess.run([sys.executable, "-c", CRASH_PROGRAM, str(root), site],
                                   env=environment, timeout=5, capture_output=True, text=True)
    assert completed.returncode == 77, completed.stderr
    pid = int(completed.stdout.strip())
    assert pid > 0 and not Path(directory).exists()
    pending = root / "knowledge" / "migration-pending.json"
    journal = json.loads(pending.read_text())
    assert journal["selected"] is None
    assert journal["sources"] == journal["snapshots"] == {}
    assert journal["history"] == {"schema": 14, "count": 0, "digest": EMPTY_HISTORY_DIGEST}
    candidate = root / journal["candidate"]
    authority = candidate if candidate.exists() else root / "knowledge" / "knowledge.db"
    assert authority.exists()
    assert (candidate.exists(), journal["publication_intent"]) == (
        (True, False) if site == "journal.prepared.after" else (False, True))
    return pending, journal, authority, {"pid": pid, "exit": completed.returncode, "reaped": True}


def guarded_sqlite(monkeypatch, root):
    paths = []
    original = preparation_module.sqlite3.connect

    def connect(path, *args, **kwargs):
        if path != ":memory:":
            concrete = Path(path).resolve()
            assert not concrete.is_relative_to(root.resolve()), "Verifier opened durable SQLite evidence"
            paths.append(concrete)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(preparation_module.sqlite3, "connect", connect)
    return paths


def durable_evidence(root):
    manifest = physical_evidence(root)["manifest"]
    # Owning serve may replace owner metadata; the existing lease file remains
    # the same inode. All database, journal, archive, and sidecar evidence stays.
    return {name: entry for name, entry in manifest.items()
            if name not in ("runtime.lock", "runtime-owner.json")}


def assert_unknown(result):
    assert result.verification == "unknown"
    assert result.history_count is None and result.history_digest is None
    assert result.recovered is False and result.upgraded is False


def record_proof(request, process, before, after, **observations):
    request.node.user_properties.append(("knowledge_evidence", {
        "schema_version": 1, "id": case_id(request), "process": process,
        "before": before, "after": after, "observations": observations,
        "proofs_passed": True,
    }))


@pytest.mark.parametrize("site", ["journal.prepared.after", "publication.rename.after"], ids=["staged", "published"])
@pytest.mark.parametrize("integrity", ["untouched", "digest-corrupt"], ids=["untouched", "digest-corrupt"])
def test_fresh_pending_history_requires_concrete_sql_proof(tmp_path, monkeypatch, site, integrity, request):
    pending, journal, authority, process = crash_fresh(tmp_path, site)
    if integrity == "digest-corrupt":
        changed = json.loads(pending.read_text())
        changed["history"]["digest"] = "0" * 64
        pending.write_text(json.dumps(changed))
        assert {**changed, "history": journal["history"]} == journal
    before = physical_evidence(tmp_path)
    with monkeypatch.context() as patch:
        paths = guarded_sqlite(patch, tmp_path)
        observation = inspect_knowledge(tmp_path)
    assert observation.lease_status == "free"
    assert physical_evidence(tmp_path) == before
    assert paths and all(not path.exists() for path in paths)
    expected_status = "recovery_required" if integrity == "digest-corrupt" else "recovery_pending"
    assert observation.result.status == expected_status
    if integrity == "digest-corrupt":
        assert observation.result.reason == "candidate_history_unverified"
        assert_unknown(observation.result)
        preserved = durable_evidence(tmp_path)
        with monkeypatch.context() as patch:
            recovery_paths = guarded_sqlite(patch, tmp_path)
            with pytest.raises(KnowledgePreparationError) as failure:
                run(tmp_path)
        assert failure.value.result.status == "recovery_required"
        assert failure.value.result.reason == "candidate_history_unverified"
        assert_unknown(failure.value.result)
        assert durable_evidence(tmp_path) == preserved
        assert recovery_paths and all(not path.exists() for path in recovery_paths)
        assert pending.exists()
    else:
        assert observation.result.verification == "passed"
        assert observation.result.history_count == 0
        assert observation.result.history_digest == EMPTY_HISTORY_DIGEST
        with monkeypatch.context() as patch:
            recovery_paths = guarded_sqlite(patch, tmp_path)
            prepared = run(tmp_path)
        assert recovery_paths and all(not path.exists() for path in recovery_paths)
        assert prepared.result.status == "created" and prepared.result.recovered
        assert prepared.result.phase == "COMPLETED" and prepared.result.verification == "passed"
        assert prepared.result.history_count == 0 and prepared.result.history_digest == EMPTY_HISTORY_DIGEST
        assert not pending.exists()
        canonical = tmp_path / "knowledge" / "knowledge.db"
        assert raw(canonical) == []
        proof = history_evidence(canonical, [])
        assert proof["valid"] and proof["raw_value_type_match"] and proof["count"] == 0
        assert proof["schema_columns"] == 14 and proof["private_copy_cleaned"]
        archive = Path(prepared.result.archive_path)
        assert json.loads((archive / "manifest.json").read_text())["history"] == journal["history"]
    record_proof(request, process, before, physical_evidence(tmp_path),
                 doctor_status=expected_status, doctor_nonmutation=True,
                 concrete_authority="candidate" if site == "journal.prepared.after" else "canonical",
                 digest_only_tamper=integrity == "digest-corrupt", private_sql_only=True,
                 private_copies_cleaned=True, retained_or_completed_journal=True)


@pytest.mark.parametrize("companion", ["-wal", "-shm", "-journal"], ids=["wal", "shm", "journal"])
def test_pending_candidate_companions_refuse_unproven_history(tmp_path, companion, request):
    pending, journal, authority, process = crash_fresh(tmp_path, "journal.prepared.after")
    Path(str(authority) + companion).write_bytes(b"unverified companion")
    before = physical_evidence(tmp_path)
    observation = inspect_knowledge(tmp_path)
    assert observation.result.status == "recovery_required"
    assert observation.result.reason == "candidate_unverified"
    assert_unknown(observation.result)
    assert physical_evidence(tmp_path) == before
    preserved = durable_evidence(tmp_path)
    with pytest.raises(KnowledgePreparationError) as failure:
        run(tmp_path)
    assert failure.value.result.status == "recovery_required"
    assert failure.value.result.reason == "candidate_unverified"
    assert_unknown(failure.value.result)
    assert durable_evidence(tmp_path) == preserved
    assert pending.exists() and Path(str(authority) + companion).exists()
    record_proof(request, process, before, physical_evidence(tmp_path),
                 doctor_nonmutation=True, companion=companion, evidence_preserved=True,
                 unverified_history_not_reported=True)


@pytest.mark.parametrize("lane", ["doctor", "runtime"])
@pytest.mark.parametrize("failure_mode", ["permission", "deadline"])
def test_pending_private_history_verification_failure_preserves_evidence(tmp_path, monkeypatch, lane, failure_mode, request):
    pending, journal, authority, process = crash_fresh(tmp_path, "journal.prepared.after")
    original_copy = preparation_module._copy_file
    copies = []

    def fail_private_copy(source, destination, operation):
        assert source == authority
        assert not destination.resolve().is_relative_to(tmp_path.resolve())
        copies.append(destination)
        if failure_mode == "permission":
            raise PermissionError(errno.EACCES, "SECRET private copy path")
        operation.budget.deadline = time.monotonic() - 1
        return original_copy(source, destination, operation)

    before = physical_evidence(tmp_path)
    preserved = durable_evidence(tmp_path)
    monkeypatch.setattr(preparation_module, "_copy_file", fail_private_copy)
    if lane == "doctor":
        result = inspect_knowledge(tmp_path).result
        assert result.status == "inspection_unavailable"
        assert physical_evidence(tmp_path) == before
    else:
        with pytest.raises(KnowledgePreparationError) as failure:
            run(tmp_path)
        result = failure.value.result
        assert result.status == "migration_failed"
    assert result.reason == ("permission_denied" if failure_mode == "permission" else "deadline_exceeded")
    assert_unknown(result)
    assert "SECRET" not in json.dumps(result.as_dict())
    assert copies and all(not copy.parent.exists() for copy in copies)
    assert durable_evidence(tmp_path) == preserved and pending.exists()
    record_proof(request, process, before, physical_evidence(tmp_path), lane=lane,
                 operational_reason=result.reason, private_copy_attempted=True,
                 private_copies_cleaned=True, evidence_preserved=True,
                 unverified_history_not_reported=True)
