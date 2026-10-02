"""Resolve contract case registries against actual collected passing test nodes."""

from __future__ import annotations

import json
import math
from pathlib import Path


REGISTRIES = ("config_doctor_cases.json", "knowledge_cases.json", "lifecycle_cases.json")


def storage_fault_proved(case: dict, nodes: list[dict]) -> bool:
    """A passing family node cannot substitute for a specific fault occurrence."""
    proofs = [proof for node in nodes
              for proof in node.get("evidence", {}).get("knowledge_evidence", [])
              if proof.get("id") == case["id"]]
    if len(proofs) != 1:
        return False
    proof = proofs[0]
    expected, observed = proof.get("expected", {}), proof.get("observed", {})
    history = proof.get("history", {})
    if not (proof.get("schema_version") == 1 and proof.get("proofs_passed") is True
            and proof.get("private_proofs_cleaned") is True
            and proof.get("site") == case.get("site")
            and proof.get("branch") == case.get("branch")
            and proof.get("occurrence") == case.get("occurrence")
            and observed.get("status") in expected.get("status", [])
            and observed.get("phase") in expected.get("phase", [])
            and observed.get("reason") == expected.get("reason")
            and all(isinstance(proof.get(name), dict) for name in ("before", "interrupted", "after"))):
        return False
    fresh_blocked = proof["branch"] == "fresh" and observed["status"] == "recovery_required"
    if not fresh_blocked and not (history.get("valid") is True
                                 and history.get("raw_value_type_match") is True
                                 and history.get("count") == expected.get("count")
                                 and history.get("digest")):
        return False
    if expected.get("pending_requires_roll_forward") and proof["branch"] != "fresh":
        snapshots = proof.get("snapshots", {})
        if not snapshots or not all(item.get("valid") is True
                                    and item.get("self_contained") is True
                                    and item.get("raw_value_type_match") is True
                                    for item in snapshots.values()):
            return False
    return True


def case_evidence_proved(case: dict, nodes: list[dict], property_name: str) -> bool:
    proofs = [proof for node in nodes for proof in node.get("evidence", {}).get(property_name, [])
              if proof.get("id") == case["id"]]
    if len(proofs) != 1:
        return False
    proof = proofs[0]
    duration, deadline = proof.get("duration_seconds"), case.get("deadline_seconds")
    return (proof.get("schema_version") == 1 and proof.get("proofs_passed") is True
            and isinstance(duration, (float, int)) and not isinstance(duration, bool)
            and isinstance(deadline, (float, int)) and not isinstance(deadline, bool)
            and math.isfinite(duration) and math.isfinite(deadline)
            and 0 <= duration <= deadline and deadline > 0
            and all(name in proof for name in ("expected", "observed", "before", "after", "cleanup")))


def verify_registries(repository: Path, inventory: dict) -> dict:
    executed = {item["id"]: item for item in inventory["required"]}
    cases = []
    failures = []
    for name in REGISTRIES:
        path = repository / "tests" / name
        if not path.is_file():
            failures.append(f"missing required case registry: {name}")
            continue
        document = json.loads(path.read_text())
        if document.get("schema_version") != 1 or not document.get("cases"):
            failures.append(f"invalid or empty case registry: {name}")
            continue
        branch_ids = {item["id"] for item in document["cases"]}
        if "fault_instance_count" in document:
            registered_faults = {item for item in branch_ids if item.startswith("knowledge.fault.")}
            observed_faults = {proof["id"] for node in executed.values()
                               for proof in node.get("evidence", {}).get("knowledge_evidence", [])
                               if proof.get("id", "").startswith("knowledge.fault.")}
            if len(registered_faults) != document["fault_instance_count"] or registered_faults != observed_faults:
                failures.append(f"fault occurrence inventory mismatch: {name}")
        for required in document.get("required_branches", []):
            if required not in branch_ids:
                failures.append(f"required branch omitted: {name}:{required}")
        for case in document["cases"]:
            nodes = []
            for requested in case.get("nodes", []):
                matches = [node for node in executed if node == requested or node.startswith(requested + "[")]
                if not matches:
                    failures.append(f"uncollected required node: {case['id']}:{requested}")
                nodes.extend(matches)
            nodes = sorted(set(nodes))
            green = bool(nodes) and all(executed[node]["outcome"] == "passed" for node in nodes)
            if document.get("requires_metadata"):
                fields = ("setup", "operation", "expected", "allowed_fs_changes", "deadline_seconds", "cleanup")
                green = green and all(name in case for name in fields)
                deadline = case.get("deadline_seconds")
                green = green and (isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
                                   and math.isfinite(deadline) and deadline > 0)
                if green:
                    green = all(sum(phase.get("duration", 0) for phase in executed[node].get("phases", []))
                                <= deadline for node in nodes)
            if document.get("evidence_property"):
                green = green and case_evidence_proved(case, [executed[node] for node in nodes], document["evidence_property"])
            if case["id"].startswith("knowledge.fault."):
                green = green and storage_fault_proved(case, [executed[node] for node in nodes])
            family_prefix = "knowledge.every_fault_site_and_occurrence."
            if case["id"].startswith(family_prefix):
                site = case["id"][len(family_prefix):]
                proofs = [proof for node in nodes
                          for proof in executed[node].get("evidence", {}).get("knowledge_evidence", [])
                          if proof.get("id", "").startswith("knowledge.fault.")]
                expected_ids = {item["id"] for item in document["cases"]
                                if item["id"].startswith("knowledge.fault.") and item.get("site") == site}
                green = green and (bool(expected_ids) and {proof.get("id") for proof in proofs} == expected_ids
                                   and all(proof.get("site") == site and proof.get("proofs_passed") is True
                                           for proof in proofs))
            if name == "knowledge_cases.json" and document.get("requires_metadata") and not case["id"].startswith((
                    "knowledge.fault.", "knowledge.real_process_crash_at_every_fault_site.", family_prefix)):
                manifests = [proof for node in nodes
                             for proof in executed[node].get("evidence", {}).get("knowledge_case_manifest", [])
                             if proof.get("id") == case["id"]]
                green = green and (len(manifests) == 1 and manifests[0].get("schema_version") == 1
                                   and all(isinstance(manifests[0].get(field), dict) for field in ("before", "after")))
            if case["id"].startswith("knowledge.real_process_crash_at_every_fault_site."):
                proofs = [proof for node in nodes
                          for proof in executed[node].get("evidence", {}).get("knowledge_evidence", [])
                          if proof.get("id") == case["id"]]
                if len(proofs) != 1:
                    green = False
                else:
                    proof = proofs[0]
                    process, history = proof.get("process", {}), proof.get("history", {})
                    green = green and (proof.get("proofs_passed") is True
                                       and proof.get("private_proofs_cleaned") is True
                                       and process.get("reaped") is True and process.get("exit") == 77
                                       and isinstance(process.get("pid"), int) and process["pid"] > 0
                                       and history.get("valid") is True and history.get("raw_value_type_match") is True
                                       and proof.get("doctor", {}).get("nonmutation") is True)
            if not green:
                failures.append(f"required case not proved: {case['id']}")
            cases.append({**case, "resolved_nodes": nodes, "outcome": "passed" if green else "unverified"})
    if len({case["id"] for case in cases}) != len(cases):
        failures.append("case IDs are not globally unique")
    return {"report_kind": "agent-team.acceptance-registry", "schema_version": 1,
            "outcome": "passed" if cases and not failures else "unverified",
            "cases": cases, "failures": failures}
