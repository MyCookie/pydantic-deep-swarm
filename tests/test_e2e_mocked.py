"""Deterministic end-to-end boundary test without model network calls."""

from agent_team.config import Config
from agent_team.contracts import (
    ArtifactResult,
    CompletionReport,
    ProjectBrief,
    Requirement,
    RequirementResult,
)
from agent_team.memory.agent_memory import AgentMemory
from agent_team.principal import validate_completion_report
from agent_team.worker_factory import get_role_by_name, suggest_roles_for_task


def test_end_to_end_mocked(tmp_path, monkeypatch):
    """Exercise configuration, staffing, report validation, and memory together."""
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setenv("LLM_BASE_URL", "http://model-service:8000/v1")
    monkeypatch.setenv("PRINCIPAL_MODEL", "test-principal")
    monkeypatch.setenv("MANAGER_MODEL", "test-manager")
    monkeypatch.setenv("WORKER_MODEL", "test-worker")

    config = Config.from_env()
    assert config.models["principal"].model == "test-principal"

    objective = "Create a Python function to calculate fibonacci numbers"
    roles = suggest_roles_for_task(objective)
    assert roles
    role = get_role_by_name(roles[0])
    assert role is not None
    assert role.responsibilities

    brief = ProjectBrief(
        objective=objective,
        requirements=[
            Requirement(id="req-1", description="Complete task", required=True)
        ],
        desired_output="Report",
    )
    report = CompletionReport(
        status="complete",
        summary="Task completed successfully",
        requirement_results=[
            RequirementResult(
                requirement_id="req-1",
                status="satisfied",
                evidence=["Test evidence"],
            )
        ],
        artifacts=[
            ArtifactResult(
                path="test.txt",
                description="Test",
                created_by="test",
                verified=True,
                verification_method="deterministic fixture check",
            )
        ],
    )

    is_valid, issues = validate_completion_report(brief, report)
    assert is_valid, issues

    memory = AgentMemory("test-agent", tmp_path / "memory")
    memory.set_preference("test_key", "test_value")
    memory.add_decision("Test decision", {"context": "test"})
    restored = AgentMemory("test-agent", tmp_path / "memory")
    assert restored.get_preference("test_key") == "test_value"
