"""Build lifecycle acceptance metadata from complete collection and actual proof.

Collection never executes a lifecycle test. A registry is written only after
every independently collected node has passed and has exactly one retained
``lifecycle_evidence`` record in the supplied inventory.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "test_foreground_contract.py",
    "test_child_confinement.py",
    "test_forced_child_foreground.py",
    "test_foreground_phase_deadlines.py",
)
DEADLINE_SECONDS = 20
SENSITIVE_TEXT = (
    "sentinel", "secret", "password", "http://user:", "https://user:",
    "bearer fixture", "fixture-api-token",
)
SENSITIVE_FIELDS = {"token", "authorization", "password", "api_key", "generation", "owner_token"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_value(value: Any, *, field: str = "") -> Any:
    """Keep concrete safe parameters, replacing sensitive fixture values by hash."""
    if field.lower() in SENSITIVE_FIELDS:
        return {"type": type(value).__name__, "fixture_value_sha256": digest(repr(value))}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and not any(word in value.lower() for word in SENSITIVE_TEXT):
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: safe_value(item, field=key) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe_value(item) for item in value]
    return {"type": type(value).__name__, "fixture_value_sha256": digest(repr(value))}


class SafeAssertions(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and any(word in node.value.lower() for word in SENSITIVE_TEXT):
            return ast.copy_location(ast.Constant("<redacted-test-value>"), node)
        return node


def safe_expression(node: ast.AST) -> str:
    return ast.unparse(SafeAssertions().visit(copy.deepcopy(node)))


def source_oracles(function: ast.AST) -> tuple[list[str], list[str]]:
    assertions = [safe_expression(node.test) for node in ast.walk(function) if isinstance(node, ast.Assert)]
    exceptions = [safe_expression(node) for node in ast.walk(function)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "raises"]
    # Checked subprocess programs contain independent fork/generation oracles.
    # Record their assertions, never their executable source or record payloads.
    for assignment in ast.walk(function):
        if (not isinstance(assignment, ast.Assign)
                or not any(isinstance(target, ast.Name) and target.id in {"code", "program"}
                           for target in assignment.targets)
                or not isinstance(assignment.value, ast.Constant)
                or not isinstance(assignment.value.value, str)):
            continue
        try:
            child = ast.parse(assignment.value.value)
        except SyntaxError:
            continue
        assertions.extend("checked subprocess assertion: " + safe_expression(node.test)
                          for node in ast.walk(child) if isinstance(node, ast.Assert))
    return assertions, exceptions


class Collection:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.deselected: list[str] = []

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        self.items = list(items)

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected.extend(item.nodeid for item in items)


def inventory_nodes(document: dict) -> dict[str, dict]:
    if isinstance(document.get("required"), list):
        entries = document["required"]
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in entries):
            raise ValueError("inventory required nodes have an invalid shape")
        nodes = {item["id"]: item for item in entries}
        if len(nodes) != len(entries):
            raise ValueError("inventory contains duplicate node IDs")
        return nodes
    if isinstance(document.get("nodes"), dict):
        nodes = document["nodes"]
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in nodes.items()):
            raise ValueError("inventory nodes have an invalid shape")
        return nodes
    raise ValueError("inventory must contain required node records or a nodes mapping")


def required_proof(nodeid: str, node: dict | None) -> dict:
    if not node or node.get("outcome") != "passed":
        raise ValueError(f"required lifecycle node is absent or did not pass: {nodeid}")
    proofs = node.get("evidence", {}).get("lifecycle_evidence", [])
    if not isinstance(proofs, list) or len(proofs) != 1 or not isinstance(proofs[0], dict):
        raise ValueError(f"required lifecycle node lacks exactly one proof: {nodeid}")
    proof = proofs[0]
    if (proof.get("schema_version") != 1 or proof.get("proofs_passed") is not True
            or not isinstance(proof.get("id"), str) or not proof["id"].startswith("lifecycle.")
            or not all(isinstance(proof.get(field), dict)
                       for field in ("expected", "observed", "before", "after", "cleanup"))):
        raise ValueError(f"required lifecycle proof has invalid schema or disposition: {nodeid}")
    duration = proof.get("duration_seconds")
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or not math.isfinite(duration) or not 0 <= duration <= DEADLINE_SECONDS):
        raise ValueError(f"required lifecycle proof exceeds its finite outer deadline: {nodeid}")
    phases = node.get("phases")
    if not isinstance(phases, list) or {item.get("phase") for item in phases} != {"setup", "call", "teardown"}:
        raise ValueError(f"required lifecycle node lacks complete phase timing: {nodeid}")
    durations = [item.get("duration") for item in phases]
    if (any(not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0 for value in durations)
            or sum(durations) > DEADLINE_SECONDS):
        raise ValueError(f"required lifecycle node phases exceed outer deadline: {nodeid}")
    if any(item.get("outcome") != "passed" or item.get("xfail") for item in phases):
        raise ValueError(f"required lifecycle node has an unverified phase: {nodeid}")
    return proof


def build(items: list[Any], nodes: dict[str, dict]) -> dict:
    cases = []
    source_files = {}
    for item in items:
        path = Path(str(item.path))
        relative = str(path.relative_to(ROOT))
        source = path.read_text()
        source_files[relative] = digest(source)
        name = item.originalname or item.name
        function = next(node for node in ast.walk(ast.parse(source))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
        parameters = getattr(getattr(item, "callspec", None), "params", {})
        operations = sorted({safe_expression(node.func) for node in ast.walk(function)
                             if isinstance(node, ast.Call)})
        assertions, exception_checks = source_oracles(function)
        if not assertions and not exception_checks:
            raise ValueError(f"required lifecycle node has no independent source oracle: {item.nodeid}")
        proof = required_proof(item.nodeid, nodes.get(item.nodeid))
        cases.append({
            "id": proof["id"], "contract": "4/8", "nodes": [item.nodeid],
            "setup": {"test": name, "fixture_arguments": [argument.arg for argument in function.args.args
                                                             if argument.arg not in parameters],
                      "parameters": safe_value(parameters),
                      "isolation": "Test-owned external pytest roots, isolated child environment and loopback fixture transport; host process tests are required."},
            "operation": {"entry_point": item.nodeid, "called_functions": operations},
            "expected": {"assertions": assertions, "expected_exception_checks": exception_checks,
                         "independent_recorded_expectations": safe_value(proof["expected"]),
                         "source": relative, "source_sha256": digest(ast.get_source_segment(source, function))},
            "allowed_fs_changes": {
                "fixture_setup_and_teardown": "Explicit test-owned temporary roots and controlled negative fixtures only.",
                "foreground_owner": ["Selected external state under sole held RuntimeLease", "Selected external workspace and artifacts through guarded production operations", "Lock metadata creation/removal only by its owner"],
                "contender_and_preflight": "No selected state, workspace, config, incumbent owner metadata or durable store changes.",
                "confined_child": "Only its owned scratch directory; durable paths and aliases, inherited durable descriptors and commit channels remain denied.",
                "stopped_or_stale_generation": "No production durable mutation after ownership ends or generation is rejected.",
                "scope": "Concrete source assertions and captured before/after manifest predicates govern which operation runs in this case; no checkout/default-home writes.",
            },
            "deadline_seconds": DEADLINE_SECONDS,
            "cleanup": {"obligations": ["Controller waits for its directly owned processes; reparented forced-exit children require explicit termination and observed exit, with the OS reaper recorded", "No claim that second-signal owner termination reaps all descendants", "Terminate or escalate resistant children within the same outer budget", "Retain an unreaped owned process handle on a failed reap; never falsely release ownership", "Restore monkeypatches, close fixture listener/HTTP/IPC/descriptors and remove owned private temporary directories"],
                        "explicit_cleanup_calls": [operation for operation in operations if operation.endswith((".terminate", ".kill", ".wait", ".finish", ".close", ".release", ".join", ".communicate", ".reap", ".terminate_all_workers"))],
                        "recorded_process_obligations": safe_value(proof["cleanup"])},
        })
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("lifecycle proof IDs are empty or not unique")
    return {"schema_version": 1, "requires_metadata": True,
            "evidence_property": "lifecycle_evidence", "source_files": source_files,
            "cases": cases, "required_branches": [case["id"] for case in cases]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "tests/lifecycle_cases.json")
    arguments = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("run the builder from the repository root to preserve exact node IDs")
    missing = [name for name in FILES if not (ROOT / "tests" / name).is_file()]
    if missing:
        parser.error("required lifecycle source file missing: " + ", ".join(missing))
    source_before = {"tests/" + name: digest((ROOT / "tests" / name).read_text()) for name in FILES}
    collector = Collection()
    status = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider",
                          *["tests/" + name for name in FILES]], plugins=[collector])
    if status or collector.deselected:
        parser.error("required lifecycle collection failed or deselected a node")
    expected_files = {str(ROOT / "tests" / name) for name in FILES}
    if {str(item.path) for item in collector.items} != expected_files:
        parser.error("each required lifecycle file must collect at least one node")
    try:
        nodes = inventory_nodes(json.loads(arguments.inventory.read_text()))
        document = build(collector.items, nodes)
        source_after = {"tests/" + name: digest((ROOT / "tests" / name).read_text()) for name in FILES}
        if source_before != source_after or document["source_files"] != source_before:
            raise ValueError("required lifecycle source changed during collection or metadata generation")
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as error:
        parser.error(str(error))
    arguments.output.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps({"lifecycle_cases": len(document["cases"]), "complete_collection": True,
                      "required_files": list(FILES), "output": str(arguments.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
