"""Lease-owned canonical knowledge preparation and nonmutating inspection.

Original bundles are never opened by SQLite here. Complete, fingerprint-checked
copies are recovered privately; restore-ready backups and publication candidates
are independently validated before any original is retired. Recovery journals
describe exact operations, rather than asking a restart to repeat selection.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..persistence import RuntimeLease, RuntimeLeaseInspection
from .knowledge import KnowledgeStore

SUFFIXES = ("", "-wal", "-shm", "-journal")
COLUMNS = ("id", "topic", "summary", "details", "source_agent", "project", "tags", "confidence", "created_at", "updated_at", "supersedes", "source_run", "source_artifact", "source_artifacts")
INDEXES = {"idx_topic": "topic", "idx_project": "project", "idx_source_agent": "source_agent", "idx_created_at": "created_at", "idx_supersedes": "supersedes"}
SCHEMA = """CREATE TABLE knowledge_records (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, summary TEXT NOT NULL,
 details TEXT NOT NULL, source_agent TEXT NOT NULL, project TEXT,
 tags TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, supersedes TEXT, source_run TEXT, source_artifact TEXT,
 source_artifacts TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY (supersedes) REFERENCES knowledge_records(id))"""
PHASES = ("PREPARED", "RETIRING", "PUBLISHED", "COMPLETED")
# Hooks cover both sides of every actual durable boundary. Context identifies
# each source/suffix/phase; the acceptance inventory must retain those instances.
FAULT_SITES = tuple(f"{operation}.{edge}" for operation in (
    "archive.mkdir", "bundle.copy", "snapshot.backup", "stage.prepare",
    "file.sync", "directory.sync", "journal.prepared", "journal.retiring",
    "journal.intent", "retirement.move", "journal.result", "publication.intent",
    "journal.rename",
    "publication.rename", "publication.retained", "journal.published",
    "journal.completed", "manifest.write", "pending.unlink",
) for edge in ("before", "after"))


@dataclass
class KnowledgeResult:
    status: str
    reason: str | None = None
    phase: str | None = None
    migration_id: str | None = None
    archive_path: str | None = None
    journal_path: str | None = None
    warnings: tuple[str, ...] = ()
    candidate_paths: dict[str, str] = field(default_factory=dict)
    history_count: int | None = None
    history_digest: str | None = None
    recovered: bool = False
    upgraded: bool = False
    remediation: str = "Stop all writers, preserve complete state and recovery evidence, and inspect private snapshots before repairing offline."
    state_dir: str | None = None
    selected_source: str | None = None
    verification: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedKnowledge:
    store: KnowledgeStore
    result: KnowledgeResult


@dataclass
class KnowledgeInspection:
    result: KnowledgeResult
    lease_status: str
    owner: dict[str, Any] | None = None


class KnowledgePreparationError(RuntimeError):
    def __init__(self, result: KnowledgeResult):
        self.result = result
        super().__init__(f"Knowledge preparation {result.status}: {result.reason or result.status}")


class _Problem(Exception):
    def __init__(self, status: str, reason: str | None = None):
        self.status, self.reason = status, reason or status


class _Budget:
    def __init__(self, deadline: float | None):
        now = time.monotonic()
        self.deadline = min(now + 30, deadline if deadline is not None else now + 30)

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Problem("migration_failed", "deadline_exceeded")

    def remaining(self) -> float:
        self.check()
        return max(0.001, self.deadline - time.monotonic())


class _Operation:
    def __init__(self, root: Path, deadline: float | None, hook: Callable[..., None] | None):
        self.root = root
        self.budget = _Budget(deadline)
        self.hook = hook
        self.journal: dict[str, Any] | None = None
        self.history_verified = False
        self.archive_path: Path | None = None
        self.migration_id: str | None = None
        self.durable_phase: str | None = None
        self.write_guard: Callable[[], None] | None = None

    def hit(self, site: str, **context: Any) -> None:
        self.budget.check()
        if self.write_guard:
            self.write_guard()
        if self.hook:
            self.hook(site, context)
        self.budget.check()

    def sync_directory(self, directory: Path) -> None:
        self.hit("directory.sync.before", path=str(directory))
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.hit("directory.sync.after", path=str(directory))

    def sync_file(self, path: Path) -> None:
        self.hit("file.sync.before", path=str(path))
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        self.hit("file.sync.after", path=str(path))

    def write_json(self, path: Path, value: dict[str, Any], site: str) -> None:
        self.hit(site + ".before", phase=value.get("phase"))
        _safe_path(self.root, path)
        fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                self.budget.check()
                self.sync_file(Path(name))
            self.budget.check()
            self.hit("journal.rename.before", phase=value.get("phase"))
            os.replace(name, path)
            self.hit("journal.rename.after", phase=value.get("phase"))
            self.sync_directory(path.parent)
            if path == _pending_path(self.root):
                self.durable_phase = value.get("phase")
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.hit(site + ".after", phase=value.get("phase"))


def _paths(root: Path) -> dict[str, Path]:
    return {"legacy": root / "knowledge.db", "canonical": root / "knowledge" / "knowledge.db"}


def _result(root: Path, status: str, **values: Any) -> KnowledgeResult:
    return KnowledgeResult(status, state_dir=str(root), candidate_paths={k: str(v) for k, v in _paths(root).items()}, **values)


def _io_reason(error: OSError) -> str:
    if isinstance(error, PermissionError) or error.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    if error.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return "disk_full"
    return "io_error"


def _safe_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _Problem("unsafe_path", "escaping_path") from None
    current = root
    for part in relative.parts:
        if part in (".", ".."):
            raise _Problem("unsafe_path", "escaping_path")
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise _Problem("unsafe_path", "nonregular_or_symlink")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise _Problem("unsafe_path", "hardlink")


def _fingerprint(path: Path, op: _Operation) -> dict[str, Any]:
    _safe_path(op.root, path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _Problem("unsafe_path", "nonregular_file")
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _Problem("migration_failed", "changed_source")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                op.budget.check()
                digest.update(chunk)
    finally:
        os.close(fd)
    after = path.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    if identity(before) != identity(after):
        raise _Problem("migration_failed", "changed_source")
    return {"sha256": digest.hexdigest(), "size": after.st_size, "mode": stat.S_IMODE(after.st_mode), "device": after.st_dev, "inode": after.st_ino, "mtime_ns": after.st_mtime_ns}


def _bundle(path: Path, op: _Operation) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for suffix in SUFFIXES:
        item = Path(str(path) + suffix)
        _safe_path(op.root, item)
        if item.exists():
            found[suffix] = _fingerprint(item, op)
    if found and "" not in found:
        raise _Problem("orphan_sidecar")
    return found


def _copy_file(source: Path, destination: Path, op: _Operation) -> None:
    op.hit("bundle.copy.before", source=str(source), destination=str(destination))
    mode = stat.S_IMODE(source.stat().st_mode) & 0o600
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with source.open("rb") as reader, os.fdopen(fd, "wb", closefd=False) as writer:
            while chunk := reader.read(1024 * 1024):
                op.budget.check()
                writer.write(chunk)
            writer.flush()
    finally:
        os.close(fd)
    op.sync_file(destination)
    op.hit("bundle.copy.after", source=str(source), destination=str(destination))


def _connect_private(path: Path, op: _Operation) -> sqlite3.Connection:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    connection = sqlite3.connect(path, timeout=min(0.1, op.budget.remaining()))
    connection.set_progress_handler(lambda: int(time.monotonic() >= op.budget.deadline), 100)
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _schema_reference(old: bool) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(SCHEMA.replace(" source_artifacts TEXT NOT NULL DEFAULT '[]',", "") if old else SCHEMA)
        return connection.execute("PRAGMA table_info(knowledge_records)").fetchall()
    finally:
        connection.close()


def _validate(path: Path, op: _Operation) -> dict[str, Any]:
    op.budget.check()
    if path.stat().st_size == 0:
        raise _Problem("invalid", "zero_byte_database")
    try:
        connection = _connect_private(path, op)
        try:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise _Problem("invalid", "integrity_check_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise _Problem("invalid", "foreign_key_check_failed")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                raise _Problem("unsupported_schema", "unknown_schema_version")
            columns = connection.execute("PRAGMA table_info(knowledge_records)").fetchall()
            old = len(columns) == 13
            extended = connection.execute("PRAGMA table_xinfo(knowledge_records)").fetchall()
            if columns != _schema_reference(old) or len(extended) != len(columns) or any(row[-1] != 0 for row in extended):
                raise _Problem("unsupported_schema", "unrecognized_columns")
            foreign_keys = connection.execute("PRAGMA foreign_key_list(knowledge_records)").fetchall()
            if foreign_keys != [(0, 0, "knowledge_records", "supersedes", "id", "NO ACTION", "NO ACTION", "NONE")]:
                raise _Problem("unsupported_schema", "unrecognized_constraints")
            objects = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master").fetchall()
            for kind, name, table, sql in objects:
                if kind == "table" and name == "knowledge_records":
                    # PRAGMAs above establish columns, defaults, keys and foreign
                    # references. Reject constraints not represented there while
                    # allowing inline REFERENCES and equivalent SQL formatting.
                    normalized = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
                    normalized = re.sub(r"'([^']|'')*'", "''", normalized)
                    if re.search(r"\b(CHECK|UNIQUE|COLLATE|DEFERRABLE|CONFLICT|STRICT|WITHOUT|GENERATED)\b", normalized, flags=re.I):
                        raise _Problem("unsupported_schema", "unrecognized_constraints")
                elif kind == "index" and name == "sqlite_autoindex_knowledge_records_1" and table == "knowledge_records" and sql is None:
                    if connection.execute(f"PRAGMA index_info('{name}')").fetchall() != [(0, 0, "id")]:
                        raise _Problem("unsupported_schema", "unrecognized_primary_key")
                    key = [row for row in connection.execute(f"PRAGMA index_xinfo('{name}')") if row[-1]]
                    if key != [(0, 0, "id", 0, "BINARY", 1)]:
                        raise _Problem("unsupported_schema", "unrecognized_primary_key")
                elif kind == "index" and name in INDEXES and table == "knowledge_records":
                    expected_column = INDEXES[name]
                    if connection.execute(f"PRAGMA index_info('{name}')").fetchall() != [(0, COLUMNS.index(expected_column), expected_column)]:
                        raise _Problem("unsupported_schema", "unrecognized_index")
                    row = next(item for item in connection.execute("PRAGMA index_list(knowledge_records)") if item[1] == name)
                    if row[2:] != (0, "c", 0):
                        raise _Problem("unsupported_schema", "unrecognized_index")
                    index_sql = re.sub(r"[\s\"`\[\]]", "", sql).lower()
                    if index_sql != f"createindex{name}onknowledge_records({expected_column})":
                        raise _Problem("unsupported_schema", "unrecognized_index")
                elif kind == "table" and name in ("sqlite_stat1", "sqlite_stat4") and table == name:
                    expected = ["tbl", "idx", "stat"] if name == "sqlite_stat1" else ["tbl", "idx", "neq", "nlt", "ndlt", "sample"]
                    if [item[1] for item in connection.execute(f"PRAGMA table_info({name})")] != expected:
                        raise _Problem("unsupported_schema", "unrecognized_statistics")
                else:
                    raise _Problem("unsupported_schema", "unknown_schema_object")
            select = ",".join(COLUMNS[:-1]) + (",'[]'" if old else ",source_artifacts")
            types = ",".join(f"typeof({name})" for name in COLUMNS[:-1]) + (",'text'" if old else ",typeof(source_artifacts)")
            digest = hashlib.sha256()
            count = 0
            seen: set[str] = set()
            for row in connection.execute(f"SELECT {select},{types} FROM knowledge_records ORDER BY id COLLATE BINARY"):
                op.budget.check()
                values, sql_types = row[:14], row[14:]
                required_text = (0, 1, 2, 3, 4, 6, 8, 9, 13)
                if any(not isinstance(values[index], str) for index in required_text) or not values[0] or values[0] in seen:
                    raise _Problem("invalid", "invalid_record_values")
                seen.add(values[0])
                if any(values[index] is not None and not isinstance(values[index], str) for index in (5, 10, 11, 12)) or not isinstance(values[7], (float, int)):
                    raise _Problem("invalid", "invalid_record_types")
                for index in (6, 13):
                    try:
                        decoded = json.loads(values[index])
                    except (ValueError, TypeError):
                        raise _Problem("invalid", "invalid_json") from None
                    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
                        raise _Problem("invalid", "invalid_json_type")
                try:
                    datetime.fromisoformat(values[8]); datetime.fromisoformat(values[9])
                except ValueError:
                    raise _Problem("invalid", "invalid_timestamp") from None
                encoded = json.dumps(list(zip(sql_types, values)), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big")); digest.update(encoded)
                count += 1
            return {"schema": 13 if old else 14, "count": count, "digest": digest.hexdigest()}
        finally:
            connection.close()
    except sqlite3.Error:
        op.budget.check()
        raise _Problem("invalid", "sqlite_validation_failed") from None


def _private_bundle(source: Path, bundle: dict[str, Any], destination: Path, op: _Operation) -> dict[str, Any]:
    _check_sqlite_contention(source, bundle)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for suffix in bundle:
        _copy_file(Path(str(source) + suffix), Path(str(destination) + suffix), op)
    if _bundle(source, op) != bundle:
        raise _Problem("migration_failed", "changed_source")
    return _validate(destination, op)


def _check_sqlite_contention(source: Path, bundle: dict[str, Any]) -> None:
    """Nonwriting SQLite byte-lock probes; arbitrary outsiders remain unsupported.

    SQLite's rollback pending/reserved region and WAL write-lock bytes use
    exclusive POSIX locks. A shared nonblocking probe detects active writers
    without opening SQLite or touching source journals. Release immediately;
    coherence additionally depends on fingerprints and stopped legacy writers.
    """
    try:
        import fcntl
    except ImportError:
        raise _Problem("migration_failed", "safe_locking_unavailable") from None
    probes = [(source, 0x40000000, 512)]
    if "-shm" in bundle:
        probes.append((Path(str(source) + "-shm"), 120, 3))
    for path, start, length in probes:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, length, start)
            except BlockingIOError:
                raise _Problem("migration_failed", "sqlite_contention") from None
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, length, start)
        finally:
            os.close(fd)


def _backup(private: Path, snapshot: Path, op: _Operation) -> dict[str, Any]:
    op.hit("snapshot.backup.before", path=str(snapshot))
    source = _connect_private(private, op)
    destination = _connect_private(snapshot, op)
    try:
        source.backup(destination, pages=64, progress=lambda *_: op.budget.check(), sleep=0.001)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    finally:
        destination.close(); source.close()
    os.chmod(snapshot, stat.S_IMODE(private.stat().st_mode) & 0o600)
    op.sync_file(snapshot)
    op.sync_directory(snapshot.parent)
    result = _validate(snapshot, op)
    op.hit("snapshot.backup.after", path=str(snapshot))
    return result


def _evidence(root: Path, op: _Operation) -> tuple[bool, tuple[str, ...]]:
    backups = root / "knowledge" / "backups"
    _safe_path(root, backups)
    prior = False
    warnings: list[str] = []
    if backups.exists():
        for entry in backups.iterdir():
            op.budget.check(); _safe_path(root, entry)
            if entry.is_file() or (entry.is_dir() and any(entry.iterdir())):
                prior = True
                try:
                    manifest = _load_journal(entry / "manifest.json", op)
                    if manifest["phase"] != "COMPLETED":
                        raise _Problem("recovery_required")
                    for snapshot in manifest["snapshots"].values():
                        if not _matches(root / snapshot["path"], snapshot["fingerprint"], op):
                            raise _Problem("recovery_required")
                    for move in manifest["moves"]:
                        fingerprint = manifest["sources"][move["name"]][move["suffix"]]
                        if not _matches(root / move["destination"], fingerprint, op):
                            raise _Problem("recovery_required")
                except (OSError, _Problem):
                    warnings.append("historical_audit_unverified")
    return prior, tuple(sorted(set(warnings)))


def _selection(root: Path, op: _Operation, temporary: Path) -> tuple[str | None, dict[str, Any], dict[str, Any], tuple[str, ...]]:
    op.budget.check()
    paths = _paths(root)
    bundles = {name: _bundle(path, op) for name, path in paths.items()}
    prior, warnings = _evidence(root, op)
    if not bundles["canonical"] and prior:
        raise _Problem("recovery_required", "evidence_unverified" if warnings else "prior_canonical_missing")
    histories: dict[str, Any] = {}
    for name, bundle in bundles.items():
        if bundle:
            histories[name] = _private_bundle(paths[name], bundle, temporary / name / "knowledge.db", op)
    if not histories:
        return None, bundles, histories, warnings
    if "canonical" not in histories:
        return "legacy", bundles, histories, warnings
    if "legacy" not in histories:
        return "canonical", bundles, histories, warnings
    legacy, canonical = histories["legacy"], histories["canonical"]
    if legacy["count"] and canonical["count"] and legacy["digest"] != canonical["digest"]:
        raise _Problem("ambiguous", "divergent_histories")
    return ("legacy" if legacy["count"] and not canonical["count"] else "canonical"), bundles, histories, warnings


def _pending_path(root: Path) -> Path:
    return root / "knowledge" / "migration-pending.json"


def _load_journal(path: Path, op: _Operation) -> dict[str, Any]:
    _safe_path(op.root, path)
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {"version", "id", "created_at", "phase", "selected", "retained", "sources", "snapshots", "candidate", "candidate_fingerprint", "history", "moves", "publication_intent", "status", "upgraded"}
        if not isinstance(value, dict) or set(value) != expected or value["version"] != 1 or not re.fullmatch(r"[0-9a-f]{32}", value["id"]):
            raise ValueError()
        if value["phase"] not in PHASES or value["selected"] not in (None, "legacy", "canonical") or value["status"] not in ("created", "canonical", "migrated", "reconciled"):
            raise ValueError()
        if type(value["retained"]) is not bool or type(value["publication_intent"]) is not bool or type(value["upgraded"]) is not bool:
            raise ValueError()
        archive = op.root / "knowledge" / "backups" / value["id"]
        if value["candidate"] != str((archive / "candidate.db").relative_to(op.root)):
            raise ValueError()
        if not isinstance(value["sources"], dict) or set(value["sources"]) - {"legacy", "canonical"}:
            raise ValueError()
        if not isinstance(value["snapshots"], dict) or set(value["snapshots"]) != set(value["sources"]):
            raise ValueError()
        if not isinstance(value["moves"], list):
            raise ValueError()
        def fingerprint_valid(item: Any) -> bool:
            return isinstance(item, dict) and set(item) == {"sha256", "size", "mode", "device", "inode", "mtime_ns"} and isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None and all(type(item[key]) is int and item[key] >= 0 for key in ("size", "mode", "device", "inode", "mtime_ns"))
        def history_valid(item: Any) -> bool:
            return isinstance(item, dict) and set(item) == {"schema", "count", "digest"} and item["schema"] in (13, 14) and type(item["count"]) is int and item["count"] >= 0 and isinstance(item["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", item["digest"]) is not None
        if not fingerprint_valid(value["candidate_fingerprint"]) or not history_valid(value["history"]) or value["history"]["schema"] != 14:
            raise ValueError()
        datetime.fromisoformat(value["created_at"])
        expected_moves = []
        for name in ("legacy", "canonical"):
            if name not in value["sources"]:
                continue
            bundle = value["sources"][name]
            if not isinstance(bundle, dict) or "" not in bundle or set(bundle) - set(SUFFIXES):
                raise ValueError()
            if not all(fingerprint_valid(item) for item in bundle.values()):
                raise ValueError()
            entry = "legacy-root" if name == "legacy" else "canonical-nested"
            snapshot = archive / entry / "snapshot.db"
            if set(value["snapshots"][name]) != {"path", "fingerprint", "history"} or value["snapshots"][name]["path"] != str(snapshot.relative_to(op.root)):
                raise ValueError()
            if not fingerprint_valid(value["snapshots"][name]["fingerprint"]) or not history_valid(value["snapshots"][name]["history"]):
                raise ValueError()
            if name == "canonical" and value["retained"]:
                continue
            for suffix in SUFFIXES:
                if suffix not in bundle:
                    continue
                expected_moves.append((name, suffix, str(Path(str(_paths(op.root)[name]) + suffix).relative_to(op.root)), str((archive / entry / ("original.db" + suffix)).relative_to(op.root))))
        if len(expected_moves) != len(value["moves"]):
            raise ValueError()
        for move, expected_move in zip(value["moves"], expected_moves):
            if set(move) != {"name", "suffix", "source", "destination", "intent", "done"} or tuple(move[k] for k in ("name", "suffix", "source", "destination")) != expected_move or type(move["intent"]) is not bool or type(move["done"]) is not bool or (move["done"] and not move["intent"]):
                raise ValueError()
        for relative in [value["candidate"], *(item["path"] for item in value["snapshots"].values()), *(move[k] for move in value["moves"] for k in ("source", "destination"))]:
            _safe_path(op.root, op.root / relative)
        if value["retained"] and (value["selected"] != "canonical" or value["upgraded"] or "canonical" not in value["sources"]):
            raise ValueError()
        if value["selected"] is None:
            if value["sources"] or value["history"]["count"] or value["status"] != "created":
                raise ValueError()
        else:
            selected_history = value["snapshots"][value["selected"]]["history"]
            if (selected_history["count"], selected_history["digest"]) != (value["history"]["count"], value["history"]["digest"]) or value["upgraded"] != (selected_history["schema"] == 13):
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _Problem("recovery_required", "malformed_journal") from None


def _matches(path: Path, fingerprint: dict[str, Any], op: _Operation) -> bool:
    return path.exists() and _fingerprint(path, op) == fingerprint


def _verify_recovery(journal: dict[str, Any], op: _Operation) -> None:
    _verify_recovery_layout(journal, op)
    for snapshot in journal["snapshots"].values():
        path = op.root / snapshot["path"]
        if not _matches(path, snapshot["fingerprint"], op) or _validate(path, op) != snapshot["history"]:
            raise _Problem("recovery_required", "snapshot_unverified")


def _resume(journal: dict[str, Any], op: _Operation, *, recovered: bool) -> KnowledgeResult:
    op.journal = journal
    op.durable_phase = journal["phase"]
    pending = _pending_path(op.root)
    archive = op.root / "knowledge" / "backups" / journal["id"]
    _verify_recovery(journal, op)
    op.history_verified = True
    if journal["phase"] == "PREPARED":
        journal["phase"] = "RETIRING"
        op.write_json(pending, journal, "journal.retiring")
    for move in journal["moves"]:
        source, destination = (op.root / move[k] for k in ("source", "destination"))
        if move["done"]:
            continue
        if not move["intent"]:
            move["intent"] = True
            op.write_json(pending, journal, "journal.intent")
        if source.exists():
            fingerprint = journal["sources"][move["name"]][move["suffix"]]
            if not _matches(source, fingerprint, op) or destination.exists():
                raise _Problem("recovery_required", "changed_source")
            op.hit("retirement.move.before", name=move["name"], suffix=move["suffix"])
            os.replace(source, destination)
            op.sync_directory(source.parent); op.sync_directory(destination.parent)
            op.hit("retirement.move.after", name=move["name"], suffix=move["suffix"])
        move["done"] = True
        op.write_json(pending, journal, "journal.result")
    canonical = _paths(op.root)["canonical"]
    if journal["phase"] in ("PREPARED", "RETIRING"):
        if not journal["publication_intent"]:
            journal["publication_intent"] = True
            op.write_json(pending, journal, "publication.intent")
        if journal["retained"]:
            op.hit("publication.retained.before")
            if _bundle(canonical, op) != journal["sources"]["canonical"]:
                raise _Problem("recovery_required", "retained_winner_changed")
            op.hit("publication.retained.after")
        else:
            candidate = op.root / journal["candidate"]
            if candidate.exists():
                if _bundle(canonical, op):
                    raise _Problem("recovery_required", "unexpected_destination_bundle")
                op.hit("publication.rename.before")
                os.replace(candidate, canonical)
                op.sync_directory(candidate.parent); op.sync_directory(canonical.parent)
                op.hit("publication.rename.after")
            elif not _matches(canonical, journal["candidate_fingerprint"], op):
                raise _Problem("recovery_required", "publication_unverified")
        journal["phase"] = "PUBLISHED"
        op.write_json(pending, journal, "journal.published")
    if journal["phase"] == "PUBLISHED":
        journal["phase"] = "COMPLETED"
        op.write_json(pending, journal, "journal.completed")
    op.write_json(archive / "manifest.json", journal, "manifest.write")
    op.hit("pending.unlink.before")
    pending.unlink()
    op.sync_directory(pending.parent)
    op.hit("pending.unlink.after")
    return _result(op.root, journal["status"], phase="COMPLETED", migration_id=journal["id"], archive_path=str(archive), history_count=journal["history"]["count"], history_digest=journal["history"]["digest"], recovered=recovered, upgraded=journal["upgraded"], selected_source=journal["selected"], verification="passed")


def _prepare(root: Path, op: _Operation) -> KnowledgeResult:
    pending = _pending_path(root)
    _safe_path(root, pending)
    knowledge = root / "knowledge"
    if knowledge.exists() and len(list(knowledge.glob("*pending*.json"))) > (1 if pending.exists() else 0):
        raise _Problem("recovery_required", "multiple_or_unknown_pending_journals")
    if pending.exists():
        return _resume(_load_journal(pending, op), op, recovered=True)
    with tempfile.TemporaryDirectory(prefix="agent-team-knowledge-") as temp:
        selected, bundles, histories, warnings = _selection(root, op, Path(temp))
        if selected == "canonical" and not bundles["legacy"] and histories["canonical"]["schema"] == 14:
            history = histories["canonical"]
            return _result(root, "canonical", warnings=warnings, history_count=history["count"], history_digest=history["digest"], selected_source=selected, verification="passed")
        migration_id = uuid.uuid4().hex
        archive = knowledge / "backups" / migration_id
        op.archive_path = archive
        op.migration_id = migration_id
        op.hit("archive.mkdir.before")
        _safe_path(root, archive)
        archive.mkdir(parents=True, mode=0o700)
        os.chmod(knowledge, 0o700)
        os.chmod(knowledge / "backups", 0o700)
        op.sync_directory(archive.parent); op.sync_directory(knowledge); op.sync_directory(root)
        op.hit("archive.mkdir.after")
        snapshots: dict[str, Any] = {}
        moves: list[dict[str, Any]] = []
        upgraded = selected is not None and histories[selected]["schema"] == 13
        retained = selected == "canonical" and not upgraded
        for name, bundle in bundles.items():
            if not bundle:
                continue
            entry = archive / ("legacy-root" if name == "legacy" else "canonical-nested")
            entry.mkdir(mode=0o700)
            private = Path(temp) / name / "knowledge.db"
            snapshot = entry / "snapshot.db"
            history = _backup(private, snapshot, op)
            if history != histories[name]:
                raise _Problem("migration_failed", "snapshot_history_changed")
            snapshots[name] = {"path": str(snapshot.relative_to(root)), "fingerprint": _fingerprint(snapshot, op), "history": history}
            if name == "canonical" and retained:
                continue
            for suffix in bundle:
                moves.append({"name": name, "suffix": suffix, "source": str(Path(str(_paths(root)[name]) + suffix).relative_to(root)), "destination": str((entry / ("original.db" + suffix)).relative_to(root)), "intent": False, "done": False})
        candidate = archive / "candidate.db"
        op.hit("stage.prepare.before")
        if selected is None:
            connection = _connect_private(candidate, op)
            try:
                connection.execute(SCHEMA); connection.commit()
            finally:
                connection.close()
        else:
            _copy_file(root / snapshots[selected]["path"], candidate, op)
        connection = _connect_private(candidate, op)
        try:
            if upgraded:
                connection.execute("ALTER TABLE knowledge_records ADD COLUMN source_artifacts TEXT NOT NULL DEFAULT '[]'")
            for name, column in INDEXES.items():
                connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON knowledge_records({column})")
            connection.commit()
        finally:
            connection.close()
        os.chmod(candidate, (stat.S_IMODE(candidate.stat().st_mode) & 0o600))
        history = _validate(candidate, op)
        if selected is not None and (history["count"], history["digest"]) != (histories[selected]["count"], histories[selected]["digest"]):
            raise _Problem("migration_failed", "upgrade_history_changed")
        op.sync_file(candidate); op.sync_directory(archive)
        op.hit("stage.prepare.after")
        for name, bundle in bundles.items():
            if _bundle(_paths(root)[name], op) != bundle:
                raise _Problem("migration_failed", "changed_source")
        journal = {"version": 1, "id": migration_id, "created_at": datetime.now(timezone.utc).isoformat(), "phase": "PREPARED", "selected": selected, "retained": retained, "sources": {name: bundle for name, bundle in bundles.items() if bundle}, "snapshots": snapshots, "candidate": str(candidate.relative_to(root)), "candidate_fingerprint": _fingerprint(candidate, op), "history": history, "moves": moves, "publication_intent": False, "status": "created" if selected is None else ("reconciled" if len(histories) == 2 else ("canonical" if selected == "canonical" else "migrated")), "upgraded": upgraded}
        op.journal = journal
        op.history_verified = True
        op.write_json(pending, journal, "journal.prepared")
        result = _resume(journal, op, recovered=False)
        result.warnings = warnings
        return result


def prepare_knowledge(state_dir: Path | str, *, lease: RuntimeLease, deadline: float | None = None, fault_hook: Callable[..., None] | None = None) -> PreparedKnowledge:
    root = Path(state_dir).expanduser().resolve()
    if not lease.is_held or lease.lock_path != root / "runtime.lock":
        raise KnowledgePreparationError(_result(root, "migration_failed", reason="runtime_lease_required"))
    generation = lease.generation
    lease.check_generation(generation)
    op = _Operation(root, deadline, fault_hook)
    op.write_guard = lambda: lease.check_generation(generation)
    try:
        result = _prepare(root, op)
        lease.check_generation(generation)
        op.budget.check()
        store = KnowledgeStore(_paths(root)["canonical"], initialize=False, write_guard=lambda: lease.check_generation(generation))
        return PreparedKnowledge(store, result)
    except (_Problem, OSError, sqlite3.Error) as error:
        status, reason = (error.status, error.reason) if isinstance(error, _Problem) else ("migration_failed", _io_reason(error) if isinstance(error, OSError) else "sqlite_error")
        journal = op.journal or {}
        history = journal.get("history", {}) if op.history_verified else {}
        raise KnowledgePreparationError(_result(root, status, reason=reason, phase=op.durable_phase, migration_id=journal.get("id") or op.migration_id, archive_path=str(root / "knowledge" / "backups" / journal["id"]) if journal.get("id") else (str(op.archive_path) if op.archive_path is not None else None), journal_path=str(_pending_path(root)) if _pending_path(root).exists() else None, selected_source=journal.get("selected"), history_count=history.get("count"), history_digest=history.get("digest"))) from None


def inspect_knowledge(state_dir: Path | str, *, deadline: float | None = None) -> KnowledgeInspection:
    root = Path(state_dir).expanduser().resolve()
    op = _Operation(root, deadline, None)
    with RuntimeLeaseInspection(root / "runtime.lock") as guard:
        if guard.status != "free":
            status = "inspection_deferred" if guard.status == "occupied" else "inspection_unavailable"
            return KnowledgeInspection(_result(root, status, reason="runtime_occupied" if guard.status == "occupied" else "existing_lock_unavailable"), guard.status, guard.owner)
        try:
            pending = _pending_path(root)
            _safe_path(root, pending)
            if (root / "knowledge").exists() and len(list((root / "knowledge").glob("*pending*.json"))) > (1 if pending.exists() else 0):
                raise _Problem("recovery_required", "multiple_or_unknown_pending_journals")
            if pending.exists():
                journal = _load_journal(pending, op)
                # Recovery verification uses SQLite only on private artifacts.
                with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temp:
                    for snapshot in journal["snapshots"].values():
                        source = root / snapshot["path"]
                        if not _matches(source, snapshot["fingerprint"], op):
                            raise _Problem("recovery_required", "snapshot_unverified")
                        copy = Path(temp) / (uuid.uuid4().hex + ".db")
                        _copy_file(source, copy, op)
                        if not _matches(source, snapshot["fingerprint"], op):
                            raise _Problem("inspection_unavailable", "changed_source")
                        if _validate(copy, op) != snapshot["history"]:
                            raise _Problem("recovery_required", "snapshot_unverified")
                    _verify_recovery_layout(journal, op)
                result = _result(root, "recovery_pending", phase=journal["phase"], migration_id=journal["id"], archive_path=str(root / "knowledge" / "backups" / journal["id"]), journal_path=str(pending), selected_source=journal["selected"], history_count=journal["history"]["count"], history_digest=journal["history"]["digest"], verification="passed")
            else:
                with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temp:
                    selected, bundles, histories, warnings = _selection(root, op, Path(temp))
                status = "needs_initialization" if selected is None else ("canonical" if selected == "canonical" and not bundles["legacy"] and histories["canonical"]["schema"] == 14 else "migration_needed")
                history = histories.get(selected, {})
                result = _result(root, status, warnings=warnings, history_count=history.get("count"), history_digest=history.get("digest"), selected_source=selected, verification="passed")
            if not guard.still_guarded:
                raise _Problem("inspection_unavailable", "existing_lock_changed")
            return KnowledgeInspection(result, "free", guard.owner)
        except (_Problem, OSError, sqlite3.Error) as error:
            status, reason = (error.status, error.reason) if isinstance(error, _Problem) else ("inspection_unavailable", _io_reason(error) if isinstance(error, OSError) else "coherent_copy_unavailable")
            if status == "migration_failed":
                status = "inspection_unavailable"
            return KnowledgeInspection(_result(root, status, reason=reason), "free", guard.owner)


def _verify_recovery_layout(journal: dict[str, Any], op: _Operation) -> None:
    """Doctor's layout verifier deliberately performs no source SQLite opens."""
    for move in journal["moves"]:
        source, destination = (op.root / move[k] for k in ("source", "destination"))
        fingerprint = journal["sources"][move["name"]][move["suffix"]]
        before = _matches(source, fingerprint, op) and not destination.exists()
        retired_source = not source.exists() or (move["name"] == "canonical" and move["suffix"] == "" and journal["publication_intent"] and _matches(source, journal["candidate_fingerprint"], op))
        after = retired_source and _matches(destination, fingerprint, op)
        if move["done"] and not after or not move["intent"] and not before or move["intent"] and not (before or after):
            raise _Problem("recovery_required", "retirement_unverified")
    for name, source in _paths(op.root).items():
        recorded = journal["sources"].get(name, {})
        for suffix in SUFFIXES:
            path = Path(str(source) + suffix)
            _safe_path(op.root, path)
            if suffix not in recorded and path.exists():
                # A published candidate is the sole legitimate new main file.
                if name != "canonical" or suffix or not journal["publication_intent"] or not _matches(path, journal["candidate_fingerprint"], op):
                    raise _Problem("recovery_required", "unexpected_source_companion")
    canonical = _paths(op.root)["canonical"]
    candidate = op.root / journal["candidate"]
    if journal["retained"]:
        if _bundle(canonical, op) != journal["sources"]["canonical"] or not _matches(candidate, journal["candidate_fingerprint"], op):
            raise _Problem("recovery_required", "retained_winner_changed")
    elif candidate.exists():
        if not _matches(candidate, journal["candidate_fingerprint"], op):
            raise _Problem("recovery_required", "candidate_unverified")
        canonical_move = next((move for move in journal["moves"] if move["name"] == "canonical" and move["suffix"] == ""), None)
        if canonical.exists() and (canonical_move is None or canonical_move["done"]):
            raise _Problem("recovery_required", "unexpected_canonical")
    elif not journal["publication_intent"] or not _matches(canonical, journal["candidate_fingerprint"], op) or any(Path(str(canonical) + suffix).exists() for suffix in SUFFIXES[1:]):
        raise _Problem("recovery_required", "publication_unverified")
