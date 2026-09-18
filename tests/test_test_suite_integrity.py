"""Meta-tests that keep E2E and smoke tests fail-closed and isolated."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
TARGETS = (
    ROOT / "tests" / "test_e2e.py",
    ROOT / "tests" / "test_e2e_mocked.py",
    ROOT / "tests" / "test_smoke.py",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_e2e_and_smoke_tests_do_not_return_boolean_outcomes():
    violations: list[str] = []
    for path in TARGETS:
        for node in _tree(path).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    violations.append(f"{path.name}:{child.lineno}:{node.name}")

    assert violations == [], "pytest ignores returned False values: " + ", ".join(violations)


def test_e2e_and_smoke_modules_do_not_mutate_environment_during_collection():
    violations: list[str] = []
    for path in TARGETS:
        for node in _tree(path).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                value = target.value
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "os"
                    and value.attr == "environ"
                ):
                    violations.append(f"{path.name}:{node.lineno}")

    assert violations == [], "collection-time os.environ writes: " + ", ".join(violations)


def test_http_suite_does_not_import_thread_blocking_testclient():
    violations: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module not in {"fastapi.testclient", "starlette.testclient"}:
                continue
            if any(alias.name == "TestClient" for alias in node.names):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == [], (
        "use httpx.AsyncClient with ASGITransport instead of the thread-blocking "
        "Starlette TestClient: " + ", ".join(violations)
    )
