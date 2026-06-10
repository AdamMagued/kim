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
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProviderError(Exception):
    """Normalized provider failure metadata.

    Providers still raise their native exceptions today; this wrapper gives the
    agent loop a central, stable classification boundary for retry decisions
    and user-visible status codes.
    """

    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)


def classify_provider_error(error: Exception) -> ProviderError:
    """Classify provider exceptions without requiring every provider to wrap them.

    Auth-like failures must be checked before OSError because PermissionError is
    an OSError subclass.  Retrying sign-in/API-key failures wastes time and hides
    the actionable fix from the user.
    """
    if isinstance(error, ProviderError):
        return error

    message = str(error)
    lowered = message.lower()
    error_type = type(error).__name__.lower()

    auth_markers = (
        "sign in",
        "unauthorized",
        "forbidden",
        "permission denied",
        "api key",
        "apikey",
        "access token",
        "oauth",
        "credential",
        "auth",
    )
    if isinstance(error, PermissionError) or any(marker in lowered for marker in auth_markers):
        return ProviderError("auth", message, retryable=False)

    if isinstance(error, ValueError) or "invalid request" in lowered or "bad request" in lowered or "400" in lowered:
        return ProviderError("invalid_request", message, retryable=False)

    if "rate" in lowered and "limit" in lowered:
        return ProviderError("rate_limit", message, retryable=True)
    if "429" in lowered or "ratelimit" in error_type:
        return ProviderError("rate_limit", message, retryable=True)

    import re as _re_provider_error
    for code in ("500", "502", "503", "529"):
        if _re_provider_error.search(r"(?<![\d])" + code + r"(?![\d])", lowered):
            return ProviderError("server_error", message, retryable=True)
    if "server" in lowered and "error" in lowered:
        return ProviderError("server_error", message, retryable=True)
    if "overloaded" in lowered:
        return ProviderError("server_error", message, retryable=True)

    if isinstance(error, TimeoutError) or "timeout" in lowered:
        return ProviderError("timeout", message, retryable=True)
    if isinstance(error, (ConnectionError, OSError)) or "connection" in lowered:
        return ProviderError("network", message, retryable=True)

    return ProviderError("unknown", message, retryable=False)


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
    Names: "claude", "openai", "gemini", "deepseek", "browser", "ollama", "fake"
    Also: "browser:claude", "browser:chatgpt", … (sets browser_provider.preferred_site).

    KIM_FAKE=1 env var forces the "fake" provider regardless of config/args.

    Gemini auth contract:
      - legacy/dev: GOOGLE_API_KEY or config["api_key"]
      - Kim OAuth: Tauri injects KIM_GOOGLE_ACCESS_TOKEN (+ optional expiry/project)
        after refreshing the OS-keychain refresh token. Python never sees refresh tokens.
    """
    import os
    if os.environ.get("KIM_FAKE", "").strip() not in ("", "0"):
        from orchestrator.providers.fake import FakeProvider
        return FakeProvider()
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
    if name == "fake":
        from orchestrator.providers.fake import FakeProvider
        return FakeProvider()
    raise ValueError(f"Unknown provider: {name!r}. Choose from: claude, openai, gemini, deepseek, browser, ollama, fake")
