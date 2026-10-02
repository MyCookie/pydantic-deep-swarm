"""Model discovery for OpenAI-compatible endpoints."""

from __future__ import annotations

from typing import Any
import time
from urllib.parse import urlsplit, urlunsplit

import httpx


class ModelDiscoveryError(RuntimeError):
    """Raised when the inference endpoint cannot identify one model safely."""

    def __init__(self, detail: str, reason: str = "model_unavailable"):
        super().__init__(detail)
        self.reason = reason


def safe_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        authority = host + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return "[invalid endpoint]"


class OpenAIModelDiscovery:
    """Discover the model advertised by an OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        api_key: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def list_models(self) -> list[str]:
        """Return model IDs advertised by ``GET /v1/models``."""
        try:
            # Build independently of client defaults, which may contain control
            # headers or cookies. A neutral Auth also prevents HTTPX deriving
            # BasicAuth from endpoint userinfo or using injected client auth.
            request = httpx.Request("GET", f"{self.base_url}/models",
                                    extensions={"timeout": httpx.Timeout(self._timeout).as_dict()})
            response = self._client.send(request, auth=httpx.Auth(), follow_redirects=False)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelDiscoveryError(
                f"unable to query models at {safe_endpoint(self.base_url)}/models"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ModelDiscoveryError(
                "OpenAI-compatible /models response must contain a list-valued 'data' field", "invalid_model_response"
            )

        models: list[str] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip() or item["id"] == "auto":
                raise ModelDiscoveryError(
                    "OpenAI-compatible /models response contains an invalid or reserved ID", "invalid_model_response"
                )
            if item["id"] not in models:
                models.append(item["id"])
        return models

    @staticmethod
    def require_single(models: list[str]) -> str:
        """Select one model or fail rather than guessing between deployments."""
        if not models:
            raise ModelDiscoveryError("endpoint advertises no models", "model_not_found")
        if len(models) != 1:
            joined = ", ".join(models)
            raise ModelDiscoveryError(
                "endpoint advertises multiple models; pass an explicit model", "ambiguous_model"
            )
        return models[0]

    def detect_model(self) -> str:
        """Discover and return the sole advertised model."""
        return self.require_single(self.list_models())


# Backward-compatible import for existing control-plane callers.
VLLMModelDiscovery = OpenAIModelDiscovery


def resolve_role_models(config, *, deadline: float | None = None):
    from ..config import active_roles
    effective = config.model_copy(deep=True)
    catalogs = {}
    for role in active_roles(effective):
        item = effective.models[role]
        if not item.base_url:
            raise ModelDiscoveryError(f"{role} requires a model endpoint", "model_unavailable")
        endpoint = item.base_url.rstrip("/")
        if endpoint not in catalogs:
            remaining = 5.0 if deadline is None else min(5.0, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("model startup deadline expired")
            discovery = OpenAIModelDiscovery(endpoint, timeout=remaining)
            try:
                catalogs[endpoint] = discovery.list_models()
            finally:
                discovery.close()
        selection = item.model or "auto"
        if selection == "auto":
            item.model = OpenAIModelDiscovery.require_single(catalogs[endpoint])
        elif selection not in catalogs[endpoint]:
            raise ModelDiscoveryError(f"{role} explicit model is not advertised", "model_not_found")
    if "curator" not in active_roles(effective):
        curator = effective.models["curator"]
        worker = effective.models["worker"]
        original_worker = config.models["worker"]
        if curator.model == original_worker.model and curator.base_url == original_worker.base_url:
            curator.model = worker.model
    return effective, catalogs
