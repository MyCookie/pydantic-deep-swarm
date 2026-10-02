"""Reproduce the immutable historical state-path defect in an isolated install."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


BASELINE = "b3c0d2263b8a606efc0caa849876d0fb1bd6ef00"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    repo = args.baseline.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != BASELINE:
        parser.error("baseline must be the exact historical revision")
    with tempfile.TemporaryDirectory(prefix="agent-team-escape-") as temporary:
        base = Path(temporary)
        home, state = base / "home", base / "selected-state"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
        env.update(HOME=str(home), AGENT_TEAM_STATE_DIR=str(state), LLM_BASE_URL="http://127.0.0.1:1/v1")
        for role in ("PRINCIPAL", "MANAGER", "WORKER", "CURATOR"):
            env[f"{role}_MODEL"] = "fixture-model"
            env[f"{role}_BASE_URL"] = env["LLM_BASE_URL"]
        init = subprocess.run([str(repo / ".venv/bin/agent-team"), "init"], cwd=base,
                              env=env, capture_output=True, text=True, timeout=20)
        probe = subprocess.run([str(repo / ".venv/bin/python"), "-c",
                                "from agent_team.config import get_config; print(get_config().runtime.state_dir)"],
                               cwd=base, env=env, capture_output=True, text=True, timeout=20)
        resolved = probe.stdout.strip()
        reproduced = init.returncode == probe.returncode == 0 and resolved == str(home / ".agent-team") and resolved != str(state)
        report = {"report_kind": "agent-team.baseline-escape", "schema_version": 1,
                  "revision": revision, "outcome": "reproduced_red" if reproduced else "failed",
                  "requested_state": str(state), "observed_state": resolved,
                  "init_exit": init.returncode, "probe_exit": probe.returncode,
                  "config_yaml": (state / "config/config.yaml").read_text() if init.returncode == 0 else None,
                  "historical_interface_deviation": "Read-only loader probe; baseline lacks serve and doctor.",
                  "default_home_created": (home / ".agent-team").exists()}
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"outcome": report["outcome"], "revision": revision}))
        return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
