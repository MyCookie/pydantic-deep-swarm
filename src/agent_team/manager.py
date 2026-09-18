"""Team Manager role: project decomposition, staffing, and report synthesis."""

from typing import Any

from .contracts import ManagerPlan, ProjectBrief, WorkerTaskSpec
from .simple_agent import SimpleAgent
from .worker_factory import get_available_roles, suggest_roles_for_task


MANAGER_SYSTEM_PROMPT = """You are the Team Manager. You receive one validated ProjectBrief, never a raw user transcript.

Own execution autonomously:
- decompose the objective into the smallest useful set of tasks;
- choose specialist roles dynamically from the available role catalog;
- identify dependencies and parallelize only independent tasks;
- provide each worker a focused objective, relevant requirements/context, tools, and definition of done;
- add a reviewer when verification materially improves reliability;
- stop work that is no longer useful;
- never let workers create sub-teams (maximum hierarchy is Principal → Manager → Workers);
- return compact evidence-backed handoffs, not reasoning transcripts.

Your output must be a typed ManagerPlan. Do not speak to the user directly and do not claim a requirement
is satisfied without evidence.
"""


def create_manager_agent(model: Any) -> SimpleAgent:
    """Create the Team Manager model wrapper."""
    return SimpleAgent(model, MANAGER_SYSTEM_PROMPT, "TeamManager")


def fallback_manager_plan(brief: ProjectBrief, max_workers: int) -> ManagerPlan:
    """Deterministic staffing fallback when structured Manager output is unavailable."""
    role_names = suggest_roles_for_task(brief.objective)
    role_names = list(dict.fromkeys(role_names))[: max(1, max_workers)]
    tasks: list[WorkerTaskSpec] = []
    requirement_ids = [requirement.id for requirement in brief.requirements]
    for index, role_name in enumerate(role_names):
        role = role_name if role_name in get_available_roles() else "software-engineer"
        task_id = f"task-{index + 1}"
        tasks.append(
            WorkerTaskSpec(
                task_id=task_id,
                role=role,
                objective=brief.objective,
                requirement_ids=requirement_ids,
                relevant_context=list(brief.relevant_context),
                dependencies=[],
                deliverable=brief.desired_output,
                definition_of_done=[criterion.description for criterion in brief.acceptance_criteria],
                tools=[],
            )
        )
    return ManagerPlan(
        tasks=tasks,
        execution_notes=["Used deterministic role selection because structured manager output was unavailable."],
        review_required="review" in brief.objective.lower() or "verify" in brief.objective.lower(),
    )
