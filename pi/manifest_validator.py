"""Fail-closed validator for the first-party Pi asset manifest."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ALLOWED_TYPES = {"extension", "skill", "prompt", "theme", "agent", "metadata", "license", "test"}
ALLOWED_SCOPES = {"project-local", "user-global"}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|secret|password|token)\s*[:=]\s*['\"]?"
        r"(?!\$\{|\{\{|<[^>]+>|YOUR_|CHANGE_ME|EXAMPLE|PLACEHOLDER|NONE|NULL)"
        r"[A-Za-z0-9_\-/+=]{8,}"
    ),
)


class ManifestValidationError(ValueError):
    """Raised when a Pi manifest or managed asset is unsafe or malformed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestValidationError(message)


def _validate_semver(value: Any, field: str) -> str:
    _require(isinstance(value, str) and SEMVER_RE.fullmatch(value) is not None, f"invalid {field}")
    return value


def _validate_string_list(value: Any, field: str) -> list[str]:
    _require(isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value), f"invalid {field}")
    return value


def _safe_path(root: Path, raw_path: Any) -> Path:
    _require(isinstance(raw_path, str) and raw_path.strip(), "asset path must be a non-empty string")
    _require("\x00" not in raw_path and "\\" not in raw_path, f"unsafe asset path: {raw_path!r}")
    relative = Path(raw_path)
    _require(not relative.is_absolute(), f"asset path must be relative: {raw_path!r}")
    _require(".." not in relative.parts, f"asset path traversal: {raw_path!r}")

    candidate = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        _require(not current.is_symlink(), f"asset path uses unsafe symlink: {raw_path!r}")
    resolved = candidate.resolve(strict=False)
    _require(resolved.is_relative_to(root.resolve()), f"asset path escapes package: {raw_path!r}")
    _require(candidate.is_file(), f"asset does not exist as a regular file: {raw_path!r}")
    return candidate


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _reject_secrets(path: Path, text: str | None) -> None:
    if text is None:
        return
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ManifestValidationError(f"embedded secret pattern in asset: {path}")


def _validate_skill(path: Path, text: str | None) -> None:
    _require(text is not None, f"skill must be UTF-8 text: {path}")
    _require(text.startswith("---\n"), f"skill missing frontmatter: {path}")
    end = text.find("\n---", 4)
    _require(end >= 0, f"skill missing frontmatter terminator: {path}")
    frontmatter = text[4:end].splitlines()
    keys: dict[str, str] = {}
    for line in frontmatter:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        keys[key.strip()] = value.strip().strip("'\"")
    _require(keys.get("name", "").strip(), f"skill frontmatter missing name: {path}")
    _require(keys.get("description", "").strip(), f"skill frontmatter missing description: {path}")


def _validate_json_asset(path: Path, text: str | None) -> None:
    _require(text is not None, f"JSON asset must be UTF-8 text: {path}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"malformed JSON asset: {path}") from exc
    _require(isinstance(value, (dict, list)), f"JSON asset must be an object or array: {path}")


def _validate_asset(root: Path, asset: Any, max_bytes: int) -> None:
    _require(isinstance(asset, dict), "each manifest asset must be an object")
    required = {
        "type", "path", "sha256", "size_bytes", "generator", "provenance",
        "minimum_pi_version", "managed_scope", "external_requirements",
    }
    _require(required <= asset.keys(), f"asset missing required fields: {sorted(required - asset.keys())}")
    asset_type = asset["type"]
    _require(asset_type in ALLOWED_TYPES, f"unsupported asset type: {asset_type!r}")
    path = _safe_path(root, asset["path"])
    _require(isinstance(asset["generator"], str) and asset["generator"].strip(), f"invalid generator: {path}")
    _require(isinstance(asset["provenance"], str) and asset["provenance"].strip(), f"invalid provenance: {path}")
    _validate_semver(asset["minimum_pi_version"], f"minimum_pi_version for {path}")
    _require(asset["managed_scope"] in ALLOWED_SCOPES, f"invalid managed_scope for {path}")
    _validate_string_list(asset["external_requirements"], f"external_requirements for {path}")

    size = path.stat().st_size
    _require(isinstance(asset["size_bytes"], int) and asset["size_bytes"] >= 0, f"invalid size_bytes for {path}")
    _require(size == asset["size_bytes"], f"size mismatch for asset: {path}")
    _require(size <= max_bytes, f"asset exceeds size limit: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _require(isinstance(asset["sha256"], str) and SHA256_RE.fullmatch(asset["sha256"]) is not None, f"invalid SHA-256 for {path}")
    _require(digest == asset["sha256"], f"SHA-256 mismatch for asset: {path}")

    text = _read_text(path)
    _reject_secrets(path, text)
    if asset_type == "skill":
        _validate_skill(path, text)
    if asset_type in {"theme", "metadata"} or path.suffix.lower() == ".json":
        _validate_json_asset(path, text)


def validate_manifest(manifest_path: Path | str, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    """Validate and return a Pi manifest; raise on any unsafe condition."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    _require(manifest_path.is_file(), f"manifest does not exist: {manifest_path}")
    root = manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"malformed manifest: {manifest_path}") from exc
    _require(isinstance(manifest, dict), "manifest must be a JSON object")
    required = {
        "schema_version", "package", "version", "minimum_pi_version", "managed_scope",
        "provenance", "external_requirements", "assets",
    }
    _require(required <= manifest.keys(), f"manifest missing required fields: {sorted(required - manifest.keys())}")
    _require(manifest["schema_version"] == 2, "unsupported manifest schema_version")
    _require(isinstance(manifest["package"], str) and manifest["package"].strip(), "invalid package")
    _validate_semver(manifest["version"], "version")
    _validate_semver(manifest["minimum_pi_version"], "minimum_pi_version")
    _require(manifest["managed_scope"] in ALLOWED_SCOPES, "invalid manifest managed_scope")
    _require(isinstance(manifest["provenance"], dict), "invalid manifest provenance")
    _require(
        isinstance(manifest["provenance"].get("generator"), str)
        and manifest["provenance"]["generator"].strip(),
        "manifest provenance requires generator",
    )
    _validate_string_list(manifest["external_requirements"], "manifest external_requirements")
    _require(isinstance(manifest["assets"], list) and manifest["assets"], "manifest assets must be non-empty")

    paths: set[str] = set()
    for asset in manifest["assets"]:
        path_value = asset.get("path") if isinstance(asset, dict) else None
        _require(path_value not in paths, f"duplicate manifest asset path: {path_value!r}")
        paths.add(path_value)
        _validate_asset(root, asset, max_bytes)
    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--max-bytes", type=int, default=1_000_000)
    args = parser.parse_args()
    validate_manifest(args.manifest, max_bytes=args.max_bytes)
    print(f"valid Pi manifest: {args.manifest}")
