"""Git provenance capture for Agent Team source, assets, and deployments.

The recorder is deliberately observational: it reads Git state and writes a
JSON record, but never stages, commits, tags, or pushes anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SCHEMA_VERSION = 1
WORKTREE_PREFIX = "worktree:"
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64}|worktree:[0-9a-f]{64})$")


class ProvenanceValidationError(ValueError):
    """Raised when provenance input or a Git repository cannot be captured safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratedBy(_StrictModel):
    """Non-secret identity of the agent or process that produced a change."""

    agent: str
    model: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    tool: str | None = None

    @field_validator("agent", "model", "session_id", "task_id", "tool")
    @classmethod
    def _non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("generated_by values must not be empty")
        return value


class ValidationResult(_StrictModel):
    """One reproducible validation observation included in a record."""

    name: str
    status: Literal["passed", "failed", "skipped"]
    command: list[str] = Field(default_factory=list)
    details: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("validation name must not be empty")
        return value

    @field_validator("command")
    @classmethod
    def _command_is_strings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("validation command entries must not be empty")
        return value


class AssetProvenance(_StrictModel):
    """Optional reference to a generated or validated repository asset."""

    path: str
    sha256: str | None = None
    generator: str | None = None
    source: str | None = None

    @field_validator("path", "generator", "source")
    @classmethod
    def _text_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("asset provenance text must not be empty")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256_format(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
            raise ValueError("asset sha256 must be a lowercase 64-character hex digest")
        return value


class RepositorySnapshot(_StrictModel):
    """Git state observed while a provenance record was captured."""

    root: str
    branch: str | None = None
    remote: str | None = None
    head_revision: str | None = None
    worktree_revision: str
    dirty: bool


class CommitPolicy(_StrictModel):
    """Policy metadata: recording provenance does not authorize commits."""

    mode: Literal["operator-authorized-only"] = "operator-authorized-only"
    auto_commit: Literal[False] = False


class PushPolicy(_StrictModel):
    """Fail-closed policy metadata for remote operations."""

    mode: Literal["disabled-unless-explicit-authorization"] = (
        "disabled-unless-explicit-authorization"
    )
    remote_push_enabled: Literal[False] = False
    auto_push: Literal[False] = False

    @model_validator(mode="after")
    def _remote_push_is_disabled(self) -> "PushPolicy":
        if self.remote_push_enabled or self.auto_push:
            raise ProvenanceValidationError(
                "remote push must remain disabled unless explicitly authorized outside the recorder"
            )
        return self


class ProvenanceRecord(_StrictModel):
    """A JSON-serializable, appendable provenance observation."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    record_id: str
    created_at: str
    project_id: str | None = None
    repository: RepositorySnapshot
    base_revision: str | None = None
    candidate_revision: str
    deployed_revision: str | None = None
    generated_by: GeneratedBy
    assets: list[AssetProvenance] = Field(default_factory=list)
    validation_results: list[ValidationResult] = Field(default_factory=list)
    commit_policy: CommitPolicy = Field(default_factory=CommitPolicy)
    push_policy: PushPolicy = Field(default_factory=PushPolicy)

    @field_validator("record_id")
    @classmethod
    def _record_id_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("record_id must not be empty")
        return value

    @field_validator("project_id")
    @classmethod
    def _project_id_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("project_id must not be empty")
        return value

    @field_validator("base_revision", "candidate_revision")
    @classmethod
    def _revision_format(cls, value: str | None) -> str | None:
        if value is not None and REVISION_RE.fullmatch(value) is None:
            raise ValueError(
                "revision must be a full hexadecimal commit ID or worktree:<sha256>"
            )
        return value

    @field_validator("deployed_revision")
    @classmethod
    def _deployed_revision_format(cls, value: str | None) -> str | None:
        if value is not None and COMMIT_RE.fullmatch(value) is None:
            raise ValueError("deployed revision must be a full committed revision")
        return value



def _git(root: Path, args: Sequence[str], *, required: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        if required:
            raise ProvenanceValidationError(f"unable to execute git: {exc}") from exc
        return None
    output = result.stdout.strip()
    if result.returncode != 0:
        if required:
            detail = result.stderr.strip() or output or f"exit {result.returncode}"
            raise ProvenanceValidationError(f"git {' '.join(args)} failed: {detail}")
        return None
    return output


def _repository_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    raw_root = _git(resolved, ["rev-parse", "--show-toplevel"])
    if raw_root is None:
        raise ProvenanceValidationError(f"not a Git repository: {resolved}")
    return Path(raw_root).resolve()


def _worktree_paths(root: Path) -> list[Path]:
    tracked_raw = _git(root, ["ls-files", "-z"], required=True) or ""
    untracked_raw = _git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        required=True,
    ) or ""
    names = {
        name for name in (tracked_raw + untracked_raw).split("\0") if name
    }
    return [root / name for name in sorted(names)]


def _worktree_fingerprint(root: Path) -> str:
    """Hash non-ignored worktree contents without changing the Git index."""
    digest = hashlib.sha256()
    for path in _worktree_paths(root):
        relative = path.relative_to(root).as_posix()
        digest.update(b"path\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(b"deleted\0")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"other\0")
            digest.update(str(metadata.st_mode).encode("ascii"))
        digest.update(b"\0")
    return WORKTREE_PREFIX + digest.hexdigest()


def capture_repository(path: Path | str) -> RepositorySnapshot:
    """Capture Git metadata without staging or changing repository state."""
    root = _repository_root(Path(path))
    branch = _git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch is None:
        branch = "(detached)" if _git(root, ["rev-parse", "--verify", "HEAD"]) else None
    remote = _git(root, ["remote", "get-url", "origin"])
    head = _git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all", "-z"], required=True)
    return RepositorySnapshot(
        root=str(root),
        branch=branch,
        remote=remote,
        head_revision=head,
        worktree_revision=_worktree_fingerprint(root),
        dirty=bool(status),
    )


def _coerce_generated_by(value: GeneratedBy | dict[str, Any]) -> GeneratedBy:
    if isinstance(value, GeneratedBy):
        return value
    try:
        return GeneratedBy.model_validate(value)
    except ValidationError as exc:
        raise ProvenanceValidationError(f"invalid generated_by metadata: {exc}") from exc


def _coerce_validation_results(
    values: Sequence[ValidationResult | dict[str, Any]],
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for value in values:
        try:
            results.append(
                value if isinstance(value, ValidationResult) else ValidationResult.model_validate(value)
            )
        except ValidationError as exc:
            raise ProvenanceValidationError(f"invalid validation result: {exc}") from exc
    return results


def _coerce_assets(values: Sequence[AssetProvenance | dict[str, Any]]) -> list[AssetProvenance]:
    assets: list[AssetProvenance] = []
    for value in values:
        try:
            assets.append(
                value if isinstance(value, AssetProvenance) else AssetProvenance.model_validate(value)
            )
        except ValidationError as exc:
            raise ProvenanceValidationError(f"invalid asset provenance: {exc}") from exc
    return assets


def _safe_asset_path(root: Path, raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ProvenanceValidationError("asset path must be a non-empty string")
    if "\x00" in raw_path or "\\" in raw_path:
        raise ProvenanceValidationError(f"unsafe asset path: {raw_path!r}")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProvenanceValidationError(f"asset path must stay inside repository: {raw_path!r}")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ProvenanceValidationError(f"asset path uses unsafe symlink: {raw_path!r}")
    candidate = root / relative
    if not candidate.is_file():
        raise ProvenanceValidationError(f"asset path is not a regular file: {raw_path!r}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ProvenanceValidationError(f"asset path escapes repository: {raw_path!r}")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_assets(
    root: Path,
    values: Sequence[AssetProvenance | dict[str, Any]],
    generated_by: GeneratedBy,
) -> list[AssetProvenance]:
    assets: list[AssetProvenance] = []
    for asset in _coerce_assets(values):
        path = _safe_asset_path(root, asset.path)
        digest = _file_sha256(path)
        if asset.sha256 is not None and asset.sha256 != digest:
            raise ProvenanceValidationError(
                f"SHA-256 mismatch for asset: {asset.path!r}"
            )
        assets.append(
            asset.model_copy(
                update={
                    "sha256": digest,
                    "generator": asset.generator or generated_by.agent,
                    "source": asset.source or "repository worktree",
                }
            )
        )
    return assets


def build_provenance_record(
    repository: Path | str,
    *,
    record_id: str,
    generated_by: GeneratedBy | dict[str, Any],
    project_id: str | None = None,
    base_revision: str | None = None,
    candidate_revision: str | None = None,
    deployed_revision: str | None = None,
    validation_results: Sequence[ValidationResult | dict[str, Any]] = (),
    assets: Sequence[AssetProvenance | dict[str, Any]] = (),
    push_remote_enabled: bool = False,
) -> ProvenanceRecord:
    """Build a provenance record from observed repository state.

    ``candidate_revision`` defaults to a content fingerprint rather than a
    fabricated commit when the worktree is uncommitted or the repository has
    no commits yet. ``deployed_revision`` is never inferred: deployment must be
    reported explicitly after verification.
    """
    if push_remote_enabled:
        raise ProvenanceValidationError(
            "remote push must remain disabled unless explicitly authorized outside the recorder"
        )
    snapshot = capture_repository(repository)
    generated = _coerce_generated_by(generated_by)
    materialized_assets = _materialize_assets(Path(snapshot.root), assets, generated)
    results = _coerce_validation_results(validation_results)
    default_candidate = (
        snapshot.head_revision
        if snapshot.head_revision is not None and not snapshot.dirty
        else snapshot.worktree_revision
    )
    try:
        return ProvenanceRecord(
            record_id=record_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            project_id=project_id,
            repository=snapshot,
            base_revision=snapshot.head_revision if base_revision is None else base_revision,
            candidate_revision=(
                default_candidate if candidate_revision is None else candidate_revision
            ),
            deployed_revision=deployed_revision,
            generated_by=generated,
            assets=materialized_assets,
            validation_results=results,
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ProvenanceValidationError):
            raise
        raise ProvenanceValidationError(f"invalid provenance record: {exc}") from exc


def write_provenance(path: Path | str, record: ProvenanceRecord) -> Path:
    """Atomically write one provenance record without touching Git state."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def load_provenance(path: Path | str) -> ProvenanceRecord:
    """Read and validate a provenance record from JSON."""
    source = Path(path).expanduser()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        return ProvenanceRecord.model_validate(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ProvenanceValidationError(f"invalid provenance file {source}: {exc}") from exc


def _validate_validation_evidence(record: ProvenanceRecord) -> None:
    """Require reproducible evidence for every declared validation outcome."""
    for result in record.validation_results:
        if result.status in {"passed", "failed"}:
            missing = [
                name
                for name, value in (
                    ("command", result.command),
                    ("details", result.details),
                    ("started_at", result.started_at),
                    ("finished_at", result.finished_at),
                )
                if not value
            ]
            if missing:
                raise ProvenanceValidationError(
                    f"validation evidence for {result.name!r} is missing: {', '.join(missing)}"
                )
            try:
                started = datetime.fromisoformat(str(result.started_at).replace("Z", "+00:00"))
                finished = datetime.fromisoformat(str(result.finished_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProvenanceValidationError(
                    f"validation evidence for {result.name!r} has an invalid timestamp"
                ) from exc
            if started.tzinfo is None or finished.tzinfo is None or finished < started:
                raise ProvenanceValidationError(
                    f"validation evidence for {result.name!r} has an invalid time range"
                )
        elif not (result.details or "").strip():
            raise ProvenanceValidationError(
                f"validation evidence for skipped check {result.name!r} requires details"
            )


def _validate_recorded_assets(root: Path, record: ProvenanceRecord) -> None:
    """Read every recorded asset and compare its current digest."""
    for asset in record.assets:
        if asset.sha256 is None:
            raise ProvenanceValidationError(
                f"recorded asset has no digest: {asset.path!r}"
            )
        path = _safe_asset_path(root, asset.path)
        if _file_sha256(path) != asset.sha256:
            raise ProvenanceValidationError(
                f"recorded asset digest does not match: {asset.path!r}"
            )


def _validate_recorded_assets_at_revision(
    root: Path,
    record: ProvenanceRecord,
    revision: str,
) -> None:
    """Compare recorded digests with blobs from an exact committed revision."""
    for asset in record.assets:
        if asset.sha256 is None:
            raise ProvenanceValidationError(
                f"recorded asset has no digest: {asset.path!r}"
            )
        raw_path = asset.path
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ProvenanceValidationError("asset path must be a non-empty string")
        if "\x00" in raw_path or "\\" in raw_path:
            raise ProvenanceValidationError(f"unsafe asset path: {raw_path!r}")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProvenanceValidationError(
                f"asset path must stay inside repository: {raw_path!r}"
            )
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:{relative.as_posix()}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ProvenanceValidationError(
                f"recorded asset is absent from candidate revision: {raw_path!r}"
            )
        if hashlib.sha256(result.stdout).hexdigest() != asset.sha256:
            raise ProvenanceValidationError(
                f"recorded candidate asset digest does not match: {raw_path!r}"
            )


def validate_provenance(
    path: Path | str,
    *,
    repository: Path | str | None = None,
    check_candidate: bool = False,
) -> ProvenanceRecord:
    """Load a record and optionally compare it with a live repository."""
    record = load_provenance(path)
    _validate_validation_evidence(record)
    if repository is None:
        return record

    snapshot = capture_repository(repository)
    if Path(snapshot.root).resolve() != Path(record.repository.root).resolve():
        raise ProvenanceValidationError(
            f"record repository root {record.repository.root!r} does not match {snapshot.root!r}"
        )
    root = Path(snapshot.root)
    if check_candidate:
        if record.candidate_revision.startswith(WORKTREE_PREFIX):
            _validate_recorded_assets(root, record)
            if record.candidate_revision != snapshot.worktree_revision:
                raise ProvenanceValidationError(
                    "candidate worktree revision does not match the current repository"
                )
        else:
            resolved = _git(
                root,
                ["rev-parse", "--verify", f"{record.candidate_revision}^{{commit}}"],
                required=True,
            )
            if resolved != record.candidate_revision:
                raise ProvenanceValidationError(
                    "candidate commit revision does not resolve exactly in the repository"
                )
            _validate_recorded_assets_at_revision(root, record, resolved)
    else:
        _validate_recorded_assets(root, record)
    return record


__all__ = [
    "AssetProvenance",
    "CommitPolicy",
    "GeneratedBy",
    "ProvenanceRecord",
    "ProvenanceValidationError",
    "PushPolicy",
    "RepositorySnapshot",
    "SCHEMA_VERSION",
    "ValidationResult",
    "build_provenance_record",
    "capture_repository",
    "load_provenance",
    "validate_provenance",
    "write_provenance",
]


if __name__ == "__main__":
    raise SystemExit("Use `agent-team provenance` or import agent_team.provenance")
