"""Enforce independent global line and branch coverage floors."""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
COUNTS = ("covered_lines", "num_statements", "missing_lines", "num_branches",
          "covered_branches", "missing_branches", "excluded_lines", "num_partial_branches")


def counts(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("summary must be an object")
    for name in COUNTS:
        if type(value.get(name)) is not int or value[name] < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if value["covered_lines"] + value["missing_lines"] != value["num_statements"]:
        raise ValueError("inconsistent line counts")
    if value["covered_branches"] + value["missing_branches"] != value["num_branches"]:
        raise ValueError("inconsistent branch counts")
    if value["num_partial_branches"] > value["missing_branches"]:
        raise ValueError("inconsistent partial branch count")
    return value


def line_set(value: list) -> set[int]:
    if not isinstance(value, list) or any(type(line) is not int or line <= 0 for line in value):
        raise ValueError("line details must contain positive integer line numbers")
    if len(set(value)) != len(value):
        raise ValueError("duplicate line details")
    return set(value)


def branch_set(value: list) -> set[tuple[int, int]]:
    if not isinstance(value, list) or any(
        not isinstance(arc, list) or len(arc) != 2
        or any(type(line) is not int for line in arc) or arc[0] <= 0 or arc[1] == 0
        for arc in value
    ):
        raise ValueError("invalid branch details")
    arcs = {tuple(arc) for arc in value}
    if len(arcs) != len(value):
        raise ValueError("duplicate branch details")
    return arcs


def validate_report(report: dict, source: Path) -> dict:
    version = report["meta"]["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("coverage tool version is required")
    if report["meta"]["branch_coverage"] is not True:
        raise ValueError("branch measurement is required")
    expected = {"src/agent_team/" + path.relative_to(source).as_posix()
                for path in source.rglob("*.py") if path.is_file()}
    if not expected or set(report["files"]) != expected:
        raise ValueError("report must contain every production Python file and no other files")
    totals = counts(report["totals"])
    if totals["num_statements"] == 0 or totals["num_branches"] == 0:
        raise ValueError("empty line or branch measurement")
    summed = dict.fromkeys(COUNTS, 0)
    for file in report["files"].values():
        summary = counts(file["summary"])
        executed = line_set(file["executed_lines"])
        missing = line_set(file["missing_lines"])
        excluded = line_set(file["excluded_lines"])
        branches = branch_set(file["executed_branches"])
        missing_branches = branch_set(file["missing_branches"])
        if executed & missing or missing & excluded or branches & missing_branches:
            raise ValueError("overlapping covered and missing details")
        if (len(executed - excluded) != summary["covered_lines"]
                or len(missing) != summary["missing_lines"]
                or len(excluded) != summary["excluded_lines"]
                or len(branches) != summary["covered_branches"]
                or len(missing_branches) != summary["missing_branches"]):
            raise ValueError("summary differs from measured details")
        for name in COUNTS:
            summed[name] += summary[name]
    if any(totals[name] != summed[name] for name in COUNTS):
        raise ValueError("totals differ from file measurements")
    return totals


def reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def validate_policy(policy: dict) -> dict:
    for name in ("line-floor", "branch-floor", "target"):
        number = policy[name]
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 100:
            raise ValueError(f"{name} must be a finite number from 0 to 100")
    if max(policy["line-floor"], policy["branch-floor"]) > policy["target"]:
        raise ValueError("coverage floors cannot exceed the target")
    return policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT / "src/agent_team")
    parser.add_argument("--policy", type=Path, default=ROOT / "pyproject.toml")
    arguments = parser.parse_args()
    try:
        report = json.loads(arguments.report.read_text(), parse_constant=reject_constant)
        policy = validate_policy(tomllib.loads(arguments.policy.read_text())["tool"]["agent-team"]["coverage"])
        totals = validate_report(report, arguments.source_root)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"invalid coverage: {error}", file=sys.stderr)
        return 1
    failed = False
    print(f"Coverage scope: pytest interpreter; child processes are not measured. Target: {policy['target']:.2f}% each.")
    for label, covered, total in (("line", "covered_lines", "num_statements"),
                                   ("branch", "covered_branches", "num_branches")):
        percentage = Fraction(totals[covered] * 100, totals[total])
        floor = Fraction(str(policy[label + "-floor"]))
        print(f"{label} coverage: {float(percentage):.2f}% (floor {float(floor):.2f}%)")
        if percentage < floor:
            print(f"{label} coverage is below its floor", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
