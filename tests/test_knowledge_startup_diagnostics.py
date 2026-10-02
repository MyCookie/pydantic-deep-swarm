"""Parse actual foreground knowledge evidence independently of record APIs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sqlite3

import pytest
import yaml

from test_foreground_contract import (
    OWNER, configuration, endpoint, evidence, finite_case_deadline, manifest, owner,
)
from test_foreground_phase_deadlines import DDL, PHASE_OWNER, private_history


PRIVATE_RECORD_MARKER = "record-private-marker-never-in-diagnostics"
ROW = ("history-id", "fixture-topic", "fixture-summary", PRIVATE_RECORD_MARKER,
       "worker", "project", '[ "tag" ]', 0.75, "2020-01-01T00:00:00",
       "2020-01-01T00:00:00", None, "original-run", "original-artifact", "[]")


def seed(path, rows=(ROW,), *, old=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = DDL.replace(" source_artifacts TEXT NOT NULL DEFAULT '[]',", "") if old else DDL
    with sqlite3.connect(path) as connection:
        connection.execute(ddl)
        for row in rows:
            values = row[:-1] if old else row
            connection.execute("INSERT INTO knowledge_records VALUES (" + ",".join("?" for _ in values) + ")", values)


def normalized_history(path):
    result = []
    for row in private_history(path):
        result.append(tuple(row[:13]) + ("[]",) + tuple(row[13:]) + ("text",) if len(row) == 26 else tuple(row))
    return result


def preparation(process):
    events = [event for event in process.events if event["event"] == "knowledge_preparation"]
    assert len(events) == 1, "each actual preparation emits exactly one structured result"
    result = events[0]
    assert result["stream"] == "stderr"
    required = {"state_dir", "status", "reason", "cause", "selected_source", "verification", "candidate_paths",
                "phase", "migration_id", "archive_path", "journal_path", "warnings", "history_count",
                "history_digest", "recovered", "upgraded", "remediation"}
    assert required <= result.keys()
    assert isinstance(result["recovered"], bool) and isinstance(result["upgraded"], bool)
    assert isinstance(result["remediation"], str) and result["remediation"]
    assert PRIVATE_RECORD_MARKER not in "\n".join(line for _, line in process.lines)
    return result


def clean_payload(event):
    return {key: value for key, value in event.items() if key not in {"event", "stream", "observed_at"}}


@pytest.mark.parametrize("branch,status,source,upgraded", [
    ("fresh", "created", None, False),
    ("canonical", "canonical", "canonical", False),
    ("legacy", "migrated", "legacy", False),
    ("identical", "reconciled", "canonical", False),
    ("canonical-old", "canonical", "canonical", True),
    ("disabled", "disabled", None, False),
])
def test_complete_preparation_result_is_public_before_ready(tmp_path, request, branch, status, source, upgraded):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=branch != "disabled")
        legacy = state / "knowledge.db"
        canonical = state / "knowledge" / "knowledge.db"
        expected_history = []
        if branch in {"legacy", "identical"}:
            seed(legacy)
            expected_history = normalized_history(legacy)
        if branch in {"canonical", "identical", "canonical-old"}:
            seed(canonical, old=branch == "canonical-old")
            expected_history = normalized_history(canonical)
        if branch == "disabled":
            state.mkdir()
            legacy.write_bytes(b"disabled store is deliberately not SQLite")
            disabled_before = manifest(state)
        with owner(config, tmp_path / "home") as process:
            ready = process.wait("serve_ready")
            process.send(signal.SIGTERM)
            proof = process.finish(143)
            result = preparation(process)
            assert (result["status"], result["selected_source"], result["upgraded"]) == (status, source, upgraded)
            assert result["state_dir"] == str(state)
            assert result["candidate_paths"] == {"legacy": str(legacy), "canonical": str(canonical)}
            assert result["reason"] is None and result["cause"] is None
            assert result["recovered"] is False
            assert [event["event"] for event in process.events if event["stream"] == "stdout"] == ["serve_ready"]
            assert ready["state_dir"] == str(state)
            if branch == "disabled":
                assert result["verification"] == "not_selected"
                assert result["history_count"] is None and result["history_digest"] is None
                assert result["archive_path"] is None and result["journal_path"] is None
                assert manifest(state)["knowledge.db"] == disabled_before["knowledge.db"]
                assert not canonical.exists()
            else:
                assert result["verification"] == "passed"
                assert result["history_count"] == len(expected_history)
                assert isinstance(result["history_digest"], str) and len(result["history_digest"]) == 64
                assert normalized_history(canonical) == expected_history
                if branch != "canonical":
                    assert result["phase"] == "COMPLETED"
                    assert Path(result["archive_path"]).is_dir()
                assert not legacy.exists()
        evidence(request, f"lifecycle.knowledge-observation.{branch}", [proof],
                 preparation_result=clean_payload(result), records_absent_from_output=True,
                 complete_history_preserved=True, disabled_source_untouched=branch == "disabled")


def test_divergent_failure_retains_full_safe_preparation_payload(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        seed(state / "knowledge.db")
        changed = list(ROW)
        changed[3] = "different " + PRIVATE_RECORD_MARKER
        seed(state / "knowledge" / "knowledge.db", [tuple(changed)])
        before = manifest(state)
        with owner(config, tmp_path / "home") as process:
            failed = process.wait("serve_failed")
            proof = process.finish(1)
            result = preparation(process)
            assert (result["status"], result["reason"], result["verification"]) == (
                "ambiguous", "divergent_histories", "unknown")
            assert result["state_dir"] == str(state)
            assert result["selected_source"] is None
            assert failed["reason"] == "runtime_initialization_failed" and failed["phase"] == "knowledge"
            assert failed["knowledge_reason"] == "divergent_histories"
            assert failed["knowledge"] == clean_payload(result)
            assert not any(event["event"] == "serve_ready" for event in process.events)
            assert manifest(state) == before
        evidence(request, "lifecycle.knowledge-observation.divergent", [proof],
                 preparation_result=clean_payload(result), nested_failure_payload_preserved=True,
                 original_bundles_preserved=True, records_absent_from_output=True)


def test_resumed_migration_is_reported_once_with_preserved_history(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        seed(state / "knowledge.db")
        history = normalized_history(state / "knowledge.db")
        with owner(config, tmp_path / "home", mode="migration_fault", program=PHASE_OWNER) as process:
            failed = process.wait("serve_failed")
            first = process.finish(1)
            initial = preparation(process)
            assert initial["status"] == "migration_failed" and initial["reason"] == "disk_full"
            assert initial["phase"] == "RETIRING"
            assert Path(initial["journal_path"]).is_file()
            assert Path(initial["archive_path"]).is_dir()
            assert initial["selected_source"] == "legacy"
            assert initial["history_count"] == 1 and len(initial["history_digest"]) == 64
            assert initial["verification"] == "unknown"
            assert initial["recovered"] is False and initial["upgraded"] is False
            assert failed["knowledge"] == clean_payload(initial)
        with owner(config, tmp_path / "successor-home") as resumed:
            resumed.wait("serve_ready")
            resumed.send(signal.SIGTERM)
            second = resumed.finish(143)
            result = preparation(resumed)
            assert result["status"] == "migrated" and result["recovered"] is True
            assert result["verification"] == "passed" and result["phase"] == "COMPLETED"
            assert result["migration_id"] == initial["migration_id"]
            assert result["history_count"] == 1 and result["selected_source"] == "legacy"
            assert normalized_history(state / "knowledge" / "knowledge.db") == history
        evidence(request, "lifecycle.knowledge-observation.resumed", [first, second],
                 failed_preparation=clean_payload(initial), resumed_preparation=clean_payload(result),
                 pending_journal_retained=True, all_raw_history_preserved=True, lease_reused=True)


def test_cleanup_failure_preserves_primary_knowledge_evidence(tmp_path, request):
    program = r'''
import os
import agent_team.app as boundary
os.environ["FIXTURE_MODE"] = "migration_fault"
def failed_release(self):
    raise OSError("fixture-sensitive-raw-error")
boundary.RuntimeLease.release = failed_release
''' + PHASE_OWNER
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        seed(state / "knowledge.db")
        with owner(config, tmp_path / "home", program=program) as process:
            failed = process.wait("serve_failed")
            proof = process.finish(1)
            result = preparation(process)
            assert (failed["reason"], failed["phase"], failed["knowledge_reason"]) == (
                "shutdown_failed", "shutdown", "disk_full")
            assert failed["knowledge"] == clean_payload(result)
            assert result["phase"] == "RETIRING" and result["verification"] == "unknown"
            assert Path(result["journal_path"]).is_file()
        evidence(request, "lifecycle.knowledge-observation.cleanup-failure", [proof],
                 preparation_result=clean_payload(result), knowledge_payload_survives_cleanup_failure=True,
                 ownership_released_by_os_teardown=True)


def test_migration_history_evidence_precedes_separate_retention(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        now = datetime.now(timezone.utc).isoformat()
        recent = list(ROW)
        recent[0], recent[8], recent[9] = "recent-id", now, now
        seed(state / "knowledge.db", [ROW, tuple(recent)])
        document = yaml.safe_load(config.read_text())
        document["retention"] = {"knowledge_max_age_days": 1}
        config.write_text(yaml.safe_dump(document))
        with owner(config, tmp_path / "home") as process:
            process.wait("serve_ready")
            process.send(signal.SIGTERM)
            proof = process.finish(143)
            result = preparation(process)
            assert result["status"] == "migrated" and result["history_count"] == 2
            remaining = normalized_history(state / "knowledge" / "knowledge.db")
            assert len(remaining) == 1 and remaining[0][0] == "recent-id"
            snapshot = Path(result["archive_path"]) / "legacy-root" / "snapshot.db"
            assert len(normalized_history(snapshot)) == 2
            retention = []
            prep_index = retention_index = None
            for index, (stream, line) in enumerate(process.lines):
                if stream == "stderr" and line.startswith("{") and json.loads(line).get("event") == "knowledge_preparation":
                    prep_index = index
                if stream == "stderr" and line.startswith("INFO: "):
                    payload = json.loads(line[len("INFO: "):])
                    if payload.get("event_type") == "retention_sweep":
                        retention.append(payload)
                        retention_index = index
            assert len(retention) == 1 and retention[0]["knowledge_records"] == 1
            assert prep_index < retention_index
        evidence(request, "lifecycle.knowledge-observation.before-retention", [proof],
                 preparation_result=clean_payload(result), pre_retention_count=2, post_retention_count=1,
                 archived_history_count=2, retention_removal_count=1, preparation_reported_before_retention=True)


def test_preparation_paths_and_records_are_redacted(tmp_path, request):
    secret = "fixture-knowledge-path-secret"
    selected = tmp_path / secret
    selected.mkdir()
    with endpoint() as (_, url):
        config, state = configuration(selected, url, knowledge=True)
        seed(state / "knowledge.db")
        original = normalized_history(state / "knowledge.db")
        with owner(config, tmp_path / "home", env_additions={"KNOWLEDGE_PATH_SECRET": secret}) as process:
            process.wait("serve_ready")
            process.send(signal.SIGTERM)
            proof = process.finish(143)
            result = preparation(process)
            output = "\n".join(line for _, line in process.lines)
            assert secret not in output and PRIVATE_RECORD_MARKER not in output
            assert "[redacted]" in result["state_dir"]
            assert "[redacted]" in result["archive_path"]
            assert normalized_history(state / "knowledge" / "knowledge.db") == original
        # Preserve the complete physical manifest with the same safe labels used
        # by runtime diagnostics for the deliberately secret-bearing directory.
        safe_proof = {**proof, "events": json.loads(json.dumps(proof["events"]).replace(secret, "[redacted]"))}
        before = {path.replace(secret, "[redacted]"): metadata for path, metadata in request.node._lifecycle_before.items()}
        after = {path.replace(secret, "[redacted]"): metadata for path, metadata in manifest(tmp_path).items()}
        evidence(request, "lifecycle.knowledge-observation.redaction", [safe_proof], before=before, after=after,
                 preparation_result=clean_payload(result), secret_paths_redacted=True,
                 records_absent_from_output=True, original_raw_history_preserved=True)
