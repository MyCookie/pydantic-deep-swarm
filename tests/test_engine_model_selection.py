"""Model-selection behavior for the model-agnostic runtime."""

from agent_team.config import ModelConfig
from agent_team.engine import AgentTeamEngine
import agent_team.engine as engine_module


def bare_engine() -> AgentTeamEngine:
    engine = object.__new__(AgentTeamEngine)
    engine._discovered_models = {}
    return engine


def test_auto_model_discovers_the_single_advertised_model(monkeypatch):
    calls: list[tuple[str, str]] = []

    class Discovery:
        def __init__(self, base_url: str, *, api_key: str = ""):
            calls.append((base_url, api_key))

        def detect_model(self) -> str:
            return "vendor/example-model"

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "endpoint-token")
    monkeypatch.setattr(engine_module, "OpenAIModelDiscovery", Discovery)

    config = ModelConfig(model="auto", base_url="http://model-service:8000/v1")
    model = bare_engine()._create_model(config)

    assert model.model_name == "vendor/example-model"
    assert config.model == "vendor/example-model"
    assert calls == [("http://model-service:8000/v1", "endpoint-token")]


def test_auto_model_discovery_is_reused_for_roles_on_the_same_endpoint(monkeypatch):
    calls = 0

    class Discovery:
        def __init__(self, base_url: str, *, api_key: str = ""):
            pass

        def detect_model(self) -> str:
            nonlocal calls
            calls += 1
            return "vendor/example-model"

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(engine_module, "OpenAIModelDiscovery", Discovery)
    engine = bare_engine()

    first = engine._create_model(ModelConfig(model="auto", base_url="http://model/v1"))
    second = engine._create_model(ModelConfig(model="auto", base_url="http://model/v1/"))

    assert first.model_name == second.model_name == "vendor/example-model"
    assert calls == 1


def test_explicit_model_bypasses_discovery(monkeypatch):
    class UnexpectedDiscovery:
        def __init__(self, *args, **kwargs):
            raise AssertionError("explicit model must not trigger discovery")

    monkeypatch.setattr(engine_module, "OpenAIModelDiscovery", UnexpectedDiscovery)

    config = ModelConfig(model="vendor/example-model", base_url="http://model/v1")
    model = bare_engine()._create_model(config)

    assert model.model_name == "vendor/example-model"
