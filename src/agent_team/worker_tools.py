"""Safe, executable tools for bounded worker agents."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRequestNode, ModelRetry, RunContext, Tool
from pydantic_ai.usage import UsageLimits

from .artifacts import ArtifactStore
from .contracts import ArtifactResult, WORKER_TOOL_NAMES
from .redaction import redact_model, redact_sensitive_text
from .runtime.cancellation import WorkerTimeoutError, WorkerTurnBudget, WorkerTurnUsage



# Hermes-native terminal, skills, MCP, and connector surfaces are deliberately
# not bridged into workers. The bounded builtin executor is the complete worker
# capability surface until a separately authorized bridge is designed.
HERMES_NATIVE_CAPABILITIES = frozenset()


@dataclass
class ToolResult:
    """Compact result returned to the worker model."""

    name: str
    ok: bool
    output: str
    artifact: ArtifactResult | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"tool": self.name, "ok": self.ok, "output": self.output}
        if self.artifact is not None:
            value["artifact"] = self.artifact.model_dump()
        return value


class WorkerToolExecutor:
    """Execute a small, auditable worker tool set inside one workspace."""

    BUILTIN_TOOLS = ("list_files", "read_file", "write_file")

    def __init__(
        self,
        workspace: Path | str,
        artifact_store: ArtifactStore | None = None,
        project_id: str = "project",
        worker_id: str = "worker",
        allowed_tools: list[str] | None = None,
        max_file_bytes: int = 1_000_000,
        max_tool_calls: int = 8,
        acceptance_criteria: list[dict[str, Any]] | None = None,
        max_turns: int | None = None,
        turn_budget: WorkerTurnBudget | None = None,
        write_guard=None,
    ):
        self.write_guard = write_guard or getattr(artifact_store, "write_guard", None)
        if self.write_guard is not None:
            self.write_guard()
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self.project_id = project_id
        self.worker_id = worker_id
        requested_tools = set(WORKER_TOOL_NAMES if allowed_tools is None else allowed_tools)
        invalid_tools = sorted(requested_tools - WORKER_TOOL_NAMES)
        if invalid_tools:
            raise ValueError(f"Unknown worker tool(s): {', '.join(invalid_tools)}")
        self.allowed_tools = requested_tools
        self.max_file_bytes = max(1, max_file_bytes)
        self.max_tool_calls = max(0, max_tool_calls)
        self.acceptance_criteria = [dict(item) for item in (acceptance_criteria or [])]
        self.turn_budget = turn_budget
        if self.turn_budget is None and max_turns is not None:
            self.turn_budget = WorkerTurnBudget(max_turns, worker_id=worker_id)
        self.results: list[ToolResult] = []
        self.artifacts: list[ArtifactResult] = []

    def _resolve(self, value: str) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("path escapes the worker workspace")
        return resolved

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name not in self.BUILTIN_TOOLS:
                result = ToolResult(name=name, ok=False, output=f"Unknown worker tool: {name}")
            elif name not in self.allowed_tools:
                result = ToolResult(name=name, ok=False, output=f"Worker tool not allowed: {name}")
            elif name == "list_files":
                result = await self._list_files(arguments)
            elif name == "read_file":
                result = await self._read_file(arguments)
            elif name == "write_file":
                result = await self._write_file(arguments)
            else:
                result = ToolResult(name=name, ok=False, output=f"Unknown worker tool: {name}")
        except Exception as exc:
            result = ToolResult(name=name, ok=False, output=f"{type(exc).__name__}: {exc}")
        result.output = redact_sensitive_text(result.output)
        if result.artifact is not None:
            result.artifact = redact_model(result.artifact)
        self.results.append(result)
        if result.artifact is not None:
            self.artifacts.append(result.artifact)
        return result

    async def _list_files(self, arguments: dict[str, Any]) -> ToolResult:
        root = self._resolve(str(arguments.get("path") or "."))
        if not root.is_dir():
            return ToolResult("list_files", False, "directory does not exist")
        entries = []
        for path in sorted(root.rglob("*")):
            if len(entries) >= 500:
                break
            relative = path.relative_to(self.workspace)
            entries.append(str(relative) + ("/" if path.is_dir() else ""))
        return ToolResult("list_files", True, "\n".join(entries) or "[empty]")

    async def _read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(str(arguments.get("path") or ""))
        if not path.is_file():
            return ToolResult("read_file", False, "file does not exist")
        if path.stat().st_size > self.max_file_bytes:
            return ToolResult("read_file", False, "file exceeds configured size limit")
        max_chars = min(int(arguments.get("max_chars") or self.max_file_bytes), self.max_file_bytes)
        return ToolResult("read_file", True, path.read_text(encoding="utf-8", errors="replace")[:max_chars])

    async def _write_file(self, arguments: dict[str, Any]) -> ToolResult:
        if self.write_guard is not None:
            self.write_guard()
        path = self._resolve(str(arguments.get("path") or ""))
        content = str(arguments.get("content") or "")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            return ToolResult("write_file", False, "content exceeds configured size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if self.write_guard is not None:
                self.write_guard()
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        digest = hashlib.sha256(encoded).hexdigest()
        artifact = ArtifactResult(
            path=str(path.relative_to(self.workspace)),
            description=str(arguments.get("description") or "Worker-created file"),
            created_by=self.worker_id,
            sha256=digest,
            size_bytes=len(encoded),
            verified=True,
            verification_method="atomic write and SHA-256",
        )
        if self.artifact_store is not None:
            artifact = self.artifact_store.verify(artifact)
            self.artifact_store.write_manifest(self.project_id, [artifact])
        return ToolResult("write_file", True, f"wrote {artifact.path}", artifact)


class _WorkerStopped(Exception):
    """A worker limit ended the run before a final result."""


class ToolAwareWorkerAgent:
    """Bounded worker loop on a Pydantic AI agent with native tool calling."""

    def __init__(
        self,
        model: Any,
        system_prompt: str,
        executor: WorkerToolExecutor,
        max_tool_calls: int | None = None,
        max_turns: int | None = None,
        turn_budget: WorkerTurnBudget | None = None,
        before_turn: Any | None = None,
        on_turn: Any | None = None,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.executor = executor
        self.acceptance_criteria = [dict(item) for item in executor.acceptance_criteria]
        self.max_tool_calls = max(0, executor.max_tool_calls if max_tool_calls is None else max_tool_calls)
        self.turn_budget = turn_budget or executor.turn_budget
        if self.turn_budget is None:
            self.turn_budget = WorkerTurnBudget(
                max_turns if max_turns is not None else 10,
                worker_id=getattr(executor, "worker_id", "worker"),
                before_turn=before_turn,
                on_turn=on_turn,
            )
        self._calls_used = 0

    @property
    def turn_usage(self) -> WorkerTurnUsage:
        """Return the model/tool iterations consumed by this worker."""
        return self.turn_budget.usage

    async def run(self, user_message: str, response_format: type[BaseModel] | None = None) -> Any:
        tools = self._tools()
        granted = ", ".join(tool.name for tool in tools) or "none"
        instructions = (
            f"{self.system_prompt}\n\nEXECUTABLE TOOLS: {granted}\n"
            "Call tools only through the native tool-calling interface supplied with this request, "
            "then return the requested final output."
        )
        agent = Agent(
            self.model,
            output_type=response_format or str,
            instructions=instructions,
            tools=tools,
            retries=1,
        )
        if response_format is not None and self.acceptance_criteria:
            agent.output_validator(self._require_acceptance_ids)
        self._calls_used = 0
        try:
            async with agent.iter(user_message, usage_limits=UsageLimits(request_limit=None)) as run:
                node = run.next_node
                while not Agent.is_end_node(node):
                    if isinstance(node, ModelRequestNode):
                        try:
                            await self.turn_budget.consume("model")
                        except WorkerTimeoutError as exc:
                            raise _WorkerStopped(str(exc)) from exc
                    node = await run.next(node)
                return run.result.output
        except _WorkerStopped as stopped:
            return self._partial_result(response_format, str(stopped))

    def _require_acceptance_ids(self, ctx: RunContext[Any], output: Any) -> Any:
        """Ask once for omitted acceptance IDs; afterwards accept the output as-is."""
        missing_ids = self._missing_acceptance_ids(output)
        if missing_ids and ctx.retry == 0:
            raise ModelRetry(
                "The parsed response omitted required acceptance criterion IDs: "
                + ", ".join(missing_ids)
                + ". Return one AcceptanceResult for every criterion. Preserve facts proven by tool "
                "results, include exact acceptance criterion IDs, and mark anything not proven as "
                "not_verified or unresolved. Required criteria:\n"
                + json.dumps(self.acceptance_criteria, indent=2)
            )
        return output

    def _missing_acceptance_ids(self, output: Any) -> list[str]:
        required = [str(item.get("id") or "") for item in self.acceptance_criteria]
        present = {
            str(getattr(item, "criterion_id", ""))
            for item in (getattr(output, "acceptance_results", None) or [])
        }
        return [criterion_id for criterion_id in required if criterion_id and criterion_id not in present]

    def _tools(self) -> list[Tool]:
        async def list_files(path: str = ".") -> dict[str, Any]:
            """List files under a workspace-relative directory."""
            return await self._call_tool("list_files", {"path": path})

        async def read_file(path: str, max_chars: int | None = None) -> dict[str, Any]:
            """Read a workspace-relative UTF-8 text file."""
            return await self._call_tool("read_file", {"path": path, "max_chars": max_chars})

        async def write_file(path: str, content: str, description: str | None = None) -> dict[str, Any]:
            """Atomically write the full content of a workspace-relative file."""
            return await self._call_tool(
                "write_file", {"path": path, "content": content, "description": description}
            )

        functions = {"list_files": list_files, "read_file": read_file, "write_file": write_file}
        return [
            Tool(function, sequential=True)
            for name, function in functions.items()
            if name in self.executor.allowed_tools
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Tools are sequential barriers, so this check-then-count cannot race
        # within a batch of tool calls from one model response.
        if self._calls_used >= self.max_tool_calls:
            raise _WorkerStopped(
                f"Worker tool-call limit of {self.max_tool_calls} reached before a final result."
            )
        try:
            await self.turn_budget.consume("tool")
        except WorkerTimeoutError as exc:
            raise _WorkerStopped(str(exc)) from exc
        self._calls_used += 1
        result = await self.executor.execute(name, arguments)
        return result.as_dict()

    def _partial_result(self, response_format: type[BaseModel] | None, reason: str) -> Any:
        """Return a schema-shaped partial result without exposing model chatter."""
        evidence = [
            f"{item.name}: {item.output[:600]}"
            for item in self.executor.results
            if item.ok and item.output
        ]
        payload = {
            "status": "partial",
            "summary": "Worker stopped before producing a final result: " + reason,
            "unresolved": [reason],
            "evidence": list(dict.fromkeys(evidence)),
            "artifacts": [item.model_dump() for item in self.executor.artifacts],
        }
        if response_format is None:
            return payload["summary"]
        return response_format.model_validate(payload)
