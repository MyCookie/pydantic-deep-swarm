"""Tests for agent team runtime."""

import pytest
from pydantic import ValidationError

from agent_team.contracts import ProjectBrief, CompletionReport, Requirement, AcceptanceCriterion, RequirementResult, ArtifactResult
from agent_team.config import Config


class TestProjectBrief:
    """Test ProjectBrief validation."""

    def test_valid_brief(self):
        """Test creating a valid ProjectBrief."""
        brief = ProjectBrief(
            objective="Build a web scraper",
            requirements=[
                Requirement(id="req-1", description="Scrape example.com", required=True)
            ],
            constraints=["Rate limit: 1 req/sec"],
            acceptance_criteria=[
                AcceptanceCriterion(id="ac-1", description="Data extracted correctly")
            ],
            desired_output="CSV file with scraped data"
        )
        assert brief.objective == "Build a web scraper"
        assert len(brief.requirements) == 1

    def test_missing_objective(self):
        """Test that missing objective raises error."""
        with pytest.raises(ValidationError):
            ProjectBrief(
                requirements=[],
                desired_output="test"
            )

    def test_empty_brief(self):
        """Test minimal valid brief."""
        brief = ProjectBrief(
            objective="Simple task",
            desired_output="Result"
        )
        assert brief.requirements == []
        assert brief.constraints == []


class TestCompletionReport:
    """Test CompletionReport validation."""

    def test_valid_report(self):
        """Test creating a valid CompletionReport."""
        report = CompletionReport(
            status="complete",
            summary="Task completed successfully",
            requirement_results=[
                RequirementResult(
                    requirement_id="req-1",
                    status="satisfied",
                    evidence=["Evidence 1"]
                )
            ],
            artifacts=[
                ArtifactResult(
                    path="/path/to/file.csv",
                    description="Scraped data",
                    created_by="scraper-worker"
                )
            ]
        )
        assert report.status == "complete"
        assert len(report.requirement_results) == 1

    def test_invalid_status(self):
        """Test that invalid status raises error."""
        with pytest.raises(ValidationError):
            CompletionReport(
                status="invalid_status",
                summary="test"
            )

    def test_partial_status(self):
        """Test partial status."""
        report = CompletionReport(
            status="partial",
            summary="Some requirements met",
            requirement_results=[]
        )
        assert report.status == "partial"


class TestConfig:
    """Test configuration loading."""

    def test_default_config(self):
        """Test default configuration."""
        config = Config()
        assert config.runtime.max_workers == 6
        assert config.runtime.max_concurrent_workers == 3
        assert config.runtime.nesting_depth == 0

    def test_env_config(self):
        """Test configuration from environment."""
        import os
        os.environ["LLM_BASE_URL"] = "http://test.example.com"
        os.environ["PRINCIPAL_MODEL"] = "test-model"

        config = Config.from_env()

        assert config.models["principal"].base_url == "http://test.example.com"
        assert config.models["principal"].model == "test-model"

        # Clean up
        del os.environ["LLM_BASE_URL"]
        del os.environ["PRINCIPAL_MODEL"]
