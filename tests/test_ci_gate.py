"""Exercise the required PR gate through its public command interface."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_ci_gate.py"


def gate(needs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--expected", "portable", "acceptance"],
        input=json.dumps(needs), text=True, capture_output=True, check=False,
    )


def test_pr_gate_passes_when_every_required_job_succeeds():
    result = gate({"portable": {"result": "success"}, "acceptance": {"result": "success"}})
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", "unknown"])
def test_pr_gate_blocks_each_nonpassing_required_job(outcome):
    result = gate({"portable": {"result": outcome}, "acceptance": {"result": "success"}})
    assert result.returncode == 1
    assert "portable" in result.stderr


@pytest.mark.parametrize("needs", [
    {}, {"portable": {"result": "success"}},
    {"portable": {"result": "success"}, "acceptance": {"result": "success"},
     "unlisted": {"result": "failure"}},
    {"portable": {}, "acceptance": {"result": "success"}},
    {"portable": "success", "acceptance": {"result": "success"}}, [], None,
])
def test_pr_gate_blocks_incomplete_or_invalid_dependency_inventory(needs):
    result = gate(needs)
    assert result.returncode == 1
    assert "invalid" in result.stderr.lower()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("payload", ["", "{bad json"])
def test_pr_gate_blocks_unreadable_json_without_a_traceback(payload):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--expected", "portable", "acceptance"],
        input=payload, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert "invalid dependency inventory" in result.stderr
    assert "Traceback" not in result.stderr
