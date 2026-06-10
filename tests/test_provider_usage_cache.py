"""
Tests for cache-token fields in provider usage dicts.

All providers must populate cache_read_tokens (and cache_creation_tokens where
applicable) in the normalized usage dict returned by _parse_response.  This
makes prompt-cache activity observable without inspecting raw API payloads.

Tests bypass provider __init__ (which requires real API keys) by using
object.__new__ — _parse_response accesses no instance state.

Semantics reference:
  Anthropic  — input_tokens EXCLUDES cache tokens (additive)
               cache_creation_input_tokens: tokens written this call (1.25x billing)
               cache_read_input_tokens:     tokens served from cache (0.1x billing)
  OpenAI     — prompt_tokens INCLUDES cached_tokens (subset relationship)
               prompt_tokens_details.cached_tokens: cache-hit portion (0.5x billing)
               cache_creation is automatic/unreported; always 0 in usage dict
  Gemini     — input INCLUDES cache (subset); cached_content_token_count is the hit portion
  DeepSeek   — reports via prompt_cache_hit_tokens (top-level, not prompt_tokens_details);
               inherited OpenAI parser will emit cache_read_tokens=0 even on a cache hit
               (known limitation — requires a DeepSeek-specific override in a later slice)
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------

def _make_anthropic_provider():
    import sys
    import types

    if "anthropic" not in sys.modules:
        anthropic_mod = types.ModuleType("anthropic")
        anthropic_mod.RateLimitError = RuntimeError
        anthropic_mod.AuthenticationError = RuntimeError
        anthropic_mod.PermissionDeniedError = RuntimeError
        anthropic_mod.APIError = RuntimeError
        anthropic_mod.AsyncAnthropic = object
        sys.modules["anthropic"] = anthropic_mod

    from orchestrator.providers.claude import AnthropicProvider
    return object.__new__(AnthropicProvider)


class _FakeAnthropicUsage:
    def __init__(self, input=0, output=0, cache_creation=0, cache_read=0):
        self.input_tokens = input
        self.output_tokens = output
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, usage, content):
        self.usage = usage
        self.content = content


class AnthropicCacheUsageTests(unittest.TestCase):
    def test_cache_fields_zero_when_no_caching(self):
        provider = _make_anthropic_provider()
        response = _FakeAnthropicResponse(
            usage=_FakeAnthropicUsage(input=100, output=50),
            content=[_FakeTextBlock("hello")],
        )
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 0)
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)

    def test_cache_creation_tokens_captured(self):
        provider = _make_anthropic_provider()
        response = _FakeAnthropicResponse(
            usage=_FakeAnthropicUsage(input=100, output=50, cache_creation=800),
            content=[_FakeTextBlock("cached")],
        )
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 800)
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)
        # input_tokens is separate from cache tokens (additive, not subset)
        self.assertEqual(result["usage"]["input"], 100)

    def test_cache_read_tokens_captured(self):
        provider = _make_anthropic_provider()
        response = _FakeAnthropicResponse(
            usage=_FakeAnthropicUsage(input=20, output=30, cache_read=800),
            content=[_FakeTextBlock("warm cache")],
        )
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_read_tokens"], 800)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 0)

    def test_both_cache_fields_present_simultaneously(self):
        """First call writes part of the cache, reads another part."""
        provider = _make_anthropic_provider()
        response = _FakeAnthropicResponse(
            usage=_FakeAnthropicUsage(input=50, output=10, cache_creation=400, cache_read=200),
            content=[_FakeTextBlock("mixed")],
        )
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 400)
        self.assertEqual(result["usage"]["cache_read_tokens"], 200)

    def test_cache_fields_absent_on_usage_object_without_attrs(self):
        """Older SDK versions may not expose cache fields — defaults to 0."""
        provider = _make_anthropic_provider()
        bare_usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        response = _FakeAnthropicResponse(
            usage=bare_usage,
            content=[_FakeTextBlock("no cache attrs")],
        )
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 0)
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _make_openai_provider():
    from orchestrator.providers.openai_provider import OpenAIProvider
    return object.__new__(OpenAIProvider)


def _fake_openai_response(prompt_tokens=100, completion_tokens=50, cached_tokens=0):
    details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens is not None else None
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=details,
    )
    message = SimpleNamespace(tool_calls=None, content="hello")
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class OpenAICacheUsageTests(unittest.TestCase):
    def test_cache_fields_zero_when_no_caching(self):
        provider = _make_openai_provider()
        result = provider._parse_response(_fake_openai_response(cached_tokens=0))
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)
        self.assertEqual(result["usage"]["cache_creation_tokens"], 0)

    def test_cache_read_tokens_captured(self):
        provider = _make_openai_provider()
        result = provider._parse_response(_fake_openai_response(
            prompt_tokens=1000, cached_tokens=800,
        ))
        self.assertEqual(result["usage"]["cache_read_tokens"], 800)
        # prompt_tokens INCLUDES cached_tokens (subset semantics)
        self.assertEqual(result["usage"]["input"], 1000)

    def test_cache_creation_always_zero(self):
        """OpenAI does not report cache-write tokens; must always be 0."""
        provider = _make_openai_provider()
        result = provider._parse_response(_fake_openai_response(cached_tokens=500))
        self.assertEqual(result["usage"]["cache_creation_tokens"], 0)

    def test_no_prompt_tokens_details_field(self):
        """Older API responses / models without caching have no details object."""
        provider = _make_openai_provider()
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, prompt_tokens_details=None)
        message = SimpleNamespace(tool_calls=None, content="hi")
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)
        result = provider._parse_response(response)
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)

    def test_cache_read_tokens_on_tool_call_response(self):
        """Cache fields are present on tool-call responses too."""
        import json
        provider = _make_openai_provider()
        tc = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments='{"path":"/tmp/x"}'))
        details = SimpleNamespace(cached_tokens=300)
        usage = SimpleNamespace(prompt_tokens=500, completion_tokens=20, prompt_tokens_details=details)
        message = SimpleNamespace(tool_calls=[tc], content=None)
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)
        result = provider._parse_response(response)
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["usage"]["cache_read_tokens"], 300)


# ---------------------------------------------------------------------------
# Gemini (REST path via _parse_rest_response)
# ---------------------------------------------------------------------------

def _make_gemini_provider(monkeypatch=None):
    """Return a GeminiProvider instance without real API keys."""
    import sys
    import types

    # Stub out google.generativeai so the import inside gemini.py succeeds without the SDK.
    # gemini.py accesses protos.Type.STRING/INTEGER/NUMBER/BOOLEAN/ARRAY/OBJECT at module level.
    if "google" not in sys.modules:
        google_mod = types.ModuleType("google")
        sys.modules["google"] = google_mod
    if "google.generativeai" not in sys.modules:
        type_stub = types.SimpleNamespace(
            STRING=1, INTEGER=2, NUMBER=3, BOOLEAN=4, ARRAY=5, OBJECT=6,
        )
        protos_stub = types.SimpleNamespace(
            Type=type_stub,
            FunctionResponse=object,
        )
        genai_mod = types.ModuleType("google.generativeai")
        genai_mod.GenerativeModel = object
        genai_mod.GenerationConfig = lambda **kw: None
        genai_mod.configure = lambda **kw: None
        genai_mod.protos = protos_stub
        sys.modules["google.generativeai"] = genai_mod
        import google
        google.generativeai = genai_mod  # type: ignore[attr-defined]

    from orchestrator.providers.gemini import GeminiProvider
    p = object.__new__(GeminiProvider)
    return p


class GeminiCacheUsageTests(unittest.TestCase):
    def _provider(self):
        return _make_gemini_provider()

    def test_cache_read_zero_when_no_caching(self):
        provider = self._provider()
        result = provider._parse_rest_response({
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        })
        self.assertEqual(result["usage"]["cache_read_tokens"], 0)

    def test_cache_read_tokens_from_rest_response(self):
        provider = self._provider()
        result = provider._parse_rest_response({
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 40,
                "cachedContentTokenCount": 800,
            },
            "candidates": [{"content": {"parts": [{"text": "cached result"}]}}],
        })
        self.assertEqual(result["usage"]["cache_read_tokens"], 800)
        # input INCLUDES cache (subset semantics for Gemini too)
        self.assertEqual(result["usage"]["input"], 1000)

    def test_cache_read_zero_when_key_absent(self):
        """usageMetadata without cachedContentTokenCount defaults to 0."""
        provider = self._provider()
        result = provider._parse_rest_response({
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10},
            "candidates": [{"content": {"parts": [{"text": "no cache key"}]}}],
        })
        self.assertEqual(result["usage"].get("cache_read_tokens", 0), 0)


if __name__ == "__main__":
    unittest.main()
