"""Typed contracts for the Principal/Team Manager boundary.

The Principal and Team Manager exchange these models instead of unconstrained
conversation. Worker records are deliberately compact: summaries and evidence
cross the boundary, not reasoning transcripts.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


WORKER_TOOL_NAMES = frozenset({"list_files", "read_file", "write_file"})


class Requirement(BaseModel):
    """A single project requirement."""

    id: str = Field(..., min_length=1, description="Unique identifier for this requirement")
    description: str = Field(..., min_length=1, description="What needs to be accomplished")
    required: bool = Field(default=True, description="Whether this is mandatory")


class AcceptanceCriterion(BaseModel):
    """How the Principal or Manager can verify a requirement."""

    id: str = Field(..., min_length=1, description="Unique identifier")
    description: str = Field(..., min_length=1, description="What constitutes acceptance")
    verification_method: str | None = Field(default=None, description="How to verify")


class ProjectBrief(BaseModel):
    """Validated project contract passed from Principal to Team Manager."""

    objective: str = Field(..., description="High-level goal")
    requirements: list[Requirement] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    relevant_context: list[str] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    permitted_actions: list[str] = Field(default_factory=list)
    prohibited_actions: list[str] = Field(default_factory=list)
    desired_output: str = Field(..., min_length=1, description="Expected deliverable format")
    unresolved_ambiguities: list[str] = Field(default_factory=list)

    model_config = {"title": "ProjectBrief"}

    @model_validator(mode="after")
    def validate_identifiers(self) -> "ProjectBrief":
        requirement_ids = [item.id.strip() for item in self.requirements]
        criterion_ids = [item.id.strip() for item in self.acceptance_criteria]
        if any(not item for item in requirement_ids):
            raise ValueError("Requirement ids must not be empty")
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("Requirement ids must be unique")
        if any(not item for item in criterion_ids):
            raise ValueError("Acceptance criterion ids must not be empty")
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("Acceptance criterion ids must be unique")
        return self


class PrincipalDecision(BaseModel):
    """Principal's typed intake decision.

    ``answer`` and ``clarify`` terminate at the Principal. ``delegate`` must
    carry a ProjectBrief; the engine rejects an incomplete handoff.
    """

    action: Literal["answer", "delegate", "clarify"]
    response: str = Field(default="", description="User-facing answer or clarification")
    brief: ProjectBrief | None = None
    rationale: str = Field(default="", description="Compact internal rationale")

    model_config = {"title": "PrincipalDecision"}

    @model_validator(mode="after")
    def validate_action_payload(self) -> "PrincipalDecision":
        if self.action == "delegate" and self.brief is None:
            raise ValueError("Delegate decisions must include a ProjectBrief")
        return self


class SpecialistRole(BaseModel):
    """A bounded role definition the Manager may create for one project."""

    name: str = Field(..., pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=48)
    title: str = Field(..., min_length=1)
    objective: str = Field(..., min_length=1)
    perspective: str = "Work from evidence and state uncertainty explicitly"
    responsibilities: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    deliverable: str = "A concise result with evidence"
    definition_of_done: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, tools: list[str]) -> list[str]:
        invalid = sorted(set(tools) - WORKER_TOOL_NAMES)
        if invalid:
            raise ValueError(f"Unknown worker tool(s): {', '.join(invalid)}")
        if len(tools) != len(set(tools)):
            raise ValueError("Role tools must be unique")
        return tools

    @model_validator(mode="after")
    def validate_role_content(self) -> "SpecialistRole":
        if not self.name.strip() or not self.title.strip() or not self.objective.strip():
            raise ValueError("Specialist roles require a name, title, and objective")
        return self


class WorkerTaskSpec(BaseModel):
    """One bounded assignment created by the Team Manager."""

    task_id: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    objective: str = Field(..., min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    relevant_context: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    deliverable: str = "A concise result with evidence"
    definition_of_done: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, tools: list[str]) -> list[str]:
        invalid = sorted(set(tools) - WORKER_TOOL_NAMES)
        if invalid:
            raise ValueError(f"Unknown worker tool(s): {', '.join(invalid)}")
        if len(tools) != len(set(tools)):
            raise ValueError("Task tools must be unique")
        return tools

    @model_validator(mode="after")
    def validate_task_content(self) -> "WorkerTaskSpec":
        if not self.task_id.strip() or not self.role.strip() or not self.objective.strip():
            raise ValueError("Worker tasks require a task_id, role, and objective")
        dependencies = [dependency.strip() for dependency in self.dependencies]
        if any(not dependency for dependency in dependencies):
            raise ValueError("Worker task dependencies must not be empty")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("Worker task dependencies must be unique")
        if self.task_id.strip() in dependencies:
            raise ValueError("Worker tasks cannot depend on themselves")
        return self


class ManagerPlan(BaseModel):
    """Validated staffing/decomposition plan from the Team Manager."""

    tasks: list[WorkerTaskSpec] = Field(default_factory=list)
    specialist_roles: list[SpecialistRole] = Field(default_factory=list)
    execution_notes: list[str] = Field(default_factory=list)
    review_required: bool = False

    model_config = {"title": "ManagerPlan"}

    @model_validator(mode="after")
    def validate_plan_identifiers(self) -> "ManagerPlan":
        role_names = [role.name for role in self.specialist_roles]
        if len(role_names) != len(set(role_names)):
            raise ValueError("Specialist role names must be unique")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Worker task ids must be unique")
        task_id_set = set(task_ids)
        for task in self.tasks:
            missing = [dependency for dependency in task.dependencies if dependency not in task_id_set]
            if missing:
                raise ValueError(
                    f"Worker task {task.task_id!r} has unknown dependency: {missing[0]!r}"
                )
        return self


class RequirementResult(BaseModel):
    """Evidence-backed result for one brief requirement."""

    requirement_id: str
    status: Literal["satisfied", "partially_satisfied", "unsatisfied", "not_applicable"]
    evidence: list[str] = Field(default_factory=list)
    notes: str | None = None


class AcceptanceResult(BaseModel):
    """Evidence that one acceptance criterion was actually verified."""

    criterion_id: str
    status: Literal["passed", "failed", "not_verified"]
    evidence: list[str] = Field(default_factory=list)
    verification_method: str | None = None


class ArtifactResult(BaseModel):
    """A durable artifact with runtime-verifiable provenance."""

    path: str
    description: str
    created_by: str
    sha256: str | None = None
    size_bytes: int | None = None
    verified: bool = False
    verification_method: str | None = None
    artifact_type: Literal["file", "directory", "url", "other"] = "file"


class WorkerOutput(BaseModel):
    """Model-facing worker output without runtime-owned identity fields."""

    status: Literal["complete", "partial", "failed"] = "complete"
    summary: str = ""
    requirement_results: list[RequirementResult] = Field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    artifacts: list[ArtifactResult] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class WorkerResult(BaseModel):
    """Compact worker-to-manager handoff; no raw worker transcript."""

    worker_id: str
    task_id: str
    role: str
    status: Literal["complete", "partial", "failed", "cancelled"]
    summary: str = ""
    requirement_results: list[RequirementResult] = Field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    artifacts: list[ArtifactResult] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    error: str | None = None


class CompletionReport(BaseModel):
    """Validated report returned by Team Manager to Principal."""

    status: Literal["complete", "partial", "blocked", "failed"]
    requirement_results: list[RequirementResult] = Field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    summary: str = Field(..., min_length=1, description="Executive summary")
    important_findings: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactResult] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)

    model_config = {"title": "CompletionReport"}

    @model_validator(mode="after")
    def validate_result_identifiers(self) -> "CompletionReport":
        requirement_ids = [item.requirement_id for item in self.requirement_results]
        criterion_ids = [item.criterion_id for item in self.acceptance_results]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("Completion report requirement results must be unique")
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("Completion report acceptance results must be unique")
        return self
