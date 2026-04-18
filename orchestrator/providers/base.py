"""
Abstract provider interface + factory.

All providers accept canonical messages and tools and return a normalized
response dict so the agent loop is provider-agnostic.

Canonical message format:
    {"role": "user"|"assistant", "content": str | list[ContentItem]}

ContentItem:
    {"type": "text",  "text": "..."}
    {"type": "image", "data": "<base64>", "media_type": "image/png"}

Return value:
    {"type": "tool_call", "tool": str, "args": dict}
  | {"type": "text",      "content": str}
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        """
        Args:
            messages:  Canonical conversation history (user/assistant turns).
            tools:     List of available MCP tools in canonical format:
                       {"name": str, "description": str, "parameters": dict (JSON Schema)}
            system:    System prompt string.

        Returns:
            {"type": "tool_call", "tool": str, "args": dict}
          | {"type": "text", "content": str}
        """


def create_provider(name: str, config: dict) -> BaseProvider:
    """
    Factory — returns a provider instance for the given name.
    Names: "claude", "openai", "gemini", "deepseek", "browser"
    """
    name = name.lower().strip()
    if name == "claude":
        from orchestrator.providers.claude import AnthropicProvider
        return AnthropicProvider(config)
    if name == "openai":
        from orchestrator.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(config)
    if name == "gemini":
        from orchestrator.providers.gemini import GeminiProvider
        return GeminiProvider(config)
    if name == "deepseek":
        from orchestrator.providers.deepseek import DeepSeekProvider
        return DeepSeekProvider(config)
    if name == "browser":
        from orchestrator.providers.browser_provider import BrowserProvider
        return BrowserProvider(config)
    raise ValueError(f"Unknown provider: {name!r}. Choose from: claude, openai, gemini, deepseek, browser")
