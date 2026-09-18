"""Acceptance checks for the bundled first-party Pi asset package."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "pi"


def test_pi_package_layout_and_manifest_are_complete():
    expected_dirs = {"extensions", "skills", "prompts", "themes", "agents", "tests"}
    assert expected_dirs <= {path.name for path in ROOT.iterdir() if path.is_dir()}

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "@agent-team/pi-assets"
    assert package["private"] is True
    assert set(package["pi"]) >= {"extensions", "skills", "prompts", "themes"}

    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["package"] == package["name"]
    assert manifest["managed_scope"] == "project-local"
    assert manifest["minimum_pi_version"]
    assert manifest["provenance"]["generator"] == "agent-team"
    for asset in manifest["assets"]:
        path = ROOT / asset["path"]
        assert path.exists(), asset["path"]
        assert asset["type"] in {
            "extension", "skill", "prompt", "theme", "agent", "metadata", "license", "test"
        }


def test_pi_sources_are_first_party_and_runtime_free():
    assert (ROOT / "extensions" / "agent-team-status.ts").is_file()
    assert (ROOT / "skills" / "agent-team-delegation" / "SKILL.md").is_file()
    assert (ROOT / "prompts" / "principal-handoff.md").is_file()
    assert (ROOT / "themes" / "agent-team.json").is_file()
    assert (ROOT / "agents" / "reviewer.md").is_file()
    assert not (ROOT / "node_modules").exists()
    assert not (ROOT / ".pi").exists()


def test_manifest_covers_each_generated_asset_kind():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assets_by_type = {asset["type"]: asset["path"] for asset in manifest["assets"]}

    assert {"extension", "skill", "prompt", "theme", "license"} <= assets_by_type.keys()
    assert assets_by_type["extension"] == "extensions/agent-team-status.ts"
    assert assets_by_type["skill"] == "skills/agent-team-delegation/SKILL.md"
    assert assets_by_type["prompt"] == "prompts/principal-handoff.md"
    assert assets_by_type["theme"] == "themes/agent-team.json"
    assert assets_by_type["license"] == "LICENSE"
