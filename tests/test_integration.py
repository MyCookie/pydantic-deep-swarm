"""Integration tests with stub model."""

import pytest
import asyncio
from pathlib import Path
import tempfile
import os

from agent_team.contracts import ProjectBrief, CompletionReport, Requirement, AcceptanceCriterion, RequirementResult, ArtifactResult
from agent_team.config import Config
from agent_team.worker_factory import create_worker_agent, get_role_by_name, suggest_roles_for_task
from agent_team.memory.knowledge import KnowledgeStore, KnowledgeRecord
from agent_team.runtime.lifecycle import TaskRegistry, ProcessManager
from agent_team.runtime.cancellation import HardCanceller, TimeoutEnforcer, ConcurrencyLimiter


class StubModel:
    """Stub model for deterministic testing."""

    def __init__(self, response: str = "Stub response"):
        self.response = response
        self.call_count = 0

    async def run(self, prompt: str, **kwargs):
        """Return stub response."""
        self.call_count += 1
        return self.response


class TestProjectBriefValidation:
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
        with pytest.raises(Exception):
            ProjectBrief(
                requirements=[],
                desired_output="test"
            )


class TestCompletionReportValidation:
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
        with pytest.raises(Exception):
            CompletionReport(
                status="invalid_status",
                summary="test"
            )


class TestWorkerFactory:
    """Test worker factory."""

    def test_create_worker(self):
        """Test creating a worker agent."""
        role = get_role_by_name("software-engineer")
        assert role is not None
        assert role.title == "Software Engineer"

    def test_suggest_roles(self):
        """Test role suggestion."""
        roles = suggest_roles_for_task("Build a web scraper")
        assert "software-engineer" in roles

    def test_suggest_researcher(self):
        """Test researcher role suggestion."""
        roles = suggest_roles_for_task("Research market trends")
        assert "researcher" in roles

    def test_suggest_reviewer(self):
        """Test reviewer role suggestion."""
        roles = suggest_roles_for_task("Review the code for bugs")
        assert "reviewer" in roles


class TestKnowledgeStore:
    """Test knowledge store."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_add_record(self, temp_db):
        """Test adding a knowledge record."""
        store = KnowledgeStore(temp_db)

        record = KnowledgeRecord(
            id="test-1",
            topic="Test Topic",
            summary="Test Summary",
            details="Test Details",
            source_agent="test-agent",
            project="test-project",
            tags=["test", "example"],
            confidence=0.9,
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
        )

        store.add(record)

        # Verify it was added
        retrieved = store.get("test-1")
        assert retrieved is not None
        assert retrieved.topic == "Test Topic"

    def test_search_by_topic(self, temp_db):
        """Test searching by topic."""
        store = KnowledgeStore(temp_db)

        # Add records
        for i in range(3):
            record = KnowledgeRecord(
                id=f"test-{i}",
                topic=f"Topic {i}",
                summary="Summary",
                details="Details",
                source_agent="test",
                tags=[],
                confidence=1.0,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:00:00",
            )
            store.add(record)

        # Search
        results = store.search(topic="Topic 1")
        assert len(results) == 1
        assert results[0].id == "test-1"

    def test_supersede(self, temp_db):
        """Test superseding records."""
        store = KnowledgeStore(temp_db)

        # Add old record
        old = KnowledgeRecord(
            id="old-1",
            topic="Topic",
            summary="Old",
            details="Details",
            source_agent="test",
            tags=[],
            confidence=1.0,
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
        )
        store.add(old)

        # Add new record
        new = KnowledgeRecord(
            id="new-1",
            topic="Topic",
            summary="New",
            details="Details",
            source_agent="test",
            tags=[],
            confidence=1.0,
            created_at="2024-01-02T00:00:00",
            updated_at="2024-01-02T00:00:00",
        )
        store.add(new)

        # Supersede
        store.supersede("old-1", "new-1")

        # Verify
        old_retrieved = store.get("old-1")
        assert old_retrieved.supersedes == "new-1"


class TestLifecycle:
    """Test lifecycle management."""

    @pytest.mark.asyncio
    async def test_register_worker(self):
        """Test registering a worker."""
        registry = TaskRegistry(max_workers=5)

        worker_id = await registry.register_worker(role="test-role")
        assert worker_id is not None
        assert len(worker_id) > 0

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        """Test concurrency limiting."""
        limiter = ConcurrencyLimiter(max_concurrent=2)

        # Acquire both slots
        await limiter.acquire()
        await limiter.acquire()

        # Should be at limit
        assert limiter.current_count == 2

        # Release
        await limiter.release()
        await limiter.release()

        assert limiter.current_count == 0


class TestConfig:
    """Test configuration."""

    def test_env_config(self):
        """Test configuration from environment."""
        os.environ["LLM_BASE_URL"] = "http://test.example.com"
        os.environ["PRINCIPAL_MODEL"] = "test-model"

        config = Config.from_env()

        assert config.models["principal"].base_url == "http://test.example.com"
        assert config.models["principal"].model == "test-model"

        # Clean up
        del os.environ["LLM_BASE_URL"]
        del os.environ["PRINCIPAL_MODEL"]

    def test_default_config(self):
        """Test default configuration."""
        config = Config()

        assert config.runtime.max_workers == 6
        assert config.runtime.max_concurrent_workers == 3
        assert config.runtime.nesting_depth == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
