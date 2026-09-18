"""Memory curator for post-run knowledge promotion."""

import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .knowledge import KnowledgeStore, KnowledgeRecord
from ..contracts import CompletionReport, RequirementResult
from ..redaction import redact_sensitive_text
from ..simple_agent import SimpleAgent


CURATOR_SYSTEM_PROMPT = """You are the Agent Team memory curator.

Extract only durable, reusable knowledge from the bounded completion report and
worker summaries. Do not reproduce transcripts, secrets, or temporary chatter.
Return concise records with evidence-grounded topics, summaries, and details.
"""


_TRANSCRIPT_MARKER_RE = re.compile(
    r"(?:^|[\n\r])\s*(?:system|developer|user|assistant|tool|function)\s*[:=]"
    r"|<\|(?:system|user|assistant|tool)\|>|\"messages\"\s*:|\[/?(?:user|assistant|tool)\]",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_ -]?key|token|authorization)\s*[:=]\s*\S+"
)


class CuratorEntry(BaseModel):
    """One durable knowledge item proposed by the curator model."""

    kind: str = "finding"
    topic: str = Field(..., min_length=1)
    summary: str = ""
    details: str = ""
    tags: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    source_artifact: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class CuratorOutput(BaseModel):
    """Structured response expected from the curator model."""

    records: list[CuratorEntry] = Field(default_factory=list)


def create_curator_agent(model: Any) -> SimpleAgent:
    """Create the configured curator model wrapper."""
    return SimpleAgent(model, CURATOR_SYSTEM_PROMPT, "MemoryCurator")


