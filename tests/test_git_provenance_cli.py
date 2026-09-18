"""CLI coverage for provenance records."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from agent_team.cli import cli


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


def test_provenance_capture_cli_writes_record_and_reports_policy(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "generated.txt").write_text("generated", encoding="utf-8")
    output = repo / "provenance" / "run.json"

    result = CliRunner().invoke(
        cli,
        [
            "provenance",
            "capture",
            "--repo-root",
            str(repo),
            "--output",
            str(output),
            "--record-id",
            "run-cli",
            "--project-id",
            "GITOPS-5",
            "--agent",
            "Hermes",
            "--model",
            "test-model",
            "--session-id",
            "session-1",
            "--task-id",
            "GITOPS-5",
            "--validation",
            "unit=passed",
            "--validation",
            "full=skipped",
            "--asset",
            "generated.txt",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["record_id"] == "run-cli"
    assert data["project_id"] == "GITOPS-5"
    assert data["generated_by"]["agent"] == "Hermes"
    assert [item["status"] for item in data["validation_results"]] == ["passed", "skipped"]
    assert data["assets"][0]["path"] == "generated.txt"
    assert len(data["assets"][0]["sha256"]) == 64
    assert data["push_policy"]["remote_push_enabled"] is False
    assert "worktree:" in result.output


def test_provenance_validate_cli_rejects_unknown_validation_status(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    output = repo / "provenance" / "bad.json"
    output.parent.mkdir()
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_id": "bad",
                "created_at": "2026-01-01T00:00:00+00:00",
                "repository": {
                    "root": str(repo),
                    "branch": "master",
                    "remote": None,
                    "head_revision": None,
                    "worktree_revision": "worktree:" + "0" * 64,
                    "dirty": False,
                },
                "base_revision": None,
                "candidate_revision": "worktree:" + "0" * 64,
                "deployed_revision": None,
                "generated_by": {"agent": "Hermes"},
                "assets": [],
                "validation_results": [{"name": "unit", "status": "unknown"}],
                "commit_policy": {"mode": "operator-authorized-only", "auto_commit": False},
                "push_policy": {
                    "mode": "disabled-unless-explicit-authorization",
                    "remote_push_enabled": False,
                    "auto_push": False,
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["provenance", "validate", str(output)])

    assert result.exit_code != 0
    assert "invalid provenance file" in result.output


def test_provenance_capture_cli_accepts_complete_validation_evidence_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "generated.txt").write_text("generated", encoding="utf-8")
    evidence = tmp_path / "validation.json"
    evidence.write_text(
        json.dumps(
            [
                {
                    "name": "full",
                    "status": "passed",
                    "command": ["python", "-m", "pytest", "tests", "-q"],
                    "details": "214 passed, 2 skipped",
                    "started_at": "2026-09-18T00:00:00+00:00",
                    "finished_at": "2026-09-18T00:01:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "release-provenance.json"

    captured = CliRunner().invoke(
        cli,
        [
            "provenance",
            "capture",
            "--repo-root",
            str(repo),
            "--output",
            str(output),
            "--record-id",
            "release-evidence",
            "--agent",
            "Hermes",
            "--validation-evidence",
            str(evidence),
            "--asset",
            "generated.txt",
        ],
    )

    assert captured.exit_code == 0, captured.output
    validated = CliRunner().invoke(
        cli,
        ["provenance", "validate", str(output), "--repo-root", str(repo)],
    )
    assert validated.exit_code == 0, validated.output
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["validation_results"][0]["command"] == [
        "python",
        "-m",
        "pytest",
        "tests",
        "-q",
    ]
