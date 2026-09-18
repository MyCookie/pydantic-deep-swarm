"""Dynamic worker factory for creating role-specific agents."""

from typing import Any
from .contracts import SpecialistRole
from .simple_agent import SimpleAgent
from .worker_tools import ToolAwareWorkerAgent, WorkerToolExecutor
from dataclasses import dataclass
import re
import uuid


@dataclass
class AgentRole:
    """Definition of a worker role."""
    name: str
    title: str
    objective: str
    perspective: str
    responsibilities: list[str]
    constraints: list[str]
    deliverable: str
    definition_of_done: list[str]
    tools: list[str]


def role_from_definition(definition: SpecialistRole) -> AgentRole:
    """Convert a validated project-local role into the prompt-facing dataclass."""
    return AgentRole(**definition.model_dump())


# Predefined professional roles
PROFESSIONAL_ROLES: dict[str, AgentRole] = {
    "systems-engineer": AgentRole(
        name="systems-engineer",
        title="Senior Systems Engineer",
        objective="Design and implement robust system architecture",
        perspective="Think in terms of scalability, failure modes, reproducibility, and operational consequences",
        responsibilities=[
            "Design system architecture",
            "Identify failure modes and edge cases",
            "Define error handling strategies",
            "Ensure reproducibility and testing"
        ],
        constraints=[
            "Must handle concurrent operations safely",
            "Must provide clear error messages",
            "Must include logging and observability"
        ],
        deliverable="Working system with tests and documentation",
        definition_of_done=[
            "All requirements satisfied",
            "Tests pass",
            "Error handling in place",
            "Documentation complete"
        ],
        tools=["filesystem", "shell", "git", "testing"]
    ),
    "software-engineer": AgentRole(
        name="software-engineer",
        title="Software Engineer",
        objective="Implement clean, maintainable code",
        perspective="Think in terms of code quality, testing, and maintainability",
        responsibilities=[
            "Write clean, well-documented code",
            "Implement unit tests",
            "Follow best practices",
            "Handle errors gracefully"
        ],
        constraints=[
            "Code must be tested",
            "Must follow project conventions",
            "Must include appropriate error handling"
        ],
        deliverable="Working code with tests",
        definition_of_done=[
            "Code implements requirements",
            "Tests pass",
            "Code reviewed",
            "Documentation complete"
        ],
        tools=["filesystem", "shell", "git", "testing"]
    ),
    "researcher": AgentRole(
        name="researcher",
        title="Research Analyst",
        objective="Gather and synthesize relevant information",
        perspective="Think in terms of source reliability, completeness, and bias",
        responsibilities=[
            "Find relevant information",
            "Evaluate source quality",
            "Synthesize findings",
            "Identify gaps and ambiguities"
        ],
        constraints=[
            "Must cite sources",
            "Must evaluate reliability",
            "Must identify uncertainties"
        ],
        deliverable="Research summary with sources",
        definition_of_done=[
            "All relevant sources found",
            "Sources evaluated",
            "Findings synthesized",
            "Gaps identified"
        ],
        tools=["web-search", "filesystem", "notes"]
    ),
    "reviewer": AgentRole(
        name="reviewer",
        title="Technical Reviewer",
        objective="Verify work against requirements",
        perspective="Think critically about completeness, correctness, and edge cases",
        responsibilities=[
            "Review work against requirements",
            "Identify missing items",
            "Check for edge cases",
            "Validate acceptance criteria"
        ],
        constraints=[
            "Must be thorough",
            "Must provide evidence",
            "Must identify specific issues"
        ],
        deliverable="Review report with findings",
        definition_of_done=[
            "All requirements checked",
            "Evidence provided",
            "Issues identified",
            "Recommendations given"
        ],
        tools=["filesystem", "testing", "review"]
    ),
    "tool-engineer": AgentRole(
        name="tool-engineer",
        title="Tool Engineer",
        objective="Build missing capabilities and tools",
        perspective="Think in terms of reusability, interface design, and integration",
        responsibilities=[
            "Identify missing capabilities",
            "Design tool interfaces",
            "Implement tools",
            "Test and document tools"
        ],
        constraints=[
            "Must follow tool architecture priority",
            "Must be reusable",
            "Must be well-documented"
        ],
        deliverable="Working tool with documentation",
        definition_of_done=[
            "Tool implements capability",
            "Tests pass",
            "Documentation complete",
            "Integration verified"
        ],
        tools=["filesystem", "shell", "git", "package-inspection"]
    ),
    "designer": AgentRole(
        name="designer",
        title="UX Designer",
        objective="Design user-friendly interfaces and experiences",
        perspective="Think in terms of user goals, usability, and information hierarchy",
        responsibilities=[
            "Understand user goals",
            "Design information hierarchy",
            "Define interaction patterns",
            "Consider accessibility"
        ],
        constraints=[
            "Must be user-centered",
            "Must be accessible",
            "Must be consistent"
        ],
        deliverable="Design specification",
        definition_of_done=[
            "User goals addressed",
            "Usability verified",
            "Accessibility considered",
            "Documentation complete"
        ],
        tools=["filesystem", "design-tools", "prototyping"]
    )
}


