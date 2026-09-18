"""Simple agent execution without pydantic-ai Agent class."""

import httpx
import json
from typing import Any
from pydantic import BaseModel


class SimpleAgent:
    """Minimal agent that uses HTTP model directly."""

    def __init__(self, model: Any, system_prompt: str, name: str = "Agent"):
        self.model = model
        self.system_prompt = system_prompt
        self.name = name

    async def run(self, user_message: str, response_format: type[BaseModel] | None = None) -> Any:
        """Run the agent with a user message."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message}
        ]

        if response_format:
            # Request structured output
            return await self.model.run_structured(messages, response_format)
        else:
            # Request plain text
            return await self.model.run(messages)


def create_simple_agent(model: Any, system_prompt: str, name: str = "Agent") -> SimpleAgent:
    """Factory to create a simple agent."""
    return SimpleAgent(model, system_prompt, name)
