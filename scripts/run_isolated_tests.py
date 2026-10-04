"""Run the installed deterministic suite without inherited deployment settings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--coverage-dir", type=Path,
                        help="external report directory; measures pytest, not child processes")
    options, arguments = parser.parse_known_args()
    if options.inventory is not None:
        command = [sys.executable, str(root / "scripts/capture_pytest.py"),
                   str(options.inventory.resolve()), *arguments]
    else:
        targets = arguments or ["tests"]
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
    coverage = None
    if options.coverage_dir is not None:
        if not options.coverage_dir.is_absolute():
            parser.error("--coverage-dir must be an absolute external directory")
        coverage_dir = options.coverage_dir.resolve()
        if coverage_dir.is_relative_to(root) or root.is_relative_to(coverage_dir):
            parser.error("--coverage-dir must be outside the checkout and its ancestors")
        try:
            if coverage_dir.exists() and any(coverage_dir.iterdir()):
                parser.error("--coverage-dir must be empty; previous evidence is preserved")
            coverage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            parser.error(f"invalid --coverage-dir: {error}")
        coverage = [sys.executable, "-m", "coverage"]
        configuration = f"--rcfile={root / 'pyproject.toml'}"
        data_file = f"--data-file={coverage_dir / '.coverage'}"
        command = [*coverage, "run", configuration, data_file, *command[1:]]
    with tempfile.TemporaryDirectory(prefix="agent-team-tests-") as directory:
        sandbox = Path(directory)
        home = sandbox / "home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR",
        }}
        env.update({
            "HOME": str(home),
            "AGENT_TEAM_STATE_DIR": str(sandbox / "state"),
            "AGENT_TEAM_WORKSPACE_DIR": str(sandbox / "workspace"),
            "LLM_BASE_URL": "http://127.0.0.1:1/v1",
            "LLM_MODEL": "test-model",
            # Fixture networking is direct. Avoid macOS system-proxy lookup,
            # which loads fork-unsafe system frameworks in the test process.
            "NO_PROXY": "*",
            "no_proxy": "*",
            "XDG_CONFIG_HOME": str(sandbox / "config"),
            "XDG_DATA_HOME": str(sandbox / "data"),
            "XDG_CACHE_HOME": str(sandbox / "cache"),
        })
        test_exit = subprocess.run(
            command,
            cwd=root, env=env, check=False,
        ).returncode
        if coverage is None:
            return test_exit
        print("Coverage scope: pytest interpreter; child processes are not measured.", file=sys.stderr)
        (coverage_dir / "measurement.json").write_text(json.dumps({
            "schema_version": 1, "scope": "pytest-interpreter", "subprocesses_measured": False,
            "pytest_exit": test_exit, "source": "agent_team", "branch": True,
        }, indent=2) + "\n")
        report_exit = 0
        for report in (
            ["json", "-o", str(coverage_dir / "coverage.json")],
            ["xml", "-o", str(coverage_dir / "coverage.xml")],
            ["html", "-d", str(coverage_dir / "html")],
        ):
            result = subprocess.run([*coverage, report[0], configuration, data_file, *report[1:]],
                                    cwd=root, env=env, check=False)
            report_exit = report_exit or result.returncode
        return test_exit or report_exit


if __name__ == "__main__":
    raise SystemExit(main())
