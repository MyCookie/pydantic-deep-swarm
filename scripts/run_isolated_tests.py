"""Run the installed deterministic suite without inherited deployment settings."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    arguments = sys.argv[1:]
    if "--inventory" in arguments:
        position = arguments.index("--inventory")
        report = str(Path(arguments[position + 1]).resolve())
        arguments = arguments[:position] + arguments[position + 2:]
        command = [sys.executable, str(root / "scripts/capture_pytest.py"), report, *arguments]
    else:
        targets = arguments or ["tests"]
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
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
        return subprocess.run(
            command,
            cwd=root, env=env, check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
