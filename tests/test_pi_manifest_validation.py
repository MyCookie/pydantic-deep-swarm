"""Fail-closed validation tests for the Pi asset manifest."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "pi"))
from manifest_validator import ManifestValidationError, validate_manifest  # noqa: E402


def test_bundled_manifest_is_integrity_complete():
    manifest = validate_manifest(ROOT / "pi" / "manifest.json")
    assert manifest["schema_version"] == 2
    assert manifest["minimum_pi_version"]
    assert manifest["provenance"]["generator"] == "agent-team"
    assert all(item["sha256"] and item["size_bytes"] >= 0 for item in manifest["assets"])


def write_fixture(tmp_path: Path, *, path: str = "asset.txt", content: str = "safe") -> Path:
    root = tmp_path / "pi"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "@fixture/pi", "version": "0.0.1"}), encoding="utf-8"
    )
    asset = root / path
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode()).hexdigest()
    manifest = {
        "schema_version": 2,
        "package": "@fixture/pi",
        "version": "0.0.1",
        "minimum_pi_version": "0.0.0",
        "managed_scope": "project-local",
        "provenance": {"generator": "test", "source": "fixture"},
        "external_requirements": [],
        "assets": [{
            "type": "prompt",
            "path": path,
            "sha256": digest,
            "size_bytes": len(content.encode()),
            "generator": "test",
            "provenance": "fixture",
            "minimum_pi_version": "0.0.0",
            "managed_scope": "project-local",
            "external_requirements": [],
        }],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_manifest_rejects_path_traversal(tmp_path):
    manifest_path = write_fixture(tmp_path, path="../outside.txt")
    with pytest.raises(ManifestValidationError, match="path"):
        validate_manifest(manifest_path)


def test_manifest_rejects_unsafe_symlink(tmp_path):
    manifest_path = write_fixture(tmp_path)
    root = manifest_path.parent
    target = root / "asset.txt"
    link = root / "link.txt"
    link.symlink_to(target)
    data = json.loads(manifest_path.read_text())
    data["assets"][0]["path"] = "link.txt"
    data["assets"][0]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    data["assets"][0]["size_bytes"] = target.stat().st_size
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ManifestValidationError, match="symlink"):
        validate_manifest(manifest_path)


def test_manifest_rejects_oversized_assets(tmp_path):
    manifest_path = write_fixture(tmp_path, content="12345")
    with pytest.raises(ManifestValidationError, match="size"):
        validate_manifest(manifest_path, max_bytes=4)


def runtime_secret_fixture() -> str:
    provider_prefix = "".join(("AK", "IA"))
    secret_value = provider_prefix + ("0" * 16)
    return "api_key = '" + secret_value + "'"


def test_manifest_rejects_embedded_secret(tmp_path):
    secret_text = runtime_secret_fixture()
    manifest_path = write_fixture(tmp_path, content=secret_text)
    data = json.loads(manifest_path.read_text())
    data["assets"][0]["sha256"] = hashlib.sha256(secret_text.encode()).hexdigest()
    data["assets"][0]["size_bytes"] = len(secret_text.encode())
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ManifestValidationError, match="secret"):
        validate_manifest(manifest_path)


def test_manifest_rejects_malformed_skill(tmp_path):
    manifest_path = write_fixture(tmp_path, path="skills/bad/SKILL.md", content="# no frontmatter")
    data = json.loads(manifest_path.read_text())
    data["assets"][0]["type"] = "skill"
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ManifestValidationError, match="frontmatter"):
        validate_manifest(manifest_path)


def test_manifest_rejects_hash_mismatch(tmp_path):
    manifest_path = write_fixture(tmp_path)
    data = json.loads(manifest_path.read_text())
    data["assets"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ManifestValidationError, match="SHA-256"):
        validate_manifest(manifest_path)
