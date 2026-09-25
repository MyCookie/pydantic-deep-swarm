"""Simple HTTP-based model wrapper for OpenAI-compatible endpoints."""

import httpx
import json
from typing import Any
from pydantic import BaseModel


class SimpleChatModel:
    """Minimal chat model implementation using HTTP requests.

    Works with any OpenAI-compatible endpoint without requiring
    specific pydantic-ai model classes.
    """

    def __init__(self, base_url: str, model_name: str, api_key: str = ""):
        self.base_url = base_url.rstrip('/')
        self.model_name = model_name
        self.api_key = api_key
        self.endpoint = f"{self.base_url}/chat/completions"

    async def run(self, messages: list[dict], **kwargs) -> str:
        """Send chat completion request and return response."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            **kwargs
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self.endpoint,
                headers=headers,
                json=payload
            )
            response.raise_for_status()

            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content

    async def run_structured(self, messages: list[dict], output_schema: type[BaseModel], **kwargs) -> BaseModel:
        """Send chat completion request and parse response as structured output."""
        # OpenAI-compatible chat templates generally allow only one system message.
        # Merge the schema instruction into an existing system prompt instead of
        # prepending a second system message, which some compatible servers reject.
        schema_instruction = f"Respond only with valid JSON matching this schema: {output_schema.model_json_schema()}"
        formatted_messages = list(messages)
        if formatted_messages and formatted_messages[0].get("role") == "system":
            existing = str(formatted_messages[0].get("content") or "")
            formatted_messages[0] = {
                "role": "system",
                "content": f"{schema_instruction}\n\n{existing}",
            }
        else:
            formatted_messages.insert(0, {"role": "system", "content": schema_instruction})

        response_content = await self.run(formatted_messages, **kwargs)

        # Try to parse JSON
        try:
            # Extract JSON from response (sometimes models add text around it)
            json_start = response_content.find('{')
            json_end = response_content.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response_content[json_start:json_end]
                data = json.loads(json_str)
                return output_schema(**data)
            else:
                raise ValueError("No JSON found in response")
        except (json.JSONDecodeError, ValueError) as e:
            # If parsing fails, try to create from raw response
            return output_schema.model_validate_json(response_content)


def create_simple_model(base_url: str, model_name: str, api_key: str = "") -> SimpleChatModel:
    """Factory function to create a simple chat model."""
    return SimpleChatModel(base_url, model_name, api_key)
