"""Deterministic smoke tests for core Agent Team behavior."""

from agent_team.contracts import (
    AcceptanceCriterion,
    AcceptanceResult,
    ArtifactResult,
    CompletionReport,
    ProjectBrief,
    Requirement,
    RequirementResult,
)
from agent_team.memory.agent_memory import AgentMemory
from agent_team.principal import validate_completion_report
from agent_team.worker_factory import get_role_by_name, suggest_roles_for_task


def test_simple_task_selects_a_valid_worker_role():
    roles = suggest_roles_for_task("Create a Python function to calculate fibonacci numbers")

    assert roles
    assert any(get_role_by_name(role_name) is not None for role_name in roles)


def test_parallel_task_selects_an_appropriate_specialist():
    roles = suggest_roles_for_task("Research Python web frameworks and design a simple API")

    assert {"researcher", "designer"} & set(roles)


def test_completion_report_validation_rejects_missing_requirement():
    brief = ProjectBrief(
        objective="Build a web scraper",
        requirements=[
            Requirement(id="req-1", description="Scrape example.com", required=True),
            Requirement(id="req-2", description="Save to CSV", required=True),
        ],
        constraints=["Rate limit: 1 req/sec"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac-1",
                description="Data extracted correctly",
                verification_method="deterministic fixture check",
            )
        ],
        desired_output="CSV file",
    )
    valid_report = CompletionReport(
        status="complete",
        summary="Task completed successfully",
        requirement_results=[
            RequirementResult(
                requirement_id="req-1",
                status="satisfied",
                evidence=["Scraped 100 items"],
            ),
            RequirementResult(
                requirement_id="req-2",
                status="satisfied",
                evidence=["Saved to output.csv"],
            ),
        ],
        artifacts=[
            ArtifactResult(
                path="output.csv",
                description="Scraped data",
                created_by="worker",
                verified=True,
                verification_method="deterministic fixture check",
            )
        ],
        acceptance_results=[
            AcceptanceResult(
                criterion_id="ac-1",
                status="passed",
                evidence=["fixture data checked"],
                verification_method="deterministic fixture check",
            )
        ],
    )

    is_valid, issues = validate_completion_report(brief, valid_report)
    assert is_valid, issues

    invalid_report = CompletionReport(
        status="complete",
        summary="Task completed",
        requirement_results=[
            RequirementResult(
                requirement_id="req-1",
                status="satisfied",
                evidence=["Scraped 100 items"],
            )
        ],
    )
    is_valid, issues = validate_completion_report(brief, invalid_report)

    assert not is_valid
    assert "Missing result for requirement: req-2" in issues


def test_memory_persists_across_instances(tmp_path):
    memory = AgentMemory("test-agent", tmp_path)
    memory.set_preference("language", "English")
    memory.add_decision("Chose Python over JavaScript", {"context": "performance"})
    memory.add_goal("Learn async programming")

    restored = AgentMemory("test-agent", tmp_path)

    assert restored.get_preference("language") == "English"
    assert len(restored.get_decisions()) == 1
    assert len(restored.get_goals()) == 1
