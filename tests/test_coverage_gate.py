"""Coverage policy is enforced through the reviewable command line boundary."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def coverage_case(tmp_path):
    source = tmp_path / "src" / "agent_team"
    source.mkdir(parents=True)
    (source / "sample.py").write_text("value = 1\n" * 10)
    summary = {"covered_lines": 9, "num_statements": 10, "missing_lines": 1,
               "num_branches": 2, "covered_branches": 1, "missing_branches": 1,
               "excluded_lines": 0, "num_partial_branches": 1}
    document = {"meta": {"branch_coverage": True, "version": "7.16.2"}, "totals": summary.copy(), "files": {
        "src/agent_team/sample.py": {"summary": summary.copy(),
            "executed_lines": list(range(1, 10)), "missing_lines": [10], "excluded_lines": [],
            "executed_branches": [[1, 2]], "missing_branches": [[1, 3]]}}}
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(document))
    policy = tmp_path / "pyproject.toml"
    policy.write_text("[tool.agent-team.coverage]\nline-floor = 90.0\nbranch-floor = 50.0\ntarget = 95.0\n")
    return source, report, policy, document


def check(source, report, policy):
    return subprocess.run([sys.executable, str(ROOT / "scripts/check_coverage.py"), str(report),
                           "--source-root", str(source), "--policy", str(policy)],
                          capture_output=True, text=True, timeout=10)


def test_line_and_branch_floors_are_independent_and_inclusive(tmp_path):
    source, report, policy, _ = coverage_case(tmp_path)
    result = check(source, report, policy)
    assert result.returncode == 0, result.stderr
    assert "90.00%" in result.stdout and "50.00%" in result.stdout
    policy.write_text("[tool.agent-team.coverage]\nline-floor = 90.01\nbranch-floor = 50.0\ntarget = 95.0\n")
    result = check(source, report, policy)
    assert result.returncode == 1 and "line" in result.stderr.lower()
    policy.write_text("[tool.agent-team.coverage]\nline-floor = 90.0\nbranch-floor = 50.01\ntarget = 95.0\n")
    result = check(source, report, policy)
    assert result.returncode == 1 and "branch" in result.stderr.lower()


@pytest.mark.parametrize("invalid", ["missing_report", "invalid_json", "missing_source",
    "missing_totals", "zero_lines", "zero_branches", "nan", "boolean_count", "negative_count",
    "inconsistent_totals", "inconsistent_file", "duplicate_lines", "overlapping_lines",
    "absolute_source", "missing_branch_data", "extra_source", "missing_version", "no_branches"])
def test_incomplete_or_malformed_measurement_cannot_pass(tmp_path, invalid):
    source, report, policy, document = coverage_case(tmp_path)
    file = document["files"]["src/agent_team/sample.py"]
    if invalid == "missing_report": report.unlink()
    elif invalid == "invalid_json": report.write_text("not json")
    else:
        if invalid == "missing_source": (source / "unmeasured.py").write_text("value = 1\n")
        elif invalid == "missing_totals": del document["totals"]
        elif invalid == "zero_lines": document["totals"]["num_statements"] = 0
        elif invalid == "zero_branches": document["totals"]["num_branches"] = 0
        elif invalid == "nan": document["totals"]["percent_covered"] = float("nan")
        elif invalid == "boolean_count": document["totals"]["covered_lines"] = True
        elif invalid == "negative_count": document["totals"]["missing_lines"] = -1
        elif invalid == "inconsistent_totals": document["totals"]["covered_lines"] = 10
        elif invalid == "inconsistent_file": file["summary"]["covered_lines"] = 10
        elif invalid == "duplicate_lines": file["executed_lines"].append(1)
        elif invalid == "overlapping_lines": file["missing_lines"] = [1]
        elif invalid == "absolute_source": document["files"] = {str(source / "sample.py"): file}
        elif invalid == "missing_branch_data": del file["executed_branches"]
        elif invalid == "extra_source": document["files"]["tests/test_sample.py"] = file
        elif invalid == "missing_version": del document["meta"]["version"]
        elif invalid == "no_branches": document["meta"]["branch_coverage"] = False
        report.write_text(json.dumps(document))
    result = check(source, report, policy)
    assert result.returncode == 1, (invalid, result.stdout, result.stderr)
    assert "invalid coverage" in result.stderr.lower(), result.stderr


@pytest.mark.parametrize("invalid", ["nan", "inf", "true", "-1", "101", '"90"', "missing", "target_below_floor"])
def test_invalid_policy_cannot_disable_the_gate(tmp_path, invalid):
    source, report, policy, _ = coverage_case(tmp_path)
    line = "" if invalid == "missing" else f"line-floor = {invalid}\n"
    target = "95.0"
    if invalid == "target_below_floor": line, target = "line-floor = 90.0\n", "80.0"
    policy.write_text(f"[tool.agent-team.coverage]\n{line}branch-floor = 50.0\ntarget = {target}\n")
    result = check(source, report, policy)
    assert result.returncode == 1, result.stdout
    assert "invalid coverage" in result.stderr.lower(), result.stderr


def test_threshold_comparison_does_not_round_measured_percentages(tmp_path):
    source, report, policy, _ = coverage_case(tmp_path)
    policy.write_text("[tool.agent-team.coverage]\nline-floor = 90.0001\nbranch-floor = 50.0\ntarget = 95.0\n")
    result = check(source, report, policy)
    assert result.returncode == 1
    assert "90.00%" in result.stdout
