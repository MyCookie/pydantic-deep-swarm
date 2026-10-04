"""Explicit coverage collection retains the isolated installed-test boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def invoke(report, test, env=None, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / "scripts/run_isolated_tests.py"),
                           "--coverage-dir", str(report), str(test)],
                          capture_output=True, text=True, timeout=60, env=env, cwd=cwd)


def test_explicit_coverage_measures_pytest_with_private_environment(tmp_path):
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text('''
import os
from agent_team import redaction

def test_installed_source_and_isolation():
    assert redaction.__file__
    assert "LLM_API_KEY" not in os.environ
    assert "COVERAGE_PROCESS_START" not in os.environ
    assert "COVERAGE_FILE" not in os.environ
    assert "PYTHONPATH" not in os.environ
    assert os.environ["LLM_BASE_URL"] == "http://127.0.0.1:1/v1"
''')
    report = tmp_path / "coverage"
    env = dict(os.environ, LLM_API_KEY="must-not-inherit", COVERAGE_PROCESS_START="untrusted",
               COVERAGE_FILE=str(tmp_path / "inherited-data"), PYTHONPATH="untrusted")
    result = invoke(report, fixture, env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    document = json.loads((report / "coverage.json").read_text())
    assert document["meta"]["branch_coverage"] is True
    assert document["files"]["src/agent_team/redaction.py"]["summary"]["covered_lines"] > 0
    assert set(document["files"]) == {path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src/agent_team").rglob("*.py") if path.is_file()}
    assert (report / "coverage.xml").is_file()
    assert (report / "html/index.html").is_file()
    measurement = json.loads((report / "measurement.json").read_text())
    assert measurement["scope"] == "pytest-interpreter"
    assert measurement["subprocesses_measured"] is False
    assert measurement["pytest_exit"] == 0
    assert not (tmp_path / "inherited-data").exists()


@pytest.mark.parametrize("invalid", ["relative", "nonempty"])
def test_coverage_refuses_ambiguous_or_reused_report_locations(tmp_path, invalid):
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("def test_pass(): assert True\n")
    report = Path("coverage") if invalid == "relative" else tmp_path / "coverage"
    if invalid == "nonempty":
        report.mkdir()
        (report / "previous-evidence").write_text("preserved")
    result = invoke(report, fixture, cwd=tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "coverage" in result.stderr.lower()
    assert not (tmp_path / "coverage/.coverage").exists()
    if invalid == "nonempty": assert (report / "previous-evidence").read_text() == "preserved"


def test_failed_pytest_retains_reports_and_failure_exit(tmp_path):
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("from agent_team import redaction\ndef test_failure(): assert False\n")
    report = tmp_path / "coverage"
    result = invoke(report, fixture)
    assert result.returncode == 1
    assert (report / "coverage.json").is_file()
    measurement = json.loads((report / "measurement.json").read_text())
    assert measurement["pytest_exit"] == 1
