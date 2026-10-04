"""Inventory CLI distinguishes host exclusions from missing required evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def capture(tmp_path, source, filename="test_fixture.py"):
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / filename).write_text(source)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/capture_pytest.py", scripts / "capture_pytest.py")
    shutil.copyfile(ROOT / "scripts/platform_proofs.py", scripts / "platform_proofs.py")
    support = ROOT / "tests/conftest.py"
    if support.exists():
        shutil.copyfile(support, suite / "conftest.py")
    destination = tmp_path / "inventory.json"
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, str(scripts / "capture_pytest.py"), str(destination)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20,
    )
    return result, json.loads(destination.read_text())


def test_foreign_native_proof_is_explicit_platform_exclusion(tmp_path):
    result, inventory = capture(tmp_path, '''
import pytest

def test_portable():
    assert True

@pytest.mark.required_platform("darwin")
def test_darwin_detached_child_capability_boundary():
    import sys
    assert sys.platform == "darwin", "foreign platform proof must never run"
''', filename="test_child_confinement.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert inventory["outcome"] == "passed"
    assert inventory["host_platform"] == sys.platform
    assert inventory["acceptance_scope"] == ("darwin-native" if sys.platform == "darwin" else "portable")
    required = ["tests/test_child_confinement.py::test_portable"]
    if sys.platform == "darwin":
        required.append("tests/test_child_confinement.py::test_darwin_detached_child_capability_boundary")
    assert [item["id"] for item in inventory["required"]] == required
    assert inventory["optional"] == []
    if sys.platform == "darwin":
        assert inventory["platform_exclusions"] == []
    else:
        excluded, = inventory["platform_exclusions"]
        assert excluded["id"] == "tests/test_child_confinement.py::test_darwin_detached_child_capability_boundary"
        assert excluded["required_platform"] == "darwin"
        assert excluded["outcome"] == "excluded"


def test_arbitrary_required_test_cannot_exempt_itself_with_platform_marker(tmp_path):
    foreign = "darwin" if sys.platform != "darwin" else "linux"
    result, inventory = capture(tmp_path, f'''
import pytest

def test_portable():
    assert True

@pytest.mark.required_platform({foreign!r})
def test_arbitrary_required():
    pytest.skip("missing required evidence")
''')
    assert result.returncode == 1
    assert inventory["outcome"] != "passed"
    assert inventory["platform_exclusions"] == []
    assert inventory["required"][1]["outcome"] == "unverified"


@pytest.mark.parametrize("missing", ["skip", "xfail", "fail"])
def test_current_host_missing_capability_cannot_pass_inventory(tmp_path, missing):
    result, inventory = capture(tmp_path, f'''
import pytest

def test_portable():
    assert True

@pytest.mark.required_platform({sys.platform!r})
def test_native_capability():
    pytest.{missing}("required native capability absent")
''')
    assert result.returncode == 1
    assert inventory["outcome"] != "passed"
    assert inventory["platform_exclusions"] == []
    assert inventory["optional"] == []
    assert inventory["required"][1]["outcome"] == ("failed" if missing == "fail" else "unverified")
