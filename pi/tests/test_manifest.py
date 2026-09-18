"""Structural checks for the first-party Pi package."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from manifest_validator import validate_manifest  # noqa: E402


def test_manifest_paths_are_safe_and_present():
    manifest = validate_manifest(ROOT / "manifest.json")
    assert manifest["schema_version"] == 2
    assert any(
        item["type"] == "license" and item["path"] == "LICENSE"
        for item in manifest["assets"]
    )
    for item in manifest["assets"]:
        relative = Path(item["path"])
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT.resolve())
        assert path.is_file()
        assert not path.is_symlink()


def test_package_declares_only_source_resources():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert "LICENSE" in package["files"]
    assert "manifest_validator.py" in package["files"]
    assert "tests/test_manifest.py" in package["files"]
    assert "tests" not in package["files"]
    assert "node_modules" not in package["files"]
    assert ".pi" not in package["files"]
