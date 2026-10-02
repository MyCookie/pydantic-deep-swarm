"""Regenerate exact configuration/control/doctor case IDs from pytest collection."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from config_doctor_evidence import case_id


FILES = ("test_core_cli.py", "test_owned_control.py", "test_doctor_contract.py", "test_config_expansion.py", "test_swarm_control.py", "test_swarm_cli.py")


class SafeAssertions(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str) and any(word in node.value.lower() for word in ("sentinel", "secret", "password", "http://user:", "https://user:")):
            return ast.copy_location(ast.Constant("<redacted-test-value>"), node)
        return node


class Registry:
    def pytest_collection_modifyitems(self, items):
        cases = []
        for item in items:
            path = Path(str(item.path))
            source = path.read_text()
            tree = ast.parse(source)
            name = item.originalname or item.name
            function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
            assertions = [ast.unparse(SafeAssertions().visit(node.test)) for node in ast.walk(function) if isinstance(node, ast.Assert)]
            exception_checks = [ast.unparse(node) for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "raises"]
            params = getattr(getattr(item, "callspec", None), "params", {})
            concrete = {}
            for key, value in params.items():
                if isinstance(value, (bool, int, float, type(None))):
                    concrete[key] = value
                elif isinstance(value, str) and not any(word in value.lower() for word in ("secret", "password", "http://user:", "https://user:")):
                    concrete[key] = value
                else:
                    concrete[key] = {"type": type(value).__name__, "fixture_value_sha256": hashlib.sha256(repr(value).encode()).hexdigest()}
            operations = sorted({ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)})
            allowed_operations = {}
            for operation in operations:
                if operation in ("diagnose", "resolve_configuration") or operation.endswith((".list_models", ".detect_model", ".inspect")):
                    allowed_operations[operation] = []
                elif operation.endswith(".reconcile"):
                    allowed_operations[operation] = ["selected external YAML when changed and not dry-run", "selected external YAML reconcile lock"]
                elif operation == "atomic_write":
                    allowed_operations[operation] = ["explicit target replacement when committed or uncertain; staging file must be removed"]
                elif operation == "write_report":
                    allowed_operations[operation] = ["explicit safe report destination replacement; no selected state/workspace/config writes"]
                elif operation.endswith(".invoke"):
                    allowed_operations[operation] = ["init: selected external config, durable overwrite backup and parent directories", "doctor: only explicit --report-json output, selected inputs unchanged", "reconcile: selected external YAML and reconcile lock, empty changes under dry-run"]
            contract = "6" if path.name == "test_doctor_contract.py" else "2/5" if path.name in ("test_core_cli.py", "test_owned_control.py") else "2" if path.name == "test_config_expansion.py" else "5"
            cases.append({
                "id": case_id(item.nodeid), "contract": contract, "nodes": [item.nodeid],
                "setup": {"test": name, "fixture_arguments": [arg.arg for arg in function.args.args if arg.arg not in params], "parameters": concrete, "isolation": "Controlled fixture/environment or mock transport; inputs remain under pytest temporary roots."},
                "operation": {"entry_point": item.nodeid, "called_functions": operations},
                "expected": {"assertions": assertions, "expected_exception_checks": exception_checks, "source_sha256": hashlib.sha256(ast.get_source_segment(source, function).encode()).hexdigest()},
                "allowed_fs_changes": {"fixture_setup_and_teardown": "pytest temporary roots", "production_operations": allowed_operations, "scope": "Explicit assertion predicates above govern operation-specific changes; manifests include fixture preparation."},
                "deadline_seconds": 20,
                "cleanup": {"recorder": "Restore operation hooks and process alarm", "fixtures": "pytest restores monkeypatches and controls temporary directory retention", "explicit_cleanup_calls": [call for call in operations if call.endswith((".terminate", ".kill", ".wait", ".close", ".release", ".join"))]},
            })
        destination = Path(__file__).with_name("config_doctor_cases.json")
        destination.write_text(json.dumps({"schema_version": 1, "requires_metadata": True, "evidence_property": "config_doctor_evidence", "cases": cases, "required_branches": [case["id"] for case in cases]}, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(pytest.main(["--collect-only", "-q", *["tests/" + name for name in FILES]], plugins=[Registry()]))
