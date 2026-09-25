"""Principal → Team Manager → workers execution engine.

The engine owns the hierarchy and lifecycle. Models may propose decisions and
plans, but typed validation, worker limits, cancellation, timeouts, persistence,
and user-facing boundaries are enforced here.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .config import Config
from .control.discovery import OpenAIModelDiscovery
from .artifacts import ArtifactStore
from .contracts import (
    AcceptanceResult,
    CompletionReport,
    ManagerPlan,
    PrincipalDecision,
    ProjectBrief,
    RequirementResult,
    WorkerOutput,
    WorkerResult,
    WorkerTaskSpec,
    WORKER_TOOL_NAMES,
)
from .manager import create_manager_agent, fallback_manager_plan
from .memory.agent_memory import AgentMemory
from .memory.curator import MemoryCurator, create_curator_agent
from .memory.knowledge import KnowledgeRecord, KnowledgeStore
from .persistence import ProjectStore
from .principal import (
    build_default_brief,
    create_principal_agent,
    looks_substantial,
    validate_completion_report,
)
from .redaction import (
    install_redacting_filter,
    redact_model,
    redact_sensitive_data,
    redact_sensitive_text,
)
from .runtime.cancellation import (
    HardCanceller,
    ProjectTimeoutError,
    TimeoutEnforcer,
    WorkerTurnBudget,
    enforce_worker_lifecycle,
)
from .runtime.lifecycle import ConcurrencyLimiter, TaskRegistry, WorkerState
from .runtime_boundary import ensure_external_runtime_paths
from .worker_factory import PROFESSIONAL_ROLES, create_worker_agent, get_role_by_name, role_from_definition
from .worker_tools import WorkerToolExecutor


logger = install_redacting_filter(logging.getLogger("agent_team.engine"))
runtime_logger = install_redacting_filter(logging.getLogger("agent-team-engine"))


@dataclass
class PrincipalTurnResult:
    """Internal result returned to the API adapter; only ``response`` is user-facing."""

    action: str
    response: str
    brief: ProjectBrief | None = None
    plan: ManagerPlan | None = None
    report: CompletionReport | None = None
    validation_issues: list[str] = field(default_factory=list)
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return redact_sensitive_data({
            "action": self.action,
            "response": self.response,
            "brief": self.brief.model_dump() if self.brief else None,
            "plan": self.plan.model_dump() if self.plan else None,
            "report": self.report.model_dump() if self.report else None,
            "validation_issues": list(self.validation_issues),
            "project_id": self.project_id,
        })


@dataclass
class ActiveProject:
    session_id: str
    project_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    worker_ids: dict[str, str] = field(default_factory=dict)
    role_catalog: dict[str, Any] = field(default_factory=dict)
    plan: ManagerPlan | None = None


class AgentTeamEngine:
    """Orchestrate one Principal, one Team Manager, and bounded workers."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = runtime_logger
        state_dir, workspace_dir = ensure_external_runtime_paths(
            config.runtime.state_dir,
            workspace_dir=getattr(config.runtime, "workspace_dir", None),
        )
        # Keep all downstream stores on the canonical, resolved external paths.
        config.runtime.state_dir = state_dir
        config.runtime.workspace_dir = workspace_dir

        self._discovered_models: dict[str, str] = {}
        self.principal_model = self._create_model(config.models.get("principal"))
        self.manager_model = self._create_model(config.models.get("manager"))
        self.worker_model = self._create_model(config.models.get("worker"))
        self.curator_model = None
        if config.memory.enabled and config.memory.shared_knowledge and config.memory.curator_enabled:
            self.curator_model = self._create_model(
                config.models.get("curator") or config.models.get("worker")
            )

        self.principal = create_principal_agent(self.principal_model)
        self.manager = create_manager_agent(self.manager_model)

        self.registry = TaskRegistry(
            max_workers=config.runtime.max_workers,
            max_concurrent=config.runtime.max_concurrent_workers,
        )
        self.process_manager = __import__(
            "agent_team.runtime.lifecycle", fromlist=["ProcessManager"]
        ).ProcessManager(self.registry)
        self.canceller = HardCanceller(self.registry, self.process_manager)
        self.timeout_enforcer = TimeoutEnforcer(
            self.registry,
            max_turns=config.runtime.max_turns,
            task_timeout_seconds=config.runtime.worker_timeout_seconds,
            team_timeout_seconds=config.runtime.team_timeout_seconds,
        )
        self.concurrency_limiter = ConcurrencyLimiter(config.runtime.max_concurrent_workers)

        state_dir = config.runtime.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        workspace = getattr(config.runtime, "workspace_dir", None) or state_dir / "workspace"
        self.artifact_store = ArtifactStore(workspace, state_dir / "artifacts")
        self.project_store = ProjectStore(state_dir / "projects")
        memory_enabled = bool(config.memory.enabled)
        self.knowledge_store = (
            KnowledgeStore(state_dir / "knowledge.db")
            if memory_enabled and config.memory.shared_knowledge
            else None
        )
        self.curator = (
            MemoryCurator(
                curator_agent=create_curator_agent(self.curator_model),
                knowledge_store=self.knowledge_store,
                state_dir=state_dir,
                enabled=True,
            )
            if self.knowledge_store is not None
            and config.memory.curator_enabled
            and self.curator_model is not None
            else None
        )
        self.principal_memory = (
            AgentMemory("principal", state_dir / "memory") if memory_enabled else None
        )
        self.manager_memory = (
            AgentMemory("manager", state_dir / "memory") if memory_enabled else None
        )
        self._role_memories: dict[str, AgentMemory] = {}

        self._active_projects: dict[str, ActiveProject] = {}
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False

    def _create_model(self, model_config) -> Any:
        """Create a model wrapper, discovering a sole advertised model when requested."""
        if not model_config:
            raise ValueError("Model config required")
        from .models.http_model import create_simple_model

        base_url = model_config.base_url or os.getenv("LLM_BASE_URL", "http://model-service:8000/v1")
        canonical_base_url = base_url.rstrip("/")
        api_key = os.getenv("LLM_API_KEY", "")
        model_name = model_config.model or os.getenv("LLM_MODEL", "auto")
        if model_name == "auto":
            if canonical_base_url not in self._discovered_models:
                self._discovered_models[canonical_base_url] = OpenAIModelDiscovery(
                    canonical_base_url,
                    api_key=api_key,
                ).detect_model()
            model_name = self._discovered_models[canonical_base_url]
            model_config.model = model_name

        return create_simple_model(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
        )

    async def run(self, user_input: str, session_id: str | None = None) -> CompletionReport:
        """Compatibility API returning only a typed report."""
        result = await self.run_turn(user_input, session_id=session_id)
        if result.report is not None:
            return result.report
        return CompletionReport(
            status="complete" if result.action == "answer" else "blocked",
            summary=result.response or "No response was produced.",
            unresolved_items=list(result.validation_issues),
        )

    async def run_turn(
        self,
        user_input: str,
        session_id: str | None = None,
        conversation: list[dict[str, Any]] | None = None,
    ) -> PrincipalTurnResult:
        """Run one user turn through the Principal boundary."""
        user_input = redact_sensitive_text(user_input)
        conversation = redact_sensitive_data(conversation or [])
        if getattr(self, "_closed", False):
            return PrincipalTurnResult(
                action="delegate",
                response="The Agent Team engine is shut down; no work was started.",
                report=CompletionReport(
                    status="failed",
                    summary="The Agent Team engine is shut down.",
                    unresolved_items=["Engine is closed."],
                ),
                validation_issues=["Engine is closed."],
            )
        session_id = session_id or "session-001"
        current = asyncio.current_task()
        if current is not None:
            self._run_tasks[session_id] = current
        project: ActiveProject | None = None
        try:
            decision = redact_model(
                await self._principal_decide(user_input, session_id, conversation)
            )
            if decision.action == "answer":
                return await self._run_direct_turn(
                    user_input, session_id, conversation, response=decision.response
                )
            if decision.action == "clarify":
                response = decision.response or "I need one clarification before I start: what outcome should I optimize for?"
                return PrincipalTurnResult(
                    action="clarify",
                    response=redact_sensitive_text(response),
                )

            brief = decision.brief or build_default_brief(user_input, session_id)
            project = await self._execute_project(user_input, session_id, brief, conversation)
            return project
        except asyncio.CancelledError:
            project_obj = project or self._active_projects.get(session_id)
            if project_obj is not None:
                await self._cleanup_project(project_obj, cancel=True)
            logger.info("project_cancelled session=%s", session_id)
            return PrincipalTurnResult(
                action="delegate",
                response="The project was cancelled. No active worker work was left running.",
                report=CompletionReport(
                    status="blocked",
                    summary="Execution was cancelled before completion.",
                    unresolved_items=["Cancelled by the user."],
                ),
                validation_issues=["Cancelled by the user."],
            )
        except Exception as exc:
            project_obj = project or self._active_projects.get(session_id)
            if project_obj is not None:
                await self._cleanup_project(project_obj, cancel=True)
            logger.exception("project_failed session=%s", session_id)
            safe_error = redact_sensitive_text(exc)
            return PrincipalTurnResult(
                action="delegate",
                response=f"I couldn't complete the project: {safe_error}",
                report=CompletionReport(
                    status="failed",
                    summary="The project failed before a validated completion report could be produced.",
                    unresolved_items=[safe_error],
                ),
                validation_issues=[safe_error],
            )
        finally:
            if current is not None and self._run_tasks.get(session_id) is current:
                self._run_tasks.pop(session_id, None)

    async def run_delegated_brief(
        self,
        brief: ProjectBrief,
        session_id: str | None = None,
    ) -> PrincipalTurnResult:
        """Execute a Principal-authored brief without re-running Principal intake."""
        if getattr(self, "_closed", False):
            return PrincipalTurnResult(
                action="delegate",
                response="The Agent Team engine is shut down; no work was started.",
                report=CompletionReport(
                    status="failed",
                    summary="The Agent Team engine is shut down.",
                    unresolved_items=["Engine is closed."],
                ),
                validation_issues=["Engine is closed."],
            )
        if not isinstance(brief, ProjectBrief):
            brief = ProjectBrief.model_validate(brief)
        brief = redact_model(brief)
        session_id = session_id or "session-001"
        current = asyncio.current_task()
        if current is not None:
            self._run_tasks[session_id] = current
        try:
            return await self._execute_project(
                brief.objective,
                session_id,
                brief,
                [],
            )
        except asyncio.CancelledError:
            return PrincipalTurnResult(
                action="delegate",
                response="The delegated project was cancelled.",
                report=CompletionReport(
                    status="blocked",
                    summary="Delegated execution was cancelled before completion.",
                    unresolved_items=["Cancelled by the user."],
                ),
                validation_issues=["Cancelled by the user."],
            )
        except Exception as exc:
            logger.exception("delegated_brief_failed session=%s", session_id)
            safe_error = redact_sensitive_text(exc)
            return PrincipalTurnResult(
                action="delegate",
                response=f"The delegated project failed: {safe_error}",
                report=CompletionReport(
                    status="failed",
                    summary="The delegated project failed before a validated completion report was produced.",
                    unresolved_items=[safe_error],
                ),
                validation_issues=[safe_error],
            )
        finally:
            if current is not None and self._run_tasks.get(session_id) is current:
                self._run_tasks.pop(session_id, None)

    async def cancel(self, session_id: str) -> bool:
        """Cancel the active project tree and the root model request."""
        project = self._active_projects.get(session_id)
        task = self._run_tasks.get(session_id)
        if project is None and task is None:
            return False
        if project is not None:
            project.cancel_event.set()
            await self._cancel_project_tree(project, cancel_root=False)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        return True

    async def close(self) -> None:
        """Stop all projects, reap resources, and make shutdown idempotent."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            current = asyncio.current_task()
            projects = list(self._active_projects.values())
            for project in projects:
                try:
                    await self._cancel_project_tree(project, cancel_root=False)
                except Exception:
                    self.logger.exception("project_shutdown_cleanup_failed project=%s", project.project_id)

            root_tasks = [
                task for task in self._run_tasks.values()
                if task is not current and not task.done()
            ]
            for task in root_tasks:
                task.cancel()
            if root_tasks:
                await asyncio.gather(*root_tasks, return_exceptions=True)

            for project in list(self._active_projects.values()):
                try:
                    await self._cleanup_project(project, cancel=True)
                except Exception:
                    self.logger.exception("project_shutdown_finalize_failed project=%s", project.project_id)
            try:
                await self.process_manager.terminate_all_workers(hard=True)
            except Exception:
                self.logger.exception("process_shutdown_cleanup_failed")
            try:
                await self.timeout_enforcer.close()
            except Exception:
                self.logger.exception("deadline_shutdown_cleanup_failed")
            try:
                await self.registry.cleanup()
            except Exception:
                self.logger.exception("registry_shutdown_cleanup_failed")
            self._active_projects.clear()
            self._run_tasks.clear()

    def _principal_memory_summary(self) -> dict[str, Any]:
        """Expose only Principal-global memory to Principal prompts."""
        if not self.config.memory.enabled or self.principal_memory is None:
            return {}
        return self.principal_memory.get_summary(limit=getattr(self.config.memory, "recent_items", 8))

    def _manager_memory_summary(self) -> dict[str, Any]:
        """Expose only Manager-global memory to Manager prompts."""
        if not self.config.memory.enabled or self.manager_memory is None:
            return {}
        return self.manager_memory.get_summary(limit=getattr(self.config.memory, "recent_items", 8))

    def _worker_memory_summary(self, role: str, project_id: str) -> dict[str, Any]:
        """Expose one role's global memory and this project's role memory only.

        Principal and Manager memory, other worker roles, and other project scopes
        are never included in a worker prompt.
        """
        if not self.config.memory.enabled:
            return {}
        role_memory = self._role_memories.setdefault(
            role,
            AgentMemory(f"worker-{role}", self.config.runtime.state_dir / "memory"),
        )
        project_memory = AgentMemory(
            f"worker-{role}", self.config.runtime.state_dir / "memory", scope=project_id
        )
        limit = getattr(self.config.memory, "recent_items", 8)
        return {
            "global": role_memory.get_summary(limit=limit),
            "project": project_memory.get_summary(limit=limit),
        }

    async def _principal_decide(
        self, user_input: str, session_id: str, conversation: list[dict[str, Any]]
    ) -> PrincipalDecision:
        history = self._compact_history(conversation)
        memory = self._principal_memory_summary()
        prompt = (
            "Decide how to handle this user turn. Return a PrincipalDecision.\n\n"
            f"USER TURN:\n{user_input}\n\n"
            f"RECENT PRINCIPAL CONVERSATION (bounded):\n{history or '[none]'}\n\n"
            f"COMPACT PRINCIPAL MEMORY SUMMARY:\n{json.dumps(memory, default=str)}\n\n"
            "Use action=answer for a simple answer, action=clarify only when material information is missing, "
            "and action=delegate for substantial work. A delegate decision must include a complete ProjectBrief."
        )
        try:
            decision = await self.principal.run(prompt, response_format=PrincipalDecision)
            if isinstance(decision, PrincipalDecision):
                return decision
            return PrincipalDecision.model_validate(decision)
        except Exception as exc:
            self.logger.warning("principal structured intake failed: %s", exc)
            if looks_substantial(user_input):
                return PrincipalDecision(
                    action="delegate",
                    brief=build_default_brief(user_input, session_id),
                    rationale="Deterministic fallback selected substantial work.",
                )
            return PrincipalDecision(action="answer", rationale="Deterministic direct-answer fallback.")

    async def _run_direct_turn(
        self,
        user_input: str,
        session_id: str,
        conversation: list[dict[str, Any]],
        response: str | None = None,
    ) -> PrincipalTurnResult:
        """Answer directly through the Principal without creating a Manager or worker."""
        if not (response or "").strip():
            prompt = (
                "Answer the user's request directly. Do not delegate, mention internal teams, or narrate "
                "orchestration. Be concise unless the request requires detail.\n\n"
                f"USER REQUEST:\n{user_input}\n\n"
                f"RECENT CONTEXT:\n{self._compact_history(conversation)}"
            )
            try:
                raw = await self.principal.run(prompt)
                response = raw if isinstance(raw, str) else str(raw or "")
            except Exception as exc:
                self.logger.warning("principal direct response failed: %s", exc)
                response = "I can answer that directly, but the Principal model did not return a usable response."
        return PrincipalTurnResult(
            action="answer",
            response=redact_sensitive_text(response).strip(),
        )

    async def _execute_project(
        self,
        user_input: str,
        session_id: str,
        brief: ProjectBrief,
        conversation: list[dict[str, Any]],
    ) -> PrincipalTurnResult:
        brief = redact_model(brief)
        conversation = redact_sensitive_data(conversation)
        project_id = f"{session_id}-{uuid.uuid4().hex[:8]}"
        project = ActiveProject(session_id=session_id, project_id=project_id)
        self._active_projects[session_id] = project
        # Start the single project budget before any planning or persistence work.
        self.timeout_enforcer.start_team(project_id)

        cleanup_with_cancel = False
        try:
            self.project_store.write_json(project_id, "brief", brief.model_dump())
            self.logger.info("project_created session=%s project=%s", session_id, project_id)
            plan = await self.timeout_enforcer.run_with_team_deadline(
                self._create_manager_plan(brief, session_id),
                project_id,
            )
            plan = redact_model(plan)
            project.plan = plan
            project.role_catalog = self._role_catalog(plan)
            self.project_store.write_json(project_id, "manager_plan", plan.model_dump())
            self.logger.info(
                "manager_plan project=%s tasks=%s review_required=%s",
                project_id, len(plan.tasks), plan.review_required,
            )
            worker_results = await self.timeout_enforcer.run_with_team_deadline(
                self._execute_plan(project, brief, plan),
                project_id,
            )
            report = redact_model(
                self._build_completion_report(brief, worker_results, project)
            )
            report = self.artifact_store.verify_report(project_id, report)
            valid, issues = self.validate_report(brief, report)
            if not valid:
                repaired = await self._repair_report(brief, report, issues)
                if repaired is not None:
                    repaired = self.artifact_store.verify_report(project_id, repaired)
                    repaired_valid, repaired_issues = self.validate_report(brief, repaired)
                    if repaired_valid:
                        report, issues, valid = redact_model(repaired), [], True
                    else:
                        report, issues = repaired, list(dict.fromkeys([*issues, *repaired_issues]))
                if report.status == "complete":
                    report.status = "partial"
                report.unresolved_items = list(dict.fromkeys([*report.unresolved_items, *issues]))
            self.project_store.write_json(project_id, "completion_report", report.model_dump())
            response = redact_sensitive_text(
                await self._render_principal_response(brief, report, issues)
            )
            await self._record_memory(project_id, brief, plan, report, worker_results)
            return PrincipalTurnResult(
                action="delegate",
                response=response,
                brief=brief,
                plan=plan,
                report=report,
                validation_issues=issues,
                project_id=project_id,
            )
        except asyncio.CancelledError:
            cleanup_with_cancel = True
            project.cancel_event.set()
            report = CompletionReport(
                status="blocked",
                summary="Execution was cancelled before completion.",
                unresolved_items=["Cancelled by the user."],
            )
            self._persist_report_best_effort(project_id, report)
            raise
        except ProjectTimeoutError:
            cleanup_with_cancel = True
            project.cancel_event.set()
            report = CompletionReport(
                status="blocked",
                summary="The Team Manager exceeded the project deadline.",
                unresolved_items=["Project timeout exceeded."],
            )
            self._persist_report_best_effort(project_id, report)
            return PrincipalTurnResult(
                action="delegate", response="The project hit its execution deadline before completion.",
                brief=brief, report=report, validation_issues=report.unresolved_items, project_id=project_id,
            )
        except asyncio.TimeoutError:
            cleanup_with_cancel = True
            project.cancel_event.set()
            report = CompletionReport(
                status="blocked",
                summary="The Team Manager exceeded the project deadline.",
                unresolved_items=["Project timeout exceeded."],
            )
            self._persist_report_best_effort(project_id, report)
            return PrincipalTurnResult(
                action="delegate", response="The project hit its execution deadline before completion.",
                brief=brief, report=report, validation_issues=report.unresolved_items, project_id=project_id,
            )
        except Exception as exc:
            cleanup_with_cancel = True
            project.cancel_event.set()
            safe_error = redact_sensitive_text(exc)
            report = CompletionReport(
                status="failed",
                summary="The project failed before a validated completion report could be produced.",
                unresolved_items=[safe_error],
            )
            self._persist_report_best_effort(project_id, report)
            self.logger.exception("project_failed session=%s project=%s", session_id, project_id)
            return PrincipalTurnResult(
                action="delegate",
                response=f"I couldn't complete the project: {safe_error}",
                brief=brief,
                plan=project.plan,
                report=report,
                validation_issues=report.unresolved_items,
                project_id=project_id,
            )
        finally:
            try:
                await self._cleanup_project(project, cancel=cleanup_with_cancel)
            except Exception:
                self.logger.exception("project_cleanup_failed session=%s project=%s", session_id, project_id)
            try:
                await self.timeout_enforcer.finish_team(project_id)
            except Exception:
                self.logger.exception("project_deadline_cleanup_failed project=%s", project_id)
            try:
                await self.registry.prune_terminal(getattr(self.config.runtime, "terminal_retention_seconds", 3600))
            except Exception:
                self.logger.exception("registry_prune_failed project=%s", project_id)
            self._active_projects.pop(session_id, None)

    async def _create_manager_plan(self, brief: ProjectBrief, session_id: str) -> ManagerPlan:
        roles = [
            {
                "name": role.name,
                "title": role.title,
                "objective": role.objective,
                "tools": sorted(WORKER_TOOL_NAMES),
            }
            for role in PROFESSIONAL_ROLES.values()
        ]
        manager_memory = self._manager_memory_summary()
        shared_knowledge = []
        if self.knowledge_store is not None:
            shared_knowledge = [record.model_dump(mode="json") for record in self.knowledge_store.search(topic=brief.objective, limit=8)]
        prompt = (
            "Create a ManagerPlan for this ProjectBrief. Choose the smallest useful team. "
            "Workers cannot create sub-teams. You may define up to the configured number of project-local "
            "specialist_roles when the fixed catalog is insufficient; each role must be narrowly scoped and "
            "use only the built-in worker tools.\n\n"
            f"PROJECT BRIEF:\n{brief.model_dump_json(indent=2)}\n\n"
            f"AVAILABLE SPECIALIST ROLES:\n{json.dumps(roles, indent=2)}\n\n"
            f"ALLOWED WORKER TOOLS:\n{json.dumps(sorted(WORKER_TOOL_NAMES))}\n\n"
            f"COMPACT MANAGER MEMORY SUMMARY:\n{json.dumps(manager_memory, default=str)}\n\n"
            f"RELEVANT SHARED KNOWLEDGE:\n{json.dumps(shared_knowledge, default=str)}\n\n"
            f"Maximum workers: {self.config.runtime.max_workers}\n"
            f"Maximum project-local specialist roles: {getattr(self.config.runtime, 'max_dynamic_roles', 3)}\n"
            "Return only the typed plan."
        )
        try:
            plan = await self.manager.run(prompt, response_format=ManagerPlan)
            if not isinstance(plan, ManagerPlan):
                plan = ManagerPlan.model_validate(plan)
        except Exception as exc:
            self.logger.warning("manager structured plan failed: %s", exc)
            plan = fallback_manager_plan(brief, self.config.runtime.max_workers)
        return self._sanitize_plan(plan, brief)

    def _sanitize_plan(self, plan: ManagerPlan, brief: ProjectBrief) -> ManagerPlan:
        allowed_requirements = {requirement.id for requirement in brief.requirements}
        dynamic_definitions = []
        dynamic_roles: dict[str, Any] = {}
        max_dynamic = int(getattr(self.config.runtime, "max_dynamic_roles", 3))
        for definition in plan.specialist_roles:
            if len(dynamic_definitions) >= max_dynamic or definition.name in PROFESSIONAL_ROLES:
                continue
            dynamic_definitions.append(definition)
            dynamic_roles[definition.name] = role_from_definition(definition)
        allowed_roles = {**PROFESSIONAL_ROLES, **dynamic_roles}
        sanitized: list[WorkerTaskSpec] = []
        seen_ids: set[str] = set()
        for index, task in enumerate(plan.tasks):
            if len(sanitized) >= self.config.runtime.max_workers:
                break
            task_id = task.task_id.strip() or f"task-{index + 1}"
            if task_id in seen_ids:
                task_id = f"{task_id}-{index + 1}"
            role = task.role.strip().lower()
            if role not in allowed_roles:
                role = self._role_alias(role)
            if role not in allowed_roles:
                role = "software-engineer"
            requirement_ids = [rid for rid in task.requirement_ids if rid in allowed_requirements]
            if not requirement_ids:
                requirement_ids = list(allowed_requirements)
            task_tools = list(task.tools) if task.tools else sorted(WORKER_TOOL_NAMES)
            invalid_tools = [tool for tool in task_tools if tool not in WORKER_TOOL_NAMES]
            if invalid_tools:
                continue
            sanitized.append(task.model_copy(update={
                "task_id": task_id,
                "role": role,
                "objective": task.objective.strip() or brief.objective,
                "requirement_ids": requirement_ids,
                "relevant_context": list(task.relevant_context)[:20],
                "dependencies": [dep for dep in task.dependencies if dep != task_id],
                "tools": task_tools,
            }))
            seen_ids.add(task_id)
        if not sanitized and brief.requirements:
            fallback = fallback_manager_plan(brief, self.config.runtime.max_workers)
            sanitized = fallback.tasks
            notes = [*plan.execution_notes, *fallback.execution_notes]
            return self._ensure_review_task(
                plan.model_copy(update={"tasks": sanitized, "specialist_roles": dynamic_definitions, "execution_notes": notes}),
                brief,
            )
        return self._ensure_review_task(
            plan.model_copy(update={"tasks": sanitized, "specialist_roles": dynamic_definitions}),
            brief,
        )

    def _ensure_review_task(self, plan: ManagerPlan, brief: ProjectBrief) -> ManagerPlan:
        if not plan.review_required or any(task.role == "reviewer" for task in plan.tasks):
            return plan
        review_task_id = "review-task"
        existing_task_ids = {task.task_id for task in plan.tasks}
        suffix = 1
        while review_task_id in existing_task_ids:
            review_task_id = f"review-task-{suffix}"
            suffix += 1
        review = WorkerTaskSpec(
            task_id=review_task_id,
            role="reviewer",
            objective="Review every completed assignment against the brief and acceptance criteria.",
            requirement_ids=[requirement.id for requirement in brief.requirements],
            relevant_context=list(brief.relevant_context),
            dependencies=[task.task_id for task in plan.tasks],
            deliverable="Evidence-backed review report",
            definition_of_done=[criterion.description for criterion in brief.acceptance_criteria],
            tools=["read_file"],
        )
        tasks = list(plan.tasks)
        max_workers = int(self.config.runtime.max_workers)
        if len(tasks) >= max_workers:
            tasks = tasks[: max(0, max_workers - 1)]
            review = review.model_copy(update={"dependencies": [task.task_id for task in tasks]})
        tasks.append(review)
        return plan.model_copy(update={
            "tasks": tasks,
            "execution_notes": [*plan.execution_notes, "Runtime commissioned a reviewer because review_required was set."],
        })

    @staticmethod
    def _role_catalog(plan: ManagerPlan) -> dict[str, Any]:
        catalog = dict(PROFESSIONAL_ROLES)
        for definition in plan.specialist_roles:
            catalog[definition.name] = role_from_definition(definition)
        return catalog

    @staticmethod
    def _role_alias(role: str) -> str:
        aliases = {
            "engineer": "software-engineer", "developer": "software-engineer", "coder": "software-engineer",
            "systems": "systems-engineer", "architect": "systems-engineer", "research": "researcher",
            "analyst": "researcher", "security-reviewer": "reviewer", "tool": "tool-engineer",
            "ux": "designer",
        }
        return aliases.get(role, role)

    async def _cancel_worker_tasks(
        self,
        project: ActiveProject,
        tasks: Iterable[asyncio.Task] | None = None,
    ) -> None:
        """Cancel and await worker tasks, tolerating repeated cleanup calls."""
        selected = list(tasks if tasks is not None else project.worker_tasks.values())
        for worker_task in selected:
            if worker_task is not asyncio.current_task() and not worker_task.done():
                worker_task.cancel()
        if selected:
            await asyncio.gather(*selected, return_exceptions=True)
        selected_ids = {id(worker_task) for worker_task in selected}
        for task_id, worker_task in list(project.worker_tasks.items()):
            if id(worker_task) in selected_ids and worker_task.done():
                project.worker_tasks.pop(task_id, None)

    async def _execute_plan(
        self, project: ActiveProject, brief: ProjectBrief, plan: ManagerPlan
    ) -> list[WorkerResult]:
        pending = {task.task_id: task for task in plan.tasks}
        completed: dict[str, WorkerResult] = {}
        results: list[WorkerResult] = []
        try:
            while pending:
                await self.timeout_enforcer.check_team_timeout(project.project_id)
                if project.cancel_event.is_set():
                    break
                ready = [
                    task for task in pending.values()
                    if all(dep in completed or dep not in {**pending} for dep in task.dependencies)
                ]
                if not ready:
                    for task in pending.values():
                        result = WorkerResult(
                            worker_id="unassigned", task_id=task.task_id, role=task.role,
                            status="failed", summary="Task could not run because its dependencies formed a cycle.",
                            unresolved=["Cyclic or missing task dependency."],
                        )
                        completed[task.task_id] = result
                        results.append(result)
                    break
                tasks: list[tuple[WorkerTaskSpec, asyncio.Task]] = []
                for task in ready:
                    pending.pop(task.task_id, None)
                    dependency_results = [completed[dep] for dep in task.dependencies if dep in completed]
                    running = asyncio.create_task(self._execute_worker(project, brief, task, dependency_results))
                    project.worker_tasks[task.task_id] = running
                    tasks.append((task, running))
                try:
                    gathered = await asyncio.gather(
                        *(running for _, running in tasks),
                        return_exceptions=True,
                    )
                    timeout_errors = [value for value in gathered if isinstance(value, ProjectTimeoutError)]
                    if timeout_errors:
                        raise timeout_errors[0]
                except BaseException:
                    await self._cancel_worker_tasks(project, [running for _, running in tasks])
                    raise
                finally:
                    for task, running in tasks:
                        if running.done():
                            project.worker_tasks.pop(task.task_id, None)
                await self.timeout_enforcer.check_team_timeout(project.project_id)
                for (task, running), value in zip(tasks, gathered):
                    if isinstance(value, WorkerResult):
                        result = value
                    elif isinstance(value, asyncio.CancelledError):
                        result = WorkerResult(
                            worker_id=project.worker_ids.get(task.task_id, "cancelled"), task_id=task.task_id,
                            role=task.role, status="cancelled", summary="Worker cancelled.",
                            unresolved=["Worker cancelled before completion."],
                        )
                    else:
                        safe_error = redact_sensitive_text(value)
                        result = WorkerResult(
                            worker_id=project.worker_ids.get(task.task_id, "failed"), task_id=task.task_id,
                            role=task.role, status="failed", summary="Worker failed.", error=safe_error,
                            unresolved=[safe_error],
                        )
                    completed[task.task_id] = result
                    results.append(result)
            return results
        finally:
            await self._cancel_worker_tasks(project)


    async def _execute_worker(
        self,
        project: ActiveProject,
        brief: ProjectBrief,
        task: WorkerTaskSpec,
        dependency_results: list[WorkerResult],
    ) -> WorkerResult:
        worker_id = await self.registry.register_worker(
            task.role,
            max_turns=self.config.runtime.max_turns,
            project_id=project.project_id,
        )
        project.worker_ids[task.task_id] = worker_id
        task_id = await self.registry.register_task(worker_id)
        try:
            current = asyncio.current_task()
            if current is not None:
                await self.registry.attach_async_task(worker_id, current)
            if not await self.registry.start_task(task_id):
                return WorkerResult(
                    worker_id=worker_id,
                    task_id=task.task_id,
                    role=task.role,
                    status="cancelled",
                    summary="Worker was cancelled before it started.",
                    unresolved=["Worker cancelled before start."],
                )
            role = project.role_catalog.get(task.role) or get_role_by_name(task.role)
            if role is None:
                raise RuntimeError(f"Unknown worker role: {task.role}")
            dependency_text = "\n".join(
                f"- {result.task_id}: {result.summary[:600]}" for result in dependency_results
            ) or "[none]"
            requirements = [
                requirement.model_dump()
                for requirement in brief.requirements
                if requirement.id in task.requirement_ids
            ]
            acceptance_criteria = [criterion.model_dump() for criterion in brief.acceptance_criteria]
            if self.config.memory.enabled:
                role_memory_summary = self._worker_memory_summary(task.role, project.project_id)
            else:
                role_memory_summary = {}
            worker_prompt = (
                "You are an internal specialist worker. Complete only the bounded assignment below. "
                "Do not contact the user, create a team, or request raw conversation history. Return a "
                "compact WorkerOutput with conclusions, evidence, artifacts, and unresolved problems. "
                "For every listed acceptance criterion, return an AcceptanceResult using the exact criterion_id. "
                "Set status=passed only when the tool results prove it; include the raw or concise tool output "
                "and the criterion's verification method.\n\n"
                f"ROLE: {role.title}\n"
                f"ASSIGNMENT:\n{task.model_dump_json(indent=2)}\n\n"
                f"RELEVANT REQUIREMENTS:\n{json.dumps(requirements, indent=2)}\n\n"
                f"ACCEPTANCE CRITERIA AND VERIFICATION METHODS:\n{json.dumps(acceptance_criteria, indent=2)}\n\n"
                f"PROJECT CONSTRAINTS:\n{json.dumps(brief.constraints)}\n\n"
                f"ROLE MEMORY SUMMARY:\n{json.dumps(role_memory_summary, default=str)}\n\n"
                f"DEPENDENCY SUMMARIES:\n{dependency_text}\n"
            )
            turn_budget = WorkerTurnBudget(
                max_turns=int(getattr(self.config.runtime, "max_turns", 10)),
                worker_id=worker_id,
                before_turn=lambda: self.timeout_enforcer.check_team_timeout(project.project_id),
                on_turn=lambda kind: self.timeout_enforcer.increment_turn(worker_id, kind=kind),
            )
            tool_executor = WorkerToolExecutor(
                self.artifact_store.workspace,
                artifact_store=self.artifact_store,
                project_id=project.project_id,
                worker_id=worker_id,
                allowed_tools=(
                    list(task.tools)
                    if getattr(self.config.tools, "enabled", True)
                    else []
                ),
                max_file_bytes=int(getattr(self.config.tools, "max_file_bytes", 1_000_000)),
                max_tool_calls=int(getattr(self.config.tools, "max_tool_calls", 8)),
                acceptance_criteria=acceptance_criteria,
                turn_budget=turn_budget,
            )
            factory_kwargs = {
                "role": role,
                "model": self.worker_model,
                "task_description": task.objective,
                "additional_context": (
                    f"Requirement IDs: {', '.join(task.requirement_ids)}\n"
                    f"Deliverable: {task.deliverable}\n"
                    f"Tools: {', '.join(task.tools) or 'built-in worker tools'}"
                ),
            }
            if "tool_executor" in inspect.signature(create_worker_agent).parameters:
                factory_kwargs["tool_executor"] = tool_executor
            worker_agent = create_worker_agent(**factory_kwargs)
            raw = await enforce_worker_lifecycle(
                worker_id,
                worker_agent.run(worker_prompt, response_format=WorkerOutput),
                self.canceller,
                self.timeout_enforcer,
                self.concurrency_limiter,
                task_id=task_id,
                team_id=project.project_id,
            )
            output = redact_model(
                raw if isinstance(raw, WorkerOutput) else WorkerOutput.model_validate(raw)
            )
            tool_evidence = [
                f"{item.name}: {item.output[:600]}"
                for item in tool_executor.results
                if item.ok and item.output
            ]
            existing_artifact_paths = {item.path for item in output.artifacts}
            output = output.model_copy(update={
                "evidence": list(dict.fromkeys([*output.evidence, *tool_evidence])),
                "artifacts": [
                    *output.artifacts,
                    *[item for item in tool_executor.artifacts if item.path not in existing_artifact_paths],
                ],
            })
            result = self._worker_result(worker_id, task, output)
            await self.registry.complete_task(task_id, result.model_dump())
            self.logger.info("worker_complete project=%s worker=%s role=%s", project.project_id, worker_id, task.role)
            return result
        except asyncio.CancelledError:
            await self.registry.cancel_task(task_id, error="worker cancelled")
            await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            raise
        except ProjectTimeoutError:
            await self.registry.fail_task(task_id, "project timeout")
            await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            raise
        except asyncio.TimeoutError:
            await self.registry.fail_task(task_id, "worker timeout")
            await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            return WorkerResult(
                worker_id=worker_id, task_id=task.task_id, role=task.role, status="failed",
                summary="Worker exceeded its deadline.", unresolved=["Worker timeout."], error="timeout",
            )
        except Exception as exc:
            safe_error = redact_sensitive_text(exc)
            await self.registry.fail_task(task_id, safe_error)
            await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            self.logger.warning("worker_failed project=%s worker=%s error=%s", project.project_id, worker_id, exc)
            return WorkerResult(
                worker_id=worker_id, task_id=task.task_id, role=task.role, status="failed",
                summary="Worker failed before producing a validated result.", unresolved=[safe_error], error=safe_error,
            )
        finally:
            try:
                await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            except Exception:
                self.logger.exception("worker_process_cleanup_failed project=%s worker=%s", project.project_id, worker_id)

    @staticmethod
    def _worker_result(worker_id: str, task: WorkerTaskSpec, output: WorkerOutput) -> WorkerResult:
        status = output.status
        evidence = list(output.evidence)
        if output.summary and output.summary not in evidence:
            evidence.append(output.summary)
        requirement_results = [
            result.model_copy(update={"requirement_id": result.requirement_id})
            for result in output.requirement_results
            if result.requirement_id in task.requirement_ids
        ]
        if not requirement_results and status == "complete":
            requirement_results = [
                RequirementResult(requirement_id=rid, status="satisfied", evidence=evidence)
                for rid in task.requirement_ids
            ]
        elif not requirement_results:
            requirement_results = [
                RequirementResult(requirement_id=rid, status="partially_satisfied", evidence=evidence)
                for rid in task.requirement_ids
            ]
        return WorkerResult(
            worker_id=worker_id,
            task_id=task.task_id,
            role=task.role,
            status=status,
            summary=output.summary,
            requirement_results=requirement_results,
            acceptance_results=list(output.acceptance_results),
            artifacts=output.artifacts,
            findings=output.findings,
            unresolved=output.unresolved,
            evidence=evidence,
        )

    def _build_completion_report(
        self, brief: ProjectBrief, results: list[WorkerResult], project: ActiveProject
    ) -> CompletionReport:
        by_requirement: dict[str, RequirementResult] = {}
        by_acceptance: dict[str, AcceptanceResult] = {}
        rank = {"satisfied": 3, "partially_satisfied": 2, "unsatisfied": 1, "not_applicable": 0}
        acceptance_rank = {"passed": 2, "failed": 1, "not_verified": 0}
        artifacts = []
        findings: list[str] = []
        unresolved: list[str] = []
        for result in results:
            for requirement_result in result.requirement_results:
                previous = by_requirement.get(requirement_result.requirement_id)
                if previous is None or rank[requirement_result.status] > rank[previous.status]:
                    by_requirement[requirement_result.requirement_id] = requirement_result
            for acceptance_result in result.acceptance_results:
                previous_acceptance = by_acceptance.get(acceptance_result.criterion_id)
                if previous_acceptance is None or acceptance_rank[acceptance_result.status] > acceptance_rank[previous_acceptance.status]:
                    by_acceptance[acceptance_result.criterion_id] = acceptance_result
            artifacts.extend(result.artifacts)
            findings.extend(result.findings)
            unresolved.extend(result.unresolved)
        for requirement in brief.requirements:
            by_requirement.setdefault(
                requirement.id,
                RequirementResult(
                    requirement_id=requirement.id,
                    status="unsatisfied",
                    evidence=[],
                    notes="No worker returned evidence for this requirement.",
                ),
            )
        for criterion in brief.acceptance_criteria:
            by_acceptance.setdefault(
                criterion.id,
                AcceptanceResult(
                    criterion_id=criterion.id,
                    status="not_verified",
                    evidence=[],
                    verification_method=None,
                ),
            )
        required = [result for requirement, result in ((r, by_requirement[r.id]) for r in brief.requirements) if requirement.required]
        criteria = list(by_acceptance.values())
        if project.cancel_event.is_set():
            status = "blocked"
        elif required and all(result.status == "satisfied" and result.evidence for result in required) and all(
            result.status == "passed" and result.evidence for result in criteria
        ) and not any(
            result.status in {"failed", "cancelled"} for result in results
        ):
            status = "complete"
        elif results:
            status = "partial"
        else:
            status = "blocked"
        unique_artifacts = []
        seen_paths = set()
        for artifact in artifacts:
            if artifact.path not in seen_paths:
                unique_artifacts.append(artifact)
                seen_paths.add(artifact.path)
        if any(result.status in {"failed", "cancelled"} for result in results):
            unresolved.append("One or more workers did not complete.")
        summary = (
            f"Team Manager completed {sum(result.status == 'complete' for result in results)} "
            f"of {len(results)} worker assignment(s); report status: {status}."
        )
        return CompletionReport(
            status=status,
            summary=summary,
            requirement_results=list(by_requirement.values()),
            acceptance_results=criteria,
            important_findings=list(dict.fromkeys(findings)),
            decisions=[f"Assigned roles: {', '.join(dict.fromkeys(result.role for result in results)) or 'none'}"],
            artifacts=unique_artifacts,
            risks=[],
            unresolved_items=list(dict.fromkeys(unresolved)),
            recommended_next_actions=(
                ["Address the unresolved requirements and rerun the project."] if status != "complete" else []
            ),
        )

    async def _repair_report(
        self,
        brief: ProjectBrief,
        report: CompletionReport,
        issues: list[str],
    ) -> CompletionReport | None:
        attempts = max(0, int(getattr(self.config.runtime, "report_repair_attempts", 1)))
        for _ in range(attempts):
            prompt = (
                "Repair this CompletionReport against the ProjectBrief. Return only a typed CompletionReport. "
                "Do not invent artifacts or verification evidence; mark anything not proven as unresolved or "
                "partial.\n\n"
                f"PROJECT BRIEF:\n{brief.model_dump_json(indent=2)}\n\n"
                f"CURRENT REPORT:\n{report.model_dump_json(indent=2)}\n\n"
                f"VALIDATION ISSUES:\n{json.dumps(issues)}"
            )
            try:
                repaired = await self.manager.run(prompt, response_format=CompletionReport)
                repaired = (
                    repaired
                    if isinstance(repaired, CompletionReport)
                    else CompletionReport.model_validate(repaired)
                )
                return redact_model(repaired)
            except Exception as exc:
                self.logger.warning("manager report repair failed: %s", exc)
        return None

    async def _render_principal_response(
        self, brief: ProjectBrief, report: CompletionReport, validation_issues: list[str]
    ) -> str:
        prompt = (
            "Compose the final response to the user from this validated project result. "
            "Do not expose raw worker transcripts or internal chatter. Do not claim complete if the "
            "status is partial, blocked, or failed. Mention important artifacts, risks, and unresolved "
            "items when present.\n\n"
            f"PROJECT BRIEF:\n{brief.model_dump_json(indent=2)}\n\n"
            f"COMPLETION REPORT:\n{report.model_dump_json(indent=2)}\n\n"
            f"VALIDATION ISSUES:\n{json.dumps(validation_issues)}"
        )
        try:
            raw = await self.principal.run(prompt)
            response = redact_sensitive_text(raw if isinstance(raw, str) else str(raw or ""))
            if response.strip():
                return response.strip()
        except Exception as exc:
            self.logger.warning("principal report rendering failed: %s", exc)
        return self._format_report_fallback(report)

    @staticmethod
    def _format_report_fallback(report: CompletionReport) -> str:
        prefix = {
            "complete": "Completed.",
            "partial": "Partially completed.",
            "blocked": "Blocked before completion.",
            "failed": "The project failed.",
        }[report.status]
        lines = [prefix, report.summary]
        if report.artifacts:
            lines.append("Artifacts: " + ", ".join(artifact.path for artifact in report.artifacts))
        if report.important_findings:
            lines.append("Findings:\n" + "\n".join(f"- {item}" for item in report.important_findings[:8]))
        if report.unresolved_items:
            lines.append("Unresolved:\n" + "\n".join(f"- {item}" for item in report.unresolved_items[:8]))
        return "\n\n".join(lines)

    def _persist_report_best_effort(self, project_id: str, report: CompletionReport) -> None:
        """Persist a terminal report without replacing the original failure."""
        try:
            self.project_store.write_json(project_id, "completion_report", report.model_dump())
        except Exception:
            self.logger.exception("terminal_report_persistence_failed project=%s", project_id)

    async def _cleanup_project(self, project: ActiveProject, *, cancel: bool) -> None:
        """Reap one project without touching resources owned by other projects."""
        worker_ids = list(dict.fromkeys(project.worker_ids.values()))
        active_tasks = [task for task in project.worker_tasks.values() if not task.done()]
        if cancel or active_tasks:
            await self._cancel_project_tree(project, cancel_root=False)
        for worker_id in worker_ids:
            try:
                await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            except Exception:
                self.logger.exception("project_process_cleanup_failed project=%s worker=%s", project.project_id, worker_id)
        try:
            await self.registry.cleanup(worker_ids)
        except Exception:
            self.logger.exception("project_registry_cleanup_failed project=%s", project.project_id)
        for worker_id in worker_ids:
            try:
                await self.process_manager.terminate_worker_processes(worker_id, hard=True)
            except Exception:
                self.logger.exception("project_process_reap_failed project=%s worker=%s", project.project_id, worker_id)
        project.worker_tasks.clear()
        project.worker_ids.clear()

    async def _cancel_project_tree(self, project: ActiveProject, *, cancel_root: bool = True) -> None:
        project.cancel_event.set()
        await self._cancel_worker_tasks(project)
        for worker_id in list(project.worker_ids.values()):
            await self.canceller.cancel_worker(worker_id, hard=True)
        if cancel_root:
            root = self._run_tasks.get(project.session_id)
            if root is not None and root is not asyncio.current_task() and not root.done():
                root.cancel()

    def validate_report(self, brief: ProjectBrief, report: CompletionReport) -> tuple[bool, list[str]]:
        return validate_completion_report(brief, report)


    async def _record_memory(
        self,
        project_id: str,
        brief: ProjectBrief,
        plan: ManagerPlan,
        report: CompletionReport,
        worker_results: list[WorkerResult],
    ) -> None:
        if not self.config.memory.enabled:
            return

        compact_context = {
            "project_id": project_id,
            "objective": brief.objective[:240],
            "status": report.status,
            "roles": list(dict.fromkeys(result.role for result in worker_results)),
        }
        try:
            if self.principal_memory:
                self.principal_memory.add_decision(
                    f"Project {project_id} returned {report.status}.", compact_context
                )
                AgentMemory("principal", self.config.runtime.state_dir / "memory", scope=project_id).add_decision(
                    f"Project {project_id} returned {report.status}.", compact_context
                )
            if self.manager_memory:
                manager_context = {"roles": compact_context["roles"], "status": report.status}
                self.manager_memory.add_decision(
                    f"Used {len(plan.tasks)} worker assignment(s) for project {project_id}.", manager_context
                )
                AgentMemory("manager", self.config.runtime.state_dir / "memory", scope=project_id).add_decision(
                    f"Used {len(plan.tasks)} worker assignment(s) for project {project_id}.", manager_context
                )
            for result in worker_results:
                memory = self._role_memories.setdefault(
                    result.role, AgentMemory(f"worker-{result.role}", self.config.runtime.state_dir / "memory")
                )
                if result.summary:
                    memory.add_lesson(result.summary[:500], category="execution")
                    AgentMemory(
                        f"worker-{result.role}", self.config.runtime.state_dir / "memory", scope=project_id
                    ).add_lesson(result.summary[:500], category="execution")
            if self.knowledge_store is not None:
                for finding in report.important_findings[:10]:
                    finding_text = finding.strip()
                    stable_id = hashlib.sha256(
                        f"{project_id}:manager:finding:{finding_text.casefold()}".encode("utf-8")
                    ).hexdigest()
                    if self.knowledge_store.get(stable_id) is not None:
                        continue
                    self.knowledge_store.add(KnowledgeRecord(
                        id=stable_id,
                        topic=finding_text[:160],
                        summary=finding_text[:300],
                        details=finding_text[:800],
                        source_agent="team-manager",
                        project=project_id,
                        tags=["finding", *( ["artifact"] if report.artifacts else [] )],
                        confidence=0.8,
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                        source_run=f"{project_id}:manager",
                        source_artifact=(report.artifacts[0].path if report.artifacts else None),
                        source_artifacts=list(dict.fromkeys(
                            artifact.path for artifact in report.artifacts if artifact.path
                        )),
                    ))
        except Exception:
            self.logger.exception("memory_recording_failed project=%s", project_id)

        if self.curator is not None:
            try:
                await self.curator.run_curator(
                    completion_report=report,
                    worker_summaries={result.worker_id: result.summary for result in worker_results},
                    project_id=project_id,
                )
            except Exception:
                self.logger.exception("curator_failed project=%s", project_id)

    @staticmethod
    def _compact_history(conversation: Iterable[dict[str, Any]]) -> str:
        lines = []
        for message in list(conversation)[-12:]:
            role = str(message.get("role", "user"))
            content = str(message.get("content", ""))
            lines.append(f"{role}: {content[:1200]}")
        return "\n".join(lines)[-12000:]
