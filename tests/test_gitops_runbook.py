"""Acceptance checks for the GitOps operator runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNBOOK = ROOT / "docs" / "gitops-runbook.md"


REQUIRED_SECTIONS = (
    "## Repository contract",
    "## External prerequisites",
    "## Promotion workflow",
    "## Executable extension trust policy",
    "## Bootstrap a clone",
    "## Reconcile Pi assets",
    "## Health and readiness checks",
    "## Rollback",
    "## Cancellation and restart recovery",
    "## Operator checklist",
)

REQUIRED_COMMANDS = (
    "git status --porcelain",
    "git rev-parse --verify HEAD",
    "agent-team provenance capture",
    "python pi/manifest_validator.py pi/manifest.json",
    "agent-team bootstrap",
    "--expected-revision",
    "agent-team pi reconcile",
    "--candidate-revision",
    "--deployed-revision",
    "--dry-run",
    "--approve",
    "agent-team pi status",
    "agent-team pi rollback",
    "curl -fsS \"$AGENT_TEAM_URL/health\"",
    "curl -fsS \"$AGENT_TEAM_URL/ready\"",
    "curl -fsS \"$MODEL_ENDPOINT/models\"",
    "/sessions/{session_id}/cancel",
    "s6-svc",
)


def test_gitops_runbook_covers_the_complete_operational_contract():
    text = RUNBOOK.read_text(encoding="utf-8")
    lowered = text.lower()

    for section in REQUIRED_SECTIONS:
        assert section in text, section
    for command in REQUIRED_COMMANDS:
        assert command in text, command

    assert "tracked" in lowered and "untracked" in lowered
    assert "external" in lowered and "mounted" in lowered
    assert "manifest.json" in text
    assert "remote push" in lowered
    assert "does not install" in lowered or "never installs" in lowered
    assert "does not fetch" in lowered or "never fetches" in lowered
    assert "processing" in lowered and "blocked" in lowered
    assert "last-known-good" in lowered


def test_readme_links_to_the_operator_runbook():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/gitops-runbook.md" in readme