class MemoryCurator:
    """Post-run memory curator.

    After a job completes, the curator:
    - Reviews completion report and worker summaries
    - Identifies useful lessons learned
    - Promotes to shared knowledge
    - Supersedes stale knowledge
    - Proposes skill updates

    Can be disabled behind config.
    """

    def __init__(
        self,
        curator_agent: Any | None,
        knowledge_store: KnowledgeStore,
        state_dir: Path,
        enabled: bool = False
    ):
        self.curator_agent = curator_agent
        self.knowledge_store = knowledge_store
        self.state_dir = state_dir
        self.enabled = enabled

    async def run_curator(
        self,
        completion_report: CompletionReport,
        worker_summaries: dict[str, str],
        project_id: str
    ) -> list[KnowledgeRecord]:
        """Run the curator after a job completes.

        Args:
            completion_report: The final report
            worker_summaries: Dict of worker_id -> summary
            project_id: Project identifier

        Returns:
            List of new knowledge records created
        """
        if not self.enabled:
            return []

        # Build context for curator
        context = self._build_curator_context(completion_report, worker_summaries, project_id)
        artifact_refs = self._artifact_references(completion_report)

        # Ask curator to identify knowledge
        new_records = await self._extract_knowledge(context, project_id, artifact_refs)

        # Promote by deterministic IDs so retrying the same project is idempotent.
        promoted: list[KnowledgeRecord] = []
        for record in new_records:
            existing = self.knowledge_store.get(record.id)
            if existing is not None:
                promoted.append(existing)
                continue
            try:
                self.knowledge_store.add(record)
            except sqlite3.IntegrityError:
                # Another caller may have promoted the same deterministic record.
                existing = self.knowledge_store.get(record.id)
                if existing is None:
                    raise
                promoted.append(existing)
                continue
            # Read back the winner so callers receive the canonical persisted row.
            existing = self.knowledge_store.get(record.id)
            if existing is None:
                raise RuntimeError(f"Knowledge promotion did not persist {record.id}")
            promoted.append(existing)

        # Check for superseding after promotion, using the stable records above.
        await self._supersede_stale(promoted)

        return promoted

    @classmethod
    def _safe_text(cls, value: Any, limit: int = 800) -> str | None:
        """Return bounded, non-transcript text for a curator prompt."""
        text = str(value or "").replace("\x00", " ").strip()
        if not text or _TRANSCRIPT_MARKER_RE.search(text):
            return None
        text = redact_sensitive_text(_SECRET_RE.sub("[redacted]", text))
        return text[:limit]

    @classmethod
    def _append_safe_items(cls, lines: list[str], prefix: str, items: Any) -> None:
        for item in items or []:
            safe = cls._safe_text(item)
            if safe is not None:
                lines.append(f"- {prefix}{safe}" if prefix else f"- {safe}")

    @staticmethod
    def _artifact_references(report: CompletionReport) -> list[str]:
        """Return stable artifact paths from the verified report."""
        return list(dict.fromkeys(
            artifact.path.strip()
            for artifact in report.artifacts
            if artifact.path and artifact.path.strip()
        ))

    def _build_curator_context(
        self,
        report: CompletionReport,
        worker_summaries: dict[str, str],
        project_id: str
    ) -> str:
        """Build an allowlisted, bounded context without raw transcripts."""
        lines = [
            f"Project: {project_id}",
            "",
            "CURATOR INPUT POLICY:",
            "Only compact report fields and worker summaries are supplied; raw transcripts are excluded.",
            "",
            "COMPLETION REPORT:",
            f"Status: {report.status}",
        ]
        for label, value in (("Summary", report.summary),):
            safe = self._safe_text(value)
            if safe is not None:
                lines.append(f"{label}: {safe}")

        lines.extend(["", "REQUIREMENT RESULTS:"])
        for result in report.requirement_results:
            lines.append(f"- {result.requirement_id}: {result.status}")
            self._append_safe_items(lines, "Evidence: ", result.evidence)

        lines.extend(["", "IMPORTANT FINDINGS:"])
        self._append_safe_items(lines, "", report.important_findings)

        lines.extend(["", "DECISIONS:"])
        self._append_safe_items(lines, "", report.decisions)

        lines.extend(["", "ARTIFACT REFERENCES:"])
        for artifact in report.artifacts:
            path = self._safe_text(artifact.path, limit=400)
            if path is None:
                continue
            metadata = (
                f"sha256={artifact.sha256 or '[not recorded]'}, "
                f"size_bytes={artifact.size_bytes if artifact.size_bytes is not None else '[not recorded]'}, "
                f"verified={artifact.verified}, method={artifact.verification_method or '[not recorded]'}"
            )
            lines.append(f"- {path} ({metadata})")

        lines.extend(["", "WORKER SUMMARIES:"])
        for worker_id, summary in worker_summaries.items():
            safe_worker_id = self._safe_text(worker_id, limit=160)
            safe_summary = self._safe_text(summary, limit=800)
            if safe_worker_id is not None and safe_summary is not None:
                lines.append(f"- {safe_worker_id}: {safe_summary}")

        lines.extend(["", "RISKS:"])
        self._append_safe_items(lines, "", report.risks)

        lines.extend(["", "UNRESOLVED ITEMS:"])
        self._append_safe_items(lines, "", report.unresolved_items)
        return "\n".join(lines)

    async def _extract_knowledge(
        self,
        context: str,
        project_id: str,
        artifact_refs: list[str] | None = None,
    ) -> list[KnowledgeRecord]:
        """Extract model-reviewed knowledge and convert it to durable records."""
        if self.curator_agent is not None:
            raw = await self.curator_agent.run(context, response_format=CuratorOutput)
            output = raw if isinstance(raw, CuratorOutput) else CuratorOutput.model_validate(raw)
            entries = output.records
        else:
            # Retain a deterministic fallback for library callers that construct
            # a curator without a model; the engine always supplies the configured model.
            entries = self._heuristic_entries(context)
        return self._records_from_entries(entries, context, project_id, artifact_refs or [])

    @staticmethod
    def _heuristic_entries(context: str) -> list[CuratorEntry]:
        """Extract bounded findings/decisions when no model is supplied."""
        entries: list[CuratorEntry] = []
        sections = (
            ("IMPORTANT FINDINGS:", "DECISIONS:", "finding", 0.8),
            ("DECISIONS:", "WORKER SUMMARIES:", "decision", 0.9),
        )
        for start, end, kind, confidence in sections:
            if start not in context:
                continue
            section = context.split(start, 1)[1]
            if end in section:
                section = section.split(end, 1)[0]
            for line in section.splitlines():
                topic = line.strip()[1:].strip() if line.strip().startswith("-") else ""
                if topic:
                    entries.append(
                        CuratorEntry(
                            kind=kind,
                            topic=topic,
                            summary=(f"Decision: {topic}" if kind == "decision" else topic),
                            details=context[:500],
                            tags=[kind],
                            confidence=confidence,
                        )
                    )
        return entries

    @staticmethod
    def _records_from_entries(
        entries: list[CuratorEntry],
        context: str,
        project_id: str,
        artifact_refs: list[str] | None = None,
    ) -> list[KnowledgeRecord]:
        """Build stable records while retaining project and artifact provenance."""
        records: dict[str, KnowledgeRecord] = {}
        source_run = f"{project_id}:curator"
        report_artifacts = list(dict.fromkeys(ref.strip() for ref in (artifact_refs or []) if ref.strip()))
        for entry in entries:
            topic = entry.topic.strip()
            if not topic:
                continue
            kind = entry.kind.strip().lower() or "finding"
            stable_key = f"{source_run}:{kind}:{topic.casefold()}"
            record_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
            entry_artifacts = [*entry.artifact_refs]
            if entry.source_artifact:
                entry_artifacts.insert(0, entry.source_artifact)
            artifact_values = list(dict.fromkeys(
                ref.strip() for ref in [*report_artifacts, *entry_artifacts] if str(ref).strip()
            ))
            tags = list(dict.fromkeys([
                kind,
                *(tag.strip() for tag in entry.tags if tag.strip()),
                *( ["artifact"] if artifact_values else [] ),
            ]))
            records.setdefault(
                record_id,
                KnowledgeRecord(
                    id=record_id,
                    topic=topic,
                    summary=entry.summary.strip() or topic,
                    details=entry.details.strip() or context[:500],
                    source_agent="curator",
                    project=project_id,
                    tags=tags,
                    confidence=entry.confidence,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                    source_run=source_run,
                    source_artifact=artifact_values[0] if artifact_values else None,
                    source_artifacts=artifact_values,
                ),
            )
        return list(records.values())

    async def _supersede_stale(self, new_records: list[KnowledgeRecord]) -> None:
        """Supersede stale knowledge with new records."""
        for new_record in new_records:
            # Find existing records with same topic
            existing = self.knowledge_store.search(topic=new_record.topic, limit=10)

            for old_record in existing:
                if old_record.id != new_record.id:
                    # Mark old as superseded
                    self.knowledge_store.supersede(old_record.id, new_record.id)

    async def propose_skill_update(self, pattern: str, project_id: str) -> str | None:
        """Persist a reviewable skill proposal instead of silently dropping a pattern."""
        if not self.enabled or not pattern.strip():
            return None
        proposal = (
            f"# Proposed Agent Team Skill\n\n"
            f"Observed pattern: {pattern.strip()}\n\n"
            "This proposal is deliberately not activated automatically. Review it, "
            "then install it as a bounded skill if it is reusable and safe.\n"
        )
        directory = self.state_dir / "skill-proposals"
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_.")[:120] or "project"
        path = directory / f"{safe_id}.md"
        fd, temporary = tempfile.mkstemp(prefix=".proposal.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(proposal)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return proposal
