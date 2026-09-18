"""Tests for Git provenance capture and policy enforcement."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_team.provenance import (
    GeneratedBy,
    ProvenanceValidationError,
    ValidationResult,
    build_provenance_record,
    load_provenance,
    validate_provenance,
    write_provenance,
)


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


def test_capture_records_unborn_base_worktree_candidate_and_push_policy(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "asset.txt").write_text("generated", encoding="utf-8")

    record = build_provenance_record(
        tmp_path,
        record_id="run-001",
        project_id="GITOPS-5",
        generated_by=GeneratedBy(
            agent="Hermes",
            model="test-model",
            session_id="session-1",
            task_id="GITOPS-5",
        ),
        validation_results=[
            ValidationResult(name="unit", status="passed", command=["pytest", "-q"])
        ],
    )

    assert record.base_revision is None
    assert record.candidate_revision.startswith("worktree:")
    assert len(record.candidate_revision.removeprefix("worktree:")) == 64
    assert record.deployed_revision is None
    assert record.generated_by.task_id == "GITOPS-5"
    assert record.validation_results[0].status == "passed"
    assert record.commit_policy.auto_commit is False
    assert record.push_policy.remote_push_enabled is False
    assert record.push_policy.mode == "disabled-unless-explicit-authorization"

    output = tmp_path / "provenance" / "run-001.json"
    write_provenance(output, record)
    loaded = load_provenance(output)
    assert loaded.model_dump() == record.model_dump()
    assert json.loads(output.read_text(encoding="utf-8"))["candidate_revision"] == record.candidate_revision


def test_capture_uses_committed_head_as_base_and_candidate_tracks_changes(tmp_path: Path):
    init_repo(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("v1", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "asset.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "initial",
        ],
        check=True,
    )
    first = build_provenance_record(
        tmp_path,
        record_id="run-002",
        generated_by={"agent": "swarm-worker", "tool": "asset-generator"},
        deployed_revision="b" * 40,
    )
    index_before = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--raw"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    asset.write_text("v2", encoding="utf-8")
    second = build_provenance_record(
        tmp_path,
        record_id="run-003",
        generated_by=GeneratedBy(agent="swarm-worker"),
        base_revision=first.base_revision,
        candidate_revision=None,
    )
    index_after = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--raw"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert first.base_revision == first.repository.head_revision
    assert first.base_revision is not None
    assert first.candidate_revision == first.base_revision
    assert first.candidate_revision != second.candidate_revision
    assert first.deployed_revision == "b" * 40
    assert first.repository.dirty is False
    assert second.repository.dirty is True
    assert index_before == index_after == ""


def test_asset_provenance_hashes_current_repository_file_and_rejects_mismatch(tmp_path: Path):
    init_repo(tmp_path)
    asset = tmp_path / "generated.txt"
    asset.write_text("generated", encoding="utf-8")

    record = build_provenance_record(
        tmp_path,
        record_id="run-asset",
        generated_by=GeneratedBy(agent="swarm-worker"),
        assets=[{"path": "generated.txt"}],
    )

    assert record.assets[0].sha256 == __import__("hashlib").sha256(
        b"generated"
    ).hexdigest()
    assert record.assets[0].generator == "swarm-worker"
    assert record.assets[0].source == "repository worktree"

    with pytest.raises(ProvenanceValidationError, match="SHA-256 mismatch"):
        build_provenance_record(
            tmp_path,
            record_id="run-asset-bad",
            generated_by=GeneratedBy(agent="swarm-worker"),
            assets=[{"path": "generated.txt", "sha256": "0" * 64}],
        )


def test_provenance_rejects_unsafe_asset_path(tmp_path: Path):
    init_repo(tmp_path)
    with pytest.raises(ProvenanceValidationError, match="asset path"):
        build_provenance_record(
            tmp_path,
            record_id="run-unsafe",
            generated_by=GeneratedBy(agent="swarm-worker"),
            assets=[{"path": "../outside.txt"}],
        )


def test_validate_provenance_checks_worktree_candidate_when_requested(tmp_path: Path):
    init_repo(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("v1", encoding="utf-8")
    record = build_provenance_record(
        tmp_path,
        record_id="run-validate",
        generated_by=GeneratedBy(agent="swarm-worker"),
    )
    output = tmp_path.parent / "run-validate.json"
    write_provenance(output, record)

    loaded = validate_provenance(output, repository=tmp_path, check_candidate=True)
    assert loaded.record_id == "run-validate"

    asset.write_text("v2", encoding="utf-8")
    with pytest.raises(ProvenanceValidationError, match="candidate worktree revision"):
        validate_provenance(output, repository=tmp_path, check_candidate=True)


def test_provenance_rejects_symbolic_revision_names(tmp_path: Path):
    init_repo(tmp_path)
    with pytest.raises(ProvenanceValidationError, match="revision"):
        build_provenance_record(
            tmp_path,
            record_id="run-symbolic",
            generated_by=GeneratedBy(agent="Hermes"),
            candidate_revision="HEAD",
        )


def test_provenance_rejects_worktree_as_deployed_revision(tmp_path: Path):
    init_repo(tmp_path)
    with pytest.raises(ProvenanceValidationError, match="deployed revision"):
        build_provenance_record(
            tmp_path,
            record_id="worktree-deployment",
            generated_by=GeneratedBy(agent="Hermes"),
            deployed_revision="worktree:" + ("a" * 64),
        )


def test_provenance_rejects_remote_push_enabled():
    with pytest.raises(ProvenanceValidationError, match="remote push"):
        build_provenance_record(
            Path("/tmp/not-used"),
            record_id="bad",
            generated_by=GeneratedBy(agent="Hermes"),
            push_remote_enabled=True,
        )


def test_validate_provenance_rejects_passed_claim_without_reproducible_evidence(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "asset.txt").write_text("candidate", encoding="utf-8")
    record = build_provenance_record(
        tmp_path,
        record_id="missing-evidence",
        generated_by=GeneratedBy(agent="Hermes"),
        validation_results=[ValidationResult(name="full", status="passed")],
    )
    output = tmp_path.parent / "missing-evidence.json"
    write_provenance(output, record)

    with pytest.raises(ProvenanceValidationError, match="validation evidence"):
        validate_provenance(output, repository=tmp_path)


def test_validate_provenance_rejects_recorded_asset_digest_drift(tmp_path: Path):
    init_repo(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("candidate", encoding="utf-8")
    record = build_provenance_record(
        tmp_path,
        record_id="asset-drift",
        generated_by=GeneratedBy(agent="Hermes"),
        assets=[{"path": "asset.txt"}],
    )
    output = tmp_path.parent / "asset-drift.json"
    write_provenance(output, record)
    asset.write_text("changed", encoding="utf-8")

    with pytest.raises(ProvenanceValidationError, match="asset digest"):
        validate_provenance(output, repository=tmp_path)


def test_validate_provenance_accepts_committed_candidate_with_complete_evidence(tmp_path: Path):
    init_repo(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("candidate", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "asset.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "candidate",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record = build_provenance_record(
        tmp_path,
        record_id="committed-evidence",
        generated_by=GeneratedBy(agent="Hermes"),
        candidate_revision=revision,
        validation_results=[
            ValidationResult(
                name="full",
                status="passed",
                command=["python", "-m", "pytest", "tests", "-q"],
                details="214 passed, 2 skipped",
                started_at="2026-09-18T00:00:00+00:00",
                finished_at="2026-09-18T00:01:00+00:00",
            )
        ],
        assets=[{"path": "asset.txt"}],
    )
    output = tmp_path.parent / "committed-evidence.json"
    write_provenance(output, record)

    loaded = validate_provenance(output, repository=tmp_path, check_candidate=True)

    assert loaded.candidate_revision == revision
    assert loaded.assets[0].sha256 == record.assets[0].sha256


def test_committed_candidate_assets_are_validated_from_commit_not_dirty_worktree(
    tmp_path: Path,
):
    init_repo(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("committed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "asset.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "--quiet",
            "-m", "candidate",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record = build_provenance_record(
        tmp_path,
        record_id="committed-asset-source",
        generated_by=GeneratedBy(agent="Hermes"),
        candidate_revision=revision,
        assets=[{"path": "asset.txt"}],
    )
    output = tmp_path.parent / "committed-asset-source.json"
    write_provenance(output, record)
    asset.write_text("dirty-worktree", encoding="utf-8")

    loaded = validate_provenance(output, repository=tmp_path, check_candidate=True)

    assert loaded.candidate_revision == revision
