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
    native_tool_calling: bool = False
    lean_system_prompt: bool = False

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
    Names: "claude", "openai", "gemini", "deepseek", "browser", "ollama"
    Also: "browser:claude", "browser:chatgpt", … (sets browser_provider.preferred_site).

    Gemini auth contract:
      - legacy/dev: GOOGLE_API_KEY or config["api_key"]
      - Kim OAuth: Tauri injects KIM_GOOGLE_ACCESS_TOKEN (+ optional expiry/project)
        after refreshing the OS-keychain refresh token. Python never sees refresh tokens.
    """
    name = name.lower().strip()
    if name.startswith("browser:"):
        sub = name.split(":", 1)[1].strip().lower()
        merged = dict(config)
        bp = dict(config.get("browser_provider") or {})
        if sub:
            bp["preferred_site"] = sub
        merged["browser_provider"] = bp
        merged["provider"] = "browser"
        from orchestrator.providers.browser_provider import BrowserProvider

        return BrowserProvider(merged)
    if name == "claude":
        from orchestrator.providers.claude import AnthropicProvider
        return AnthropicProvider(config)
    if name == "openai":
        from orchestrator.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(config)
    if name == "gemini":
        # GeminiProvider chooses exactly one auth path: API key for legacy/dev,
        # or Kim Google OAuth via short-lived bearer env/config. Keep this branch
        # API-first; Browser: Gemini remains `browser:gemini`.
        from orchestrator.providers.gemini import GeminiProvider
        return GeminiProvider(config)
    if name == "deepseek":
        from orchestrator.providers.deepseek import DeepSeekProvider
        return DeepSeekProvider(config)
    if name == "ollama":
        from orchestrator.providers.ollama import OllamaProvider
        return OllamaProvider(config)
    if name == "browser":
        from orchestrator.providers.browser_provider import BrowserProvider
        return BrowserProvider(config)
    raise ValueError(f"Unknown provider: {name!r}. Choose from: claude, openai, gemini, deepseek, browser, ollama")
