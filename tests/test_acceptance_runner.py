"""Missing contract evidence must block standalone promotion."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "acceptance_registry", Path(__file__).resolve().parents[1] / "scripts/acceptance_registry.py",
)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
audit_spec = importlib.util.spec_from_file_location(
    "asset_access_audit", Path(__file__).resolve().parents[1] / "scripts/asset_access_audit.py",
)
audit_module = importlib.util.module_from_spec(audit_spec)
audit_spec.loader.exec_module(audit_module)


def write_registries(root, *, nodes=None, required=None):
    (root / "tests").mkdir()
    for index, name in enumerate(registry.REGISTRIES):
        (root / "tests" / name).write_text(json.dumps({
            "schema_version": 1,
            "cases": [{"id": f"case-{index}", "contract": str(index),
                       "nodes": nodes if nodes is not None else ["tests/feature.py::test_case"]}],
            "required_branches": required or [f"case-{index}"],
        }))


def test_missing_registry_does_not_pass_even_with_green_suite(tmp_path):
    result = registry.verify_registries(tmp_path, {"required": [{"id": "tests/feature.py::test_case", "outcome": "passed"}]})
    assert result["outcome"] == "unverified"
    assert len(result["failures"]) == 3


@pytest.mark.parametrize("outcome", ["failed", "unverified", "not_selected"])
def test_required_nonpassing_nodes_block_contract_case(tmp_path, outcome):
    write_registries(tmp_path)
    result = registry.verify_registries(tmp_path, {"required": [{"id": "tests/feature.py::test_case", "outcome": outcome}]})
    assert result["outcome"] == "unverified"
    assert all(case["outcome"] == "unverified" for case in result["cases"])


def test_uncollected_required_parameter_blocks_even_when_other_parameters_pass(tmp_path):
    write_registries(tmp_path, nodes=["tests/feature.py::test_case[missing]"])
    result = registry.verify_registries(tmp_path, {"required": [{"id": "tests/feature.py::test_case[actual]", "outcome": "passed"}]})
    assert result["outcome"] == "unverified"
    assert any("uncollected required node" in failure for failure in result["failures"])


def test_omitted_declared_branch_blocks_registry(tmp_path):
    write_registries(tmp_path, required=["required-but-omitted"])
    result = registry.verify_registries(tmp_path, {"required": [{"id": "tests/feature.py::test_case", "outcome": "passed"}]})
    assert result["outcome"] == "unverified"
    assert any("required branch omitted" in failure for failure in result["failures"])


def test_parametrized_case_requires_every_collected_parameter(tmp_path):
    write_registries(tmp_path)
    result = registry.verify_registries(tmp_path, {"required": [
        {"id": "tests/feature.py::test_case[a]", "outcome": "passed"},
        {"id": "tests/feature.py::test_case[b]", "outcome": "unverified"},
    ]})
    assert result["outcome"] == "unverified"
    assert result["cases"][0]["resolved_nodes"] == ["tests/feature.py::test_case[a]", "tests/feature.py::test_case[b]"]


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong-occurrence", "history", "snapshot", "status", "none"])
def test_fault_occurrence_requires_its_own_complete_proof(damage):
    case = {"id": "knowledge.fault.legacy.after_publish.0", "site": "after_publish", "branch": "legacy", "occurrence": 0}
    proof = {**case, "schema_version": 1, "proofs_passed": True, "private_proofs_cleaned": True,
             "expected": {"status": ["migrated"], "phase": ["COMPLETED"], "reason": None,
                          "count": 1, "pending_requires_roll_forward": True},
             "observed": {"status": "migrated", "phase": "COMPLETED", "reason": None},
             "before": {}, "interrupted": {}, "after": {},
             "history": {"valid": True, "raw_value_type_match": True, "count": 1, "digest": "fixture"},
             "snapshots": {"legacy": {"valid": True, "self_contained": True, "raw_value_type_match": True}}}
    if damage == "wrong-occurrence": proof["occurrence"] = 1
    if damage == "history": proof["history"]["raw_value_type_match"] = False
    if damage == "snapshot": proof["snapshots"] = {}
    if damage == "status": proof["observed"]["status"] = "canonical"
    proofs = [] if damage == "missing" else [proof, proof] if damage == "duplicate" else [proof]
    assert registry.storage_fault_proved(case, [{"evidence": {"knowledge_evidence": proofs}}]) == (damage == "none")


@pytest.mark.parametrize("duration,deadline,expected", [(1, 20, True), (21, 20, False), (float("nan"), 20, False), (1, float("inf"), False)])
def test_case_proof_rejects_overdue_or_unbounded_execution(duration, deadline, expected):
    case = {"id": "fixture-case", "deadline_seconds": deadline}
    proof = {"id": case["id"], "schema_version": 1, "proofs_passed": True,
             "duration_seconds": duration, "expected": {}, "observed": {},
             "before": {}, "after": {}, "cleanup": {}}
    assert registry.case_evidence_proved(case, [{"evidence": {"fixture_evidence": [proof]}}], "fixture_evidence") == expected


@pytest.mark.parametrize("damage", ["missing-manifest", "wrong-id", "overdue", "none"])
def test_ordinary_storage_case_requires_its_own_bounded_manifest(tmp_path, damage):
    write_registries(tmp_path)
    path = tmp_path / "tests/knowledge_cases.json"
    document = json.loads(path.read_text())
    document["requires_metadata"] = True
    case = document["cases"][0]
    case.update(setup={}, operation={}, expected={}, allowed_fs_changes={}, deadline_seconds=5, cleanup={})
    path.write_text(json.dumps(document))
    manifest = {"id": case["id"], "schema_version": 1, "before": {}, "after": {}}
    if damage == "wrong-id": manifest["id"] = "another-case"
    node = {"id": "tests/feature.py::test_case", "outcome": "passed",
            "phases": [{"duration": 6 if damage == "overdue" else 1}],
            "evidence": {"knowledge_case_manifest": [] if damage == "missing-manifest" else [manifest]}}
    result = registry.verify_registries(tmp_path, {"required": [node]})
    assert (result["outcome"] == "passed") == (damage == "none")


@pytest.mark.parametrize("forbidden", [False, True], ids=["allowed-core-read", "denied-fallback-read"])
def test_console_access_audit_observes_reads_and_denies_source_fallback(tmp_path, forbidden):
    site = tmp_path / "audit-site"
    site.mkdir()
    protected = tmp_path / "source-fallback"
    protected.mkdir()
    target = (protected if forbidden else tmp_path) / "file.txt"
    target.write_text("fixture")
    log = tmp_path / "access.jsonl"
    audit_module.install_audit(site, log, [protected])
    console = tmp_path / "agent-team"
    console.write_text(f"from pathlib import Path\nPath({str(target)!r}).read_text()\n")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env["PYTHONPATH"] = str(site)  # fixture hook only; never package source
    result = subprocess.run([sys.executable, str(console)], env=env, capture_output=True, text=True)
    assert result.returncode == (1 if forbidden else 0)
    evidence = audit_module.read_audit(log)
    assert evidence["denied_attempts"] == (1 if forbidden else 0)
    assert any(event.get("path") == str(target.resolve()) for event in evidence["events"])
