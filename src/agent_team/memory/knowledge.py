"""SQLite-based shared knowledge store."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from pydantic import BaseModel, Field

from ..redaction import redact_sensitive_data


T = TypeVar("T")


class KnowledgeRecord(BaseModel):
    """A single knowledge record."""

    id: str
    topic: str
    summary: str
    details: str

    source_agent: str
    project: str | None = None

    tags: list[str]
    confidence: float = 1.0

    created_at: datetime
    updated_at: datetime

    # ``supersedes`` is stored on the old record and points to its replacement.
    supersedes: str | None = None
    source_run: str | None = None
    source_artifact: str | None = None
    source_artifacts: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for SQLite storage."""
        artifact_refs = list(dict.fromkeys([
            *([self.source_artifact] if self.source_artifact else []),
            *self.source_artifacts,
        ]))
        return {
            "id": self.id,
            "topic": self.topic,
            "summary": self.summary,
            "details": self.details,
            "source_agent": self.source_agent,
            "project": self.project,
            "tags": json.dumps(self.tags),
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "supersedes": self.supersedes,
            "source_run": self.source_run,
            "source_artifact": self.source_artifact or (artifact_refs[0] if artifact_refs else None),
            "source_artifacts": json.dumps(artifact_refs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeRecord":
        """Create from dictionary without mutating the caller's mapping."""
        data = dict(data)
        if isinstance(data.get("tags"), str):
            data["tags"] = json.loads(data["tags"])
        if isinstance(data.get("source_artifacts"), str):
            data["source_artifacts"] = json.loads(data["source_artifacts"])
        data.setdefault("source_artifacts", [])
        if not data["source_artifacts"] and data.get("source_artifact"):
            data["source_artifacts"] = [data["source_artifact"]]
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if isinstance(data.get("updated_at"), str):
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return cls.model_validate(redact_sensitive_data(data))


class KnowledgeStore:
    """SQLite-backed shared knowledge store with process-safe writes.

    Writes use explicit transactions and retry transient SQLite lock errors.
    Normal searches exclude superseded records; historical records can be
    requested with ``include_superseded=True``.
    """

    DEFAULT_BUSY_TIMEOUT_MS = 30_000
    DEFAULT_MAX_RETRIES = 8

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1_000, int(busy_timeout_ms))
        self.max_retries = max(1, int(max_retries))
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a consistently configured connection.

        WAL mode is persistent database state and is enabled during schema
        initialization. Busy timeout and foreign-key enforcement are per
        connection, so they are applied here for every operation.
        """
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except BaseException:
            if connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _is_transient_lock(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return "database is locked" in message or "database is busy" in message

    def _with_retry(self, operation: Callable[[], T]) -> T:
        """Retry only transient SQLite lock failures with bounded backoff."""
        for attempt in range(self.max_retries):
            try:
                return operation()
            except sqlite3.OperationalError as error:
                if not self._is_transient_lock(error) or attempt == self.max_retries - 1:
                    raise
                time.sleep(min(0.25, 0.01 * (2 ** attempt)))
        raise AssertionError("unreachable")

    def _init_db(self) -> None:
        """Initialize or migrate the database schema atomically."""
        def initialize() -> None:
            with self._connect() as connection:
                # journal_mode must be set outside an explicit transaction.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_records (
                        id TEXT PRIMARY KEY,
                        topic TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        details TEXT NOT NULL,
                        source_agent TEXT NOT NULL,
                        project TEXT,
                        tags TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        supersedes TEXT,
                        source_run TEXT,
                        source_artifact TEXT,
                        source_artifacts TEXT NOT NULL DEFAULT '[]',
                        FOREIGN KEY (supersedes) REFERENCES knowledge_records(id)
                    )
                """)
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(knowledge_records)").fetchall()
                }
                if "source_artifacts" not in columns:
                    connection.execute(
                        "ALTER TABLE knowledge_records "
                        "ADD COLUMN source_artifacts TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_topic ON knowledge_records(topic)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_project ON knowledge_records(project)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_source_agent ON knowledge_records(source_agent)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_created_at ON knowledge_records(created_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_supersedes ON knowledge_records(supersedes)"
                )
                connection.commit()

        self._with_retry(initialize)

    @staticmethod
    def _prepared_record(record: KnowledgeRecord) -> KnowledgeRecord:
        """Copy and normalize a record without mutating the caller's model."""
        prepared = record.model_copy(deep=True)
        if not prepared.id:
            prepared.id = str(uuid.uuid4())
        prepared.updated_at = datetime.utcnow()
        prepared.source_artifacts = list(dict.fromkeys([
            *([prepared.source_artifact] if prepared.source_artifact else []),
            *prepared.source_artifacts,
        ]))
        if prepared.source_artifacts and not prepared.source_artifact:
            prepared.source_artifact = prepared.source_artifacts[0]
        return KnowledgeRecord.model_validate(redact_sensitive_data(prepared))

    @staticmethod
    def _record_values(record: KnowledgeRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.topic,
            record.summary,
            record.details,
            record.source_agent,
            record.project,
            json.dumps(record.tags),
            record.confidence,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.supersedes,
            record.source_run,
            record.source_artifact,
            json.dumps(record.source_artifacts),
        )

    def add(self, record: KnowledgeRecord) -> str:
        """Atomically insert a record, ignoring a duplicate ID.

        A duplicate ID is treated as an idempotent retry and never overwrites
        the first record that won the insert race.
        """
        prepared = self._prepared_record(record)

        def insert() -> str:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    INSERT INTO knowledge_records
                    (id, topic, summary, details, source_agent, project, tags, confidence,
                     created_at, updated_at, supersedes, source_run, source_artifact,
                     source_artifacts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                """, self._record_values(prepared))
                connection.commit()
            return prepared.id

        return self._with_retry(insert)

    def update(self, record: KnowledgeRecord) -> None:
        """Atomically update an existing record."""
        prepared = self._prepared_record(record)

        def replace() -> None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    UPDATE knowledge_records
                    SET topic=?, summary=?, details=?, source_agent=?, project=?,
                        tags=?, confidence=?, updated_at=?, supersedes=?,
                        source_run=?, source_artifact=?, source_artifacts=?
                    WHERE id=?
                """, (
                    prepared.topic,
                    prepared.summary,
                    prepared.details,
                    prepared.source_agent,
                    prepared.project,
                    json.dumps(prepared.tags),
                    prepared.confidence,
                    prepared.updated_at.isoformat(),
                    prepared.supersedes,
                    prepared.source_run,
                    prepared.source_artifact,
                    json.dumps(prepared.source_artifacts),
                    prepared.id,
                ))
                connection.commit()

        self._with_retry(replace)

    def supersede(self, old_id: str, new_id: str) -> bool:
        """Atomically point an existing record at its replacement.

        Returns ``True`` when the relationship exists after the call, including
        an identical retry. Missing records return ``False``; an already
        different replacement is rejected rather than silently overwritten.
        """
        if old_id == new_id:
            raise ValueError("A knowledge record cannot supersede itself")

        def replace() -> bool:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                old = connection.execute(
                    "SELECT supersedes FROM knowledge_records WHERE id=?",
                    (old_id,),
                ).fetchone()
                new = connection.execute(
                    "SELECT id FROM knowledge_records WHERE id=?",
                    (new_id,),
                ).fetchone()
                if old is None or new is None:
                    connection.rollback()
                    return False
                current = old[0]
                if current not in (None, new_id):
                    connection.rollback()
                    raise ValueError(
                        f"Knowledge record {old_id!r} already supersedes {current!r}"
                    )
                connection.execute(
                    "UPDATE knowledge_records SET supersedes=?, updated_at=? "
                    "WHERE id=? AND (supersedes IS NULL OR supersedes=?)",
                    (new_id, datetime.utcnow().isoformat(), old_id, new_id),
                )
                connection.commit()
                return True

        return self._with_retry(replace)

    def get(self, record_id: str) -> KnowledgeRecord | None:
        """Get a record by ID, including superseded records."""
        def fetch() -> KnowledgeRecord | None:
            with self._connect() as connection:
                cursor = connection.execute(
                    "SELECT * FROM knowledge_records WHERE id=?",
                    (record_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                columns = [description[0] for description in cursor.description]
                return KnowledgeRecord.from_dict(dict(zip(columns, row)))

        return self._with_retry(fetch)

    def search(
        self,
        topic: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        source_agent: str | None = None,
        limit: int = 50,
        include_superseded: bool = False,
    ) -> list[KnowledgeRecord]:
        """Search records using bound parameters.

        Superseded records are excluded unless explicitly requested. Tag
        predicates use SQLite's JSON table-valued function and are still fully
        parameterized.
        """
        bounded_limit = max(0, int(limit))
        if bounded_limit == 0:
            return []
        conditions: list[str] = []
        params: list[Any] = []

        if topic:
            conditions.append("topic LIKE ?")
            params.append(f"%{topic}%")
        if project:
            conditions.append("project = ?")
            params.append(project)
        if source_agent:
            conditions.append("source_agent = ?")
            params.append(source_agent)
        if tags:
            for tag in tags:
                conditions.append(
                    "EXISTS (SELECT 1 FROM json_each(knowledge_records.tags) "
                    "WHERE json_each.value = ?)"
                )
                params.append(str(tag))
        if not include_superseded:
            conditions.append("supersedes IS NULL")

        query = "SELECT * FROM knowledge_records"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(bounded_limit)

        def fetch() -> list[KnowledgeRecord]:
            with self._connect() as connection:
                cursor = connection.execute(query, params)
                columns = [description[0] for description in cursor.description]
                return [
                    KnowledgeRecord.from_dict(dict(zip(columns, row)))
                    for row in cursor.fetchall()
                ]

        return self._with_retry(fetch)

    def search_text(
        self,
        query: str,
        limit: int = 50,
        include_superseded: bool = False,
    ) -> list[KnowledgeRecord]:
        """Search topic, summary, and details with literal bound parameters."""
        bounded_limit = max(0, int(limit))
        if bounded_limit == 0:
            return []
        where = "(topic LIKE ? OR summary LIKE ? OR details LIKE ?)"
        if not include_superseded:
            where += " AND supersedes IS NULL"
        params: list[Any] = [f"%{query}%"] * 3
        params.append(bounded_limit)

        def fetch() -> list[KnowledgeRecord]:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"SELECT * FROM knowledge_records WHERE {where} "
                    "ORDER BY created_at DESC LIMIT ?",
                    params,
                )
                columns = [description[0] for description in cursor.description]
                return [
                    KnowledgeRecord.from_dict(dict(zip(columns, row)))
                    for row in cursor.fetchall()
                ]

        return self._with_retry(fetch)

    def get_by_project(
        self,
        project: str,
        include_superseded: bool = False,
    ) -> list[KnowledgeRecord]:
        """Get all knowledge for a project."""
        return self.search(
            project=project,
            limit=1000,
            include_superseded=include_superseded,
        )

    def get_by_tags(
        self,
        tags: list[str],
        include_superseded: bool = False,
    ) -> list[KnowledgeRecord]:
        """Get knowledge matching every requested tag."""
        return self.search(
            tags=tags,
            limit=1000,
            include_superseded=include_superseded,
        )

    def count(self) -> int:
        """Get total number of records, including historical records."""
        def count_records() -> int:
            with self._connect() as connection:
                return int(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_records"
                ).fetchone()[0])

        return self._with_retry(count_records)

    def prune_before(self, cutoff: datetime) -> int:
        """Atomically remove records older than a cutoff without dangling links."""
        if cutoff.tzinfo is None:
            raise ValueError("knowledge retention cutoff must be timezone-aware")

        def remove_expired() -> int:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT id FROM knowledge_records "
                    "WHERE julianday(updated_at) < julianday(?)",
                    (cutoff.isoformat(),),
                ).fetchall()
                record_ids = [str(row[0]) for row in rows]
                if not record_ids:
                    connection.commit()
                    return 0
                placeholders = ",".join("?" for _ in record_ids)
                connection.execute(
                    f"UPDATE knowledge_records SET supersedes=NULL "
                    f"WHERE supersedes IN ({placeholders})",
                    record_ids,
                )
                cursor = connection.execute(
                    f"DELETE FROM knowledge_records WHERE id IN ({placeholders})",
                    record_ids,
                )
                connection.commit()
                return int(cursor.rowcount)

        return self._with_retry(remove_expired)

    def delete(self, record_id: str) -> bool:
        """Atomically delete a record."""
        def remove() -> bool:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM knowledge_records WHERE id=?",
                    (record_id,),
                )
                connection.commit()
                return cursor.rowcount > 0

        return self._with_retry(remove)
