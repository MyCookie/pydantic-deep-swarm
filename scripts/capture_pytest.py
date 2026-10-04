"""Execute all installed tests and retain complete per-node acceptance inventory."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest
from platform_proofs import NATIVE_PROOFS


class Inventory:
    def __init__(self):
        self.collected = []
        self.deselected = []
        self.nodes = {}
        self.platforms = {}

    def pytest_collection_modifyitems(self, items):
        self.collected = [item.nodeid for item in items]
        self.platforms = {
            item.nodeid: marker.args[0]
            for item in items
            if (marker := item.get_closest_marker("required_platform")) is not None
        }

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        entry = self.nodes.setdefault(report.nodeid, {"phases": [], "outcome": "unverified"})
        phase = {"phase": report.when, "outcome": report.outcome, "duration": report.duration}
        if report.skipped:
            phase["reason"] = str(report.longrepr)
        if getattr(report, "wasxfail", None):
            phase["xfail"] = report.wasxfail
        entry["phases"].append(phase)
        if report.when in ("call", "teardown") and report.user_properties:
            evidence = {}
            for name, value in report.user_properties:
                evidence.setdefault(name, []).append(value)
            entry["evidence"] = evidence
        if report.failed:
            entry["outcome"] = "failed"
        elif report.skipped:
            entry["outcome"] = "unverified"
            entry["skip_reason"] = str(report.longrepr)
        elif report.when == "call" and entry["outcome"] != "failed":
            entry["outcome"] = "passed"
        if getattr(report, "wasxfail", None):
            entry["outcome"] = "unverified"


def main() -> int:
    destination = Path(sys.argv[1])
    inventory = Inventory()
    started = time.monotonic()
    code = pytest.main(["tests", "-q", "-p", "no:cacheprovider", *sys.argv[2:]], plugins=[inventory])
    optional = []
    required = []
    platform_exclusions = []
    for node in inventory.collected:
        result = inventory.nodes.get(node, {"outcome": "unverified", "phases": []})
        result["id"] = node
        platform = inventory.platforms.get(node)
        if (platform is not None and NATIVE_PROOFS.get(node) == platform
                and platform != sys.platform and "skip_reason" in result):
            result.update(lane="native-platform-proof", required_platform=platform, outcome="excluded")
            platform_exclusions.append(result)
        elif node.startswith("tests/test_e2e.py::") or node.endswith("::test_principal_plugin_registers_tool_for_normal_agent"):
            result["lane"] = "actual-external-integration"
            if result["outcome"] == "unverified" and "skip_reason" in result:
                result["outcome"] = "not_selected"
            elif result["outcome"] == "unverified":
                result["outcome"] = "failed"
            optional.append(result)
        else:
            result["lane"] = "standalone-required"
            required.append(result)
    complete = (bool(required) and not inventory.deselected
                and all(item["outcome"] == "passed" for item in required)
                and all(item["outcome"] in {"passed", "not_selected"} for item in optional))
    document = {"report_kind": "agent-team.test-inventory", "schema_version": 1,
                "outcome": "passed" if code == 0 and complete else "failed" if code else "unverified",
                "pytest_exit": code, "duration_seconds": time.monotonic() - started,
                "host_platform": sys.platform,
                "acceptance_scope": "darwin-native" if sys.platform == "darwin" else "portable",
                "collected_count": len(inventory.collected), "deselected": inventory.deselected,
                "required": required, "optional": optional, "platform_exclusions": platform_exclusions}
    destination.write_text(json.dumps(document, indent=2) + "\n")
    return 0 if document["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
