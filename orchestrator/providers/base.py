"""
Abstract provider interface + factory.

All providers accept canonical messages and tools and return a normalized
response dict so the agent loop is provider-agnostic.

Canonical message format:
    {"role": "user"|"assistant", "content": str | list[ContentItem]}

ContentItem:
    {"type": "text",  "text": "..."}
    {"type": "image", "data": "<base64>", "media_type": "image/png"}

Return value (see ProviderResponse below):
    {"type": "tool_call", "tool": str, "args": dict}
  | {"type": "text",      "content": str}
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Optional, TypedDict, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stop / finish reason handling (finding 3.1)
# ---------------------------------------------------------------------------
# Providers historically returned only the completion text and dropped the
# stop/finish reason, so a max_tokens-truncated answer was presented as a
# complete one and a safety-blocked reply came back as a silent empty string.
# finalize_text_content() annotates the terminal text with an honest note so
# neither failure masquerades as a finished answer. Reasons are matched
# case-insensitively to cover Anthropic ("max_tokens"), OpenAI ("length",
# "content_filter") and Gemini ("MAX_TOKENS", "SAFETY", …) vocabularies.

_TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "length", "model_length", "max_output_tokens"})
_BLOCK_STOP_REASONS = frozenset({
    "content_filter", "safety", "recitation", "refusal",
    "prohibited_content", "blocklist", "spii", "other",
})

_TRUNCATION_NOTE = (
    "[Response truncated: the model hit its output-token limit before "
    "finishing. Ask for a shorter answer or raise max_tokens.]"
)


def finalize_text_content(content: str, stop_reason: Optional[str]) -> str:
    """Annotate terminal completion text based on why generation stopped.

    - Truncation (max_tokens/length): append a note so a clipped answer is not
      mistaken for a complete one. When nothing was produced, the note stands
      in for the empty string.
    - Block/refusal (content_filter/safety/…): when the model produced no text,
      return an explanatory line instead of a blank reply.
    Normal stops ("stop", "end_turn", "tool_use", None) pass through unchanged.
    """
    content = content or ""
    reason = (stop_reason or "").strip().lower()
    if reason in _TRUNCATION_STOP_REASONS:
        return f"{content}\n\n{_TRUNCATION_NOTE}" if content.strip() else _TRUNCATION_NOTE
    if reason in _BLOCK_STOP_REASONS and not content.strip():
        return (
            f"[No answer produced: the model stopped for reason "
            f"'{stop_reason}' (typically a safety block or filtered content).]"
        )
    return content


# ---------------------------------------------------------------------------
# Typed contracts — providers return one of these; agent consumes typed fields
# ---------------------------------------------------------------------------

class ToolCallResponse(TypedDict):
    """Provider response requesting a tool call."""
    type: Literal["tool_call"]
    tool: str
    args: dict[str, Any]


class TextResponse(TypedDict, total=False):
    """Provider response with a text completion."""
    type: Literal["text"]       # required
    content: str                # required
    usage: dict[str, Any]       # optional — token counts from the provider


# The union type returned by BaseProvider.complete().
# TypedDicts are plain dicts at runtime, so existing code is unaffected.
ProviderResponse = Union[ToolCallResponse, TextResponse]


class ToolResult(TypedDict, total=False):
    """Typed envelope for tool execution results.

    `ok` and `payload` are required; `display_text` is optional.
    Use `ToolResult(ok=True, payload=data)` for successful results and
    `ToolResult(ok=False, payload={"error": msg})` for failures.
    """
    ok: bool            # required
    payload: Any        # required — raw result data
    display_text: str   # optional — human-readable summary for the UI


# ---------------------------------------------------------------------------


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


class ProviderEnvironmentError(EnvironmentError):
    """A provider-environment problem the user must fix (daemon not running,
    model not installed/pulled, nothing configured).

    Subclasses EnvironmentError so existing ``except EnvironmentError``
    call sites keep working, but classify_provider_error() can distinguish it
    from a transient OSError: retrying a stopped daemon or a missing model
    five times under a false "rate limited" banner only hides the actionable
    message (H1).
    """


def classify_provider_error(error: Exception) -> ProviderError:
    """Classify provider exceptions without requiring every provider to wrap them.

    Auth-like failures must be checked before OSError because PermissionError is
    an OSError subclass.  Retrying sign-in/API-key failures wastes time and hides
    the actionable fix from the user.
    """
    if isinstance(error, ProviderError):
        return error

    # Environment problems (daemon stopped, model not installed) need the user
    # to act — never retryable, and checked before every message heuristic so
    # wording like "not running" can't be mistaken for a transient failure.
    if isinstance(error, ProviderEnvironmentError):
        return ProviderError("environment", str(error), retryable=False)

    import json as _json_provider_error
    import re as _re_provider_error

    # A JSONDecodeError is a ValueError subclass, so without this it falls into
    # the invalid_request branch below and is marked non-retryable — but it
    # almost always means a truncated or proxy-garbled response body (e.g.
    # Gemini's REST transport doing json.loads on a mangled 200), which is a
    # transient network failure worth retrying (finding 3.4).
    if isinstance(error, _json_provider_error.JSONDecodeError):
        return ProviderError("network", str(error), retryable=True)

    message = str(error)
    lowered = message.lower()
    error_type = type(error).__name__.lower()

    # Exact-substring markers that are unambiguous on their own.
    auth_markers = (
        "sign in",
        "unauthorized",
        "forbidden",
        "permission denied",
        "api key",
        "apikey",
        "access token",
        "credential",
    )
    # "auth" and "oauth" need word boundaries to avoid false positives such as
    # 'author', 'authority', or 'authenticated' (which is a success state).
    _AUTH_WORD_RE = _re_provider_error.compile(r"\b(auth|oauth)\b")
    if (
        isinstance(error, PermissionError)
        or any(marker in lowered for marker in auth_markers)
        or _AUTH_WORD_RE.search(lowered)
    ):
        return ProviderError("auth", message, retryable=False)

    # Use a digit-bounded pattern for 400 to avoid matching strings like "error 4001".
    if (
        isinstance(error, ValueError)
        or "invalid request" in lowered
        or "bad request" in lowered
        or _re_provider_error.search(r"(?<!\d)400(?!\d)", lowered)
    ):
        return ProviderError("invalid_request", message, retryable=False)

    if "rate" in lowered and "limit" in lowered:
        return ProviderError("rate_limit", message, retryable=True)
    # Digit-bounded like the 400/5xx checks — a bare "429" substring would
    # also match e.g. "error 14290" (L4).
    if _re_provider_error.search(r"(?<!\d)429(?!\d)", lowered) or "ratelimit" in error_type:
        return ProviderError("rate_limit", message, retryable=True)

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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> dict[str, Any]:
        """
        Args:
            messages:  Canonical conversation history (user/assistant turns).
            tools:     List of available MCP tools in canonical format:
                       {"name": str, "description": str, "parameters": dict (JSON Schema)}
            system:    System prompt string.

        Returns:
            ProviderResponse — either ToolCallResponse or TextResponse (see above).
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
    raise ValueError(
        f"Unknown provider: {name!r}. Choose from: claude, openai, gemini, deepseek, browser, ollama, fake"
    )