def create_worker_agent(
    role: AgentRole,
    model: Any,
    task_description: str,
    additional_context: str | None = None,
    tool_executor: WorkerToolExecutor | None = None,
) -> SimpleAgent | ToolAwareWorkerAgent:
    """Create a worker agent with role-specific instructions."""

    # Build responsibilities list
    resp_lines = [f"- {r}" for r in role.responsibilities]
    resp_str = "\n".join(resp_lines)

    # Build constraints list
    cons_lines = [f"- {c}" for c in role.constraints]
    cons_str = "\n".join(cons_lines)

    # Build definition of done list
    done_lines = [f"- {d}" for d in role.definition_of_done]
    done_str = "\n".join(done_lines)

    # Build tools list
    tools_str = ", ".join(role.tools)

    # Build additional context section
    context_section = ""
    if additional_context:
        context_section = "ADDITIONAL CONTEXT:\n" + additional_context + "\n\n"

    system_prompt = f"""You are a {role.title}.

OBJECTIVE:
{role.objective}

PERSPECTIVE:
{role.perspective}

RESPONSIBILITIES:
{resp_str}

CONSTRAINTS:
{cons_str}

DELIVERABLE:
{role.deliverable}

DEFINITION OF DONE:
{done_str}

ROLE CAPABILITIES (descriptive only; not executable Hermes tools):
{tools_str}

EXECUTABLE CAPABILITY POLICY:
Only the bounded worker tools explicitly supplied by the runtime are executable.
Hermes-native terminal, skills, MCP, and connector surfaces are unavailable.

TASK:
{task_description}

{context_section}IMPORTANT:
- Think like a professional in your role
- Focus on your specific responsibilities
- Do not create additional teams (nesting depth = 0)
- Provide evidence for your conclusions
- Report completion status clearly"""

    agent_name = f"Worker-{role.name}-{uuid.uuid4().hex[:4]}"
    if tool_executor is not None:
        return ToolAwareWorkerAgent(model, system_prompt, tool_executor)
    return SimpleAgent(model, system_prompt, agent_name)


def get_available_roles() -> list[str]:
    """Get list of available role names."""
    return list(PROFESSIONAL_ROLES.keys())


def get_role_by_name(name: str) -> AgentRole | None:
    """Get a role definition by name."""
    return PROFESSIONAL_ROLES.get(name)


def suggest_roles_for_task(task_description: str) -> list[str]:
    """Suggest which roles might be appropriate for a task."""
    task_lower = task_description.lower()
    tokens = set(re.findall(r"[a-z0-9]+", task_lower))
    suggested = []

    def has_keyword(keyword: str) -> bool:
        """Match whole words so short role tokens do not hit inside other words."""
        if " " in keyword:
            return keyword in task_lower
        return keyword in tokens

    if any(has_keyword(kw) for kw in ["design", "ui", "ux", "interface", "user"]):
        suggested.append("designer")

    if any(has_keyword(kw) for kw in ["research", "investigate", "find", "gather"]):
        suggested.append("researcher")

    if any(has_keyword(kw) for kw in ["review", "verify", "validate", "check"]):
        suggested.append("reviewer")

    if any(has_keyword(kw) for kw in ["tool", "capability", "missing", "build tool"]):
        suggested.append("tool-engineer")

    if any(has_keyword(kw) for kw in ["system", "architecture", "infrastructure"]):
        suggested.append("systems-engineer")

    if any(has_keyword(kw) for kw in ["build", "code", "implement", "develop", "program"]):
        suggested.append("software-engineer")

    if not suggested:
        suggested.append("software-engineer")

    return suggested
