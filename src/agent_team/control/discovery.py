"""Model discovery for OpenAI-compatible endpoints."""

from __future__ import annotations

from typing import Any

import httpx


class ModelDiscoveryError(RuntimeError):
    """Raised when the inference endpoint cannot identify one model safely."""


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
        self._client = client or httpx.Client(timeout=timeout)

    def list_models(self) -> list[str]:
        """Return model IDs advertised by ``GET /v1/models``."""
        try:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
            response = self._client.get(f"{self.base_url}/models", headers=headers)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelDiscoveryError(
                f"unable to query models at {self.base_url}/models: {exc}"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ModelDiscoveryError(
                "OpenAI-compatible /models response must contain a list-valued 'data' field"
            )

        models: list[str] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ModelDiscoveryError(
                    "OpenAI-compatible /models response contains an entry without a string 'id'"
                )
            models.append(item["id"])
        return models

    @staticmethod
    def require_single(models: list[str]) -> str:
        """Select one model or fail rather than guessing between deployments."""
        if not models:
            raise ModelDiscoveryError("endpoint advertises no models")
        if len(models) != 1:
            joined = ", ".join(models)
            raise ModelDiscoveryError(
                f"endpoint advertises multiple models ({joined}); pass an explicit model"
            )
        return models[0]

    def detect_model(self) -> str:
        """Discover and return the sole advertised model."""
        return self.require_single(self.list_models())


# Backward-compatible import for existing control-plane callers.
VLLMModelDiscovery = OpenAIModelDiscovery
