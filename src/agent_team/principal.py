"""Principal role: user-facing intake, bounded handoff, and report validation."""

from typing import Any

from .contracts import (
    CompletionReport,
    ProjectBrief,
    Requirement,
    RequirementResult,
)
from .simple_agent import SimpleAgent


PRINCIPAL_SYSTEM_PROMPT = """You are the Principal: the user's trusted interface to a capable internal team.

Speak like a composed, pragmatic, technically literate colleague. Understand the user's actual goal,
reason about ambiguity, and ask only when it materially changes the outcome or authorization.

For substantial work, create a ProjectBrief for the Team Manager. The brief must contain an explicit
objective, requirements, constraints, acceptance criteria, relevant context, permitted/prohibited
actions, desired output, and unresolved ambiguities. Do not choose individual workers.

For simple requests, answer directly without creating a team. If important information is missing,
ask one focused clarification. Never expose manager/worker chatter or pass raw conversation to workers.
When a CompletionReport returns, compare it to the original brief and do not claim unmet requirements
are complete. Report meaningful decisions, risks, artifacts, and next actions naturally.
"""


def create_principal_agent(model: Any) -> SimpleAgent:
    """Create the Principal model wrapper."""
    return SimpleAgent(model, PRINCIPAL_SYSTEM_PROMPT, "Principal")


def build_default_brief(user_input: str, session_id: str, *, context: list[str] | None = None) -> ProjectBrief:
    """Deterministic fallback brief used when local structured output is unavailable."""
    objective = user_input.strip() or "Complete the user's requested task"
    return ProjectBrief(
        objective=objective,
        requirements=[Requirement(id="req-1", description="Complete the requested objective", required=True)],
        constraints=[],
        acceptance_criteria=[],
        relevant_context=list(context or []),
        context_refs=[f"session:{session_id}"],
        permitted_actions=[],
        prohibited_actions=["Expose internal worker conversations to the user"],
        desired_output="A concise result with evidence and unresolved risks",
        unresolved_ambiguities=[],
    )


def looks_substantial(user_input: str) -> bool:
    """Conservative deterministic fallback for direct-vs-delegated routing."""
    text = (user_input or "").lower()
    markers = (
        "build", "implement", "develop", "create", "write", "refactor", "debug", "fix",
        "research", "investigate", "analyze", "compare", "design", "deploy", "configure",
        "migrate", "review", "audit", "test", "architecture", "system",
    )
    return len(text.split()) >= 24 or any(marker in text for marker in markers)


def validate_completion_report(
    brief: ProjectBrief,
    report: CompletionReport,
) -> tuple[bool, list[str]]:
    """Validate every requirement, acceptance criterion, and artifact claim."""
    issues: list[str] = []
    if report.status in {"failed", "blocked"}:
        issues.append(f"Report status is '{report.status}'")

    brief_requirements = {requirement.id: requirement for requirement in brief.requirements}
    report_results = {result.requirement_id: result for result in report.requirement_results}
    brief_criteria = {criterion.id: criterion for criterion in brief.acceptance_criteria}
    acceptance_results = {result.criterion_id: result for result in report.acceptance_results}

    for requirement_id in brief_requirements:
        if requirement_id not in report_results:
            issues.append(f"Missing result for requirement: {requirement_id}")
    for requirement_id in report_results:
        if requirement_id not in brief_requirements:
            issues.append(f"Unexpected result for requirement: {requirement_id}")

    for requirement_id, requirement in brief_requirements.items():
        result: RequirementResult | None = report_results.get(requirement_id)
        if result is None:
            continue
        if requirement.required and result.status in {"unsatisfied", "partially_satisfied"}:
            issues.append(f"Required requirement not fully satisfied: {requirement_id}")
        if result.status in {"satisfied", "partially_satisfied"} and not result.evidence:
            issues.append(f"Requirement {requirement_id} has no evidence")

    for criterion_id, criterion in brief_criteria.items():
        if not criterion.verification_method:
            issues.append(f"Acceptance criterion has no verification method: {criterion_id}")
        result = acceptance_results.get(criterion_id)
        if result is None:
            issues.append(f"Missing verification for acceptance criterion: {criterion_id}")
            continue
        if result.status != "passed":
            issues.append(f"Acceptance criterion not passed: {criterion_id}")
        if not result.evidence:
            issues.append(f"Acceptance criterion has no evidence: {criterion_id}")
        if not result.verification_method:
            issues.append(f"Acceptance criterion has no recorded verification method: {criterion_id}")
    for criterion_id in acceptance_results:
        if criterion_id not in brief_criteria:
            issues.append(f"Unexpected acceptance criterion result: {criterion_id}")

    for artifact in report.artifacts:
        if not artifact.verified:
            issues.append(f"Artifact is not verified: {artifact.path}")

    if report.status == "complete" and report.unresolved_items:
        issues.extend(f"Unresolved: {item}" for item in report.unresolved_items)
    if report.status == "complete" and any(
        result.status != "satisfied" for result in report_results.values() if result.requirement_id in brief_requirements
    ):
        issues.append("Report is marked complete while at least one requirement is not satisfied")
    if report.status == "complete" and any(
        result.status != "passed" for result in acceptance_results.values() if result.criterion_id in brief_criteria
    ):
        issues.append("Report is marked complete while at least one acceptance criterion is not passed")
    return not issues, list(dict.fromkeys(issues))


def validate_report_summary(report: CompletionReport) -> bool:
    """Quick structural validation retained for callers of the original API."""
    return bool(report.summary and report.summary.strip() and report.status in {"complete", "partial", "blocked", "failed"})
