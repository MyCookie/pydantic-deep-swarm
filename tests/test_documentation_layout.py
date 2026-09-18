"""Publication gates for the consolidated Agent Team documentation."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
EXPECTED_DOCS = {
    "README.md",
    "architecture.md",
    "end-to-end-workflow.md",
    "gitops-runbook.md",
    "operations.md",
    "testing.md",
}
STALE_TOP_LEVEL = {
    "ARCHITECTURE.md",
    "COMPLETE.md",
    "FINAL_STATUS.md",
    "IMPLEMENTATION_STATUS.md",
    "PROGRESS.md",
    "TDD_DOCUMENTATION.md",
}


def test_reader_documentation_is_consolidated_under_docs():
    assert {path.name for path in DOCS.glob("*.md")} == EXPECTED_DOCS
    assert not {path.name for path in ROOT.glob("*.md")} & STALE_TOP_LEVEL
    assert (ROOT / "README.md").is_file()
    assert "docs/README.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_end_to_end_workflow_covers_every_runtime_boundary():
    workflow = (DOCS / "end-to-end-workflow.md").read_text(encoding="utf-8").lower()
    for boundary in (
        "hermes principal",
        "projectbrief",
        "team manager",
        "worker",
        "artifact",
        "completionreport",
        "persistence",
        "cancellation",
    ):
        assert boundary in workflow


def test_documentation_has_no_broken_relative_markdown_links():
    documents = [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    broken: list[str] = []
    for document in documents:
        for raw_target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().split()[0].strip("<>\"'")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (document.parent / relative).resolve().exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")

    assert broken == []
