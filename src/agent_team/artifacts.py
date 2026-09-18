"""Runtime-owned artifact verification and manifests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .contracts import ArtifactResult, CompletionReport
from .redaction import redact_model, redact_sensitive_data


class ArtifactStore:
    """Verify worker artifact claims inside a bounded workspace.

    A model claim is never treated as proof. Local files must exist beneath the
    configured workspace; their size and SHA-256 are recorded in a per-project
    manifest using an atomic replace.
    """

    def __init__(self, workspace: Path | str, manifest_root: Path | str):
        self.workspace = Path(workspace).expanduser().resolve()
        self.manifest_root = Path(manifest_root).expanduser()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.manifest_root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError(f"Artifact path escapes workspace: {path}")
        return resolved

    @staticmethod
    def _digest(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def verify(self, artifact: ArtifactResult) -> ArtifactResult:
        artifact = redact_model(artifact)
        if artifact.artifact_type != "file":
            if artifact.artifact_type == "directory":
                try:
                    path = self.resolve(artifact.path)
                except ValueError as exc:
                    return artifact.model_copy(update={
                        "verified": False,
                        "verification_method": str(exc),
                    })
                verified = path.is_dir()
                return artifact.model_copy(update={
                    "verified": verified,
                    "verification_method": "directory exists" if verified else "directory missing",
                })
            return artifact.model_copy(update={
                "verified": False,
                "verification_method": "runtime cannot verify non-local artifact",
            })
        try:
            path = self.resolve(artifact.path)
        except ValueError as exc:
            return artifact.model_copy(update={
                "verified": False,
                "verification_method": str(exc),
            })
        if not path.is_file():
            return artifact.model_copy(update={
                "verified": False,
                "verification_method": "file missing",
            })
        digest, size = self._digest(path)
        if artifact.sha256 and artifact.sha256 != digest:
            return artifact.model_copy(update={
                "sha256": digest,
                "size_bytes": size,
                "verified": False,
                "verification_method": "sha256 mismatch",
            })
        return artifact.model_copy(update={
            "path": str(path.relative_to(self.workspace)),
            "sha256": digest,
            "size_bytes": size,
            "verified": True,
            "verification_method": "file exists and SHA-256 verified",
        })

    def verify_many(self, project_id: str, artifacts: Iterable[ArtifactResult]) -> list[ArtifactResult]:
        verified = [self.verify(item) for item in artifacts]
        self.write_manifest(project_id, verified)
        return verified

    def verify_report(self, project_id: str, report: CompletionReport) -> CompletionReport:
        return report.model_copy(update={
            "artifacts": self.verify_many(project_id, report.artifacts),
        })

    def write_manifest(self, project_id: str, artifacts: Iterable[ArtifactResult]) -> Path:
        directory = self.manifest_root / self._safe_component(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "artifacts.json"
        payload = {
            "project_id": project_id,
            "workspace": str(self.workspace),
            "artifacts": [item.model_dump() for item in artifacts],
        }
        payload = redact_sensitive_data(payload)
        fd, temporary = tempfile.mkstemp(prefix=".artifacts.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_.")
        return cleaned[:160] or "project"
