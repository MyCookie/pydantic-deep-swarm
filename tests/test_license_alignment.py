"""Publication licensing must stay aligned across package metadata and docs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).parents[1]
AGPL_V3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
MIT_TEXT = """MIT License

Copyright (c) 2026 Agent Team contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the \"Software\"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def test_agent_team_agpl_and_pi_mit_are_complete_and_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pi_package = json.loads((ROOT / "pi" / "package.json").read_text())
    pi_manifest = json.loads((ROOT / "pi" / "manifest.json").read_text())
    root_license = (ROOT / "LICENSE").read_bytes()

    assert hashlib.sha256(root_license).hexdigest() == AGPL_V3_SHA256
    assert project["project"]["license"] == "AGPL-3.0-only"
    assert project["project"]["license-files"] == ["LICENSE"]

    assert (ROOT / "pi" / "LICENSE").read_text() == MIT_TEXT
    assert pi_package["license"] == "MIT"
    assert "LICENSE" in pi_package["files"]
    assert any(asset["path"] == "LICENSE" for asset in pi_manifest["assets"])

    root_readme = (ROOT / "README.md").read_text()
    docs_readme = (ROOT / "docs" / "README.md").read_text()
    pi_readme = (ROOT / "pi" / "README.md").read_text()
    assert "[GNU AGPLv3](LICENSE)" in root_readme
    assert "[MIT License](pi/LICENSE)" in root_readme
    assert "[GNU AGPLv3](../LICENSE)" in docs_readme
    assert "[MIT License](../pi/LICENSE)" in docs_readme
    assert "[MIT License](LICENSE)" in pi_readme
