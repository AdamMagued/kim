import importlib
import sys
import types
from typing import Any

import pytest


def install_google_stubs(monkeypatch):
    """Install google.* stubs via monkeypatch.setitem so they are RESTORED
    after each test — the previous version overwrote sys.modules["google"]
    permanently, shadowing any real google namespace package for every later
    test in the session. (gemini.py is REST-based today, so these stubs are
    belt-and-braces for older import paths.)"""
    # Typed Any: these are dynamic stub modules we patch attributes onto, which
    # a bare ModuleType annotation would reject (reportAttributeAccessIssue).
    google_mod: Any = types.ModuleType("google")
    genai_mod: Any = types.ModuleType("google.generativeai")
    protos_mod: Any = types.ModuleType("google.generativeai.protos")

    class Type:
        STRING = 1
        INTEGER = 2
        NUMBER = 3
        BOOLEAN = 4
        ARRAY = 5
        OBJECT = 6

    class Schema:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.properties = {}
            self.required = []
            self.enum = []
            self.items = types.SimpleNamespace(CopyFrom=lambda other: None)

    protos_mod.Type = Type
    protos_mod.Schema = Schema
    protos_mod.Content = lambda **kwargs: types.SimpleNamespace(**kwargs)
    protos_mod.Part = lambda **kwargs: types.SimpleNamespace(**kwargs)
    protos_mod.Blob = lambda **kwargs: types.SimpleNamespace(**kwargs)
    protos_mod.Tool = lambda **kwargs: types.SimpleNamespace(**kwargs)
    protos_mod.FunctionDeclaration = lambda **kwargs: types.SimpleNamespace(**kwargs)

    genai_mod.protos = protos_mod
    genai_mod.configure = lambda **kwargs: None
    genai_mod.GenerationConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    genai_mod.GenerativeModel = lambda **kwargs: types.SimpleNamespace(**kwargs)

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai.protos", protos_mod)


def load_module(monkeypatch):
    install_google_stubs(monkeypatch)
    sys.modules.pop("orchestrator.providers.gemini", None)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("KIM_GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT", raising=False)
    return importlib.import_module("orchestrator.providers.gemini")


def test_requires_exactly_one_auth_path(monkeypatch):
    gemini = load_module(monkeypatch)
    with pytest.raises(EnvironmentError, match="not configured"):
        gemini.GeminiProvider({})

    monkeypatch.setenv("GOOGLE_API_KEY", "dev-key")
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN", "oauth-token")
    with pytest.raises(EnvironmentError, match="ambiguous"):
        gemini.GeminiProvider({})


def test_oauth_request_uses_bearer_contract_without_refresh_token(monkeypatch):
    gemini = load_module(monkeypatch)
    provider = gemini.GeminiProvider({
        "gemini_auth_mode": "oauth",
        "oauth_access_token": "ya29.short-lived",
        "oauth_access_token_expires_at": 4_102_444_800,
        "google_cloud_project": "kim-user-project",
        "model": {"gemini": "gemini-2.0-flash"},
    })

    assert provider._auth_mode == "oauth"
    assert provider._quota_project == "kim-user-project"
    token = provider._oauth_access_token_provider()
    assert token.token == "ya29.short-lived"

    body = provider._to_rest_request(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }],
        system="You are Kim.",
    )

    assert body["systemInstruction"]["parts"][0]["text"] == "You are Kim."
    assert body["contents"][0]["parts"] == [{"text": "hello"}]
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "read_file"
    assert declaration["parameters"]["properties"]["path"]["type"] == "STRING"
    assert "refresh" not in repr(body).lower()


def test_parse_rest_response_preserves_usage_and_tool_calls(monkeypatch):
    gemini = load_module(monkeypatch)
    provider = gemini.GeminiProvider({"gemini_auth_mode": "oauth", "oauth_access_token": "t"})

    parsed = provider._parse_rest_response({
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
        "candidates": [{"content": {"parts": [{"functionCall": {"name": "bash", "args": {"cmd": "ls"}}}]}}],
    })

    assert parsed == {
        "type": "tool_call",
        "tool": "bash",
        "args": {"cmd": "ls"},
        # H2: narration accompanying a tool call is preserved (empty here).
        "content": "",
        # cache_creation_tokens is now always present (0 for Gemini) so the
        # usage shape matches the other providers (finding 3.8).
        "usage": {"input": 10, "output": 4, "cache_creation_tokens": 0, "cache_read_tokens": 0},
    }


# ── F-B-1: OAuth HTTP failures must classify by status, not by the "oauth"
# label word that classify_provider_error's auth check would otherwise match. ──

def _make_http_error(code, body):
    import io
    import urllib.error
    return urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        code,
        "err",
        {},  # type: ignore[arg-type]
        io.BytesIO(body.encode("utf-8")),
    )


def _oauth_provider(gemini):
    return gemini.GeminiProvider({
        "gemini_auth_mode": "oauth",
        "oauth_access_token": "ya29.tok",
        "oauth_access_token_expires_at": 4_102_444_800,
        "google_cloud_project": "kim-shared",
        "model": {"gemini": "gemini-2.0-flash"},
    })


def test_oauth_429_is_retryable_rate_limit_not_auth(monkeypatch):
    from orchestrator.providers.base import classify_provider_error, ProviderError

    gemini = load_module(monkeypatch)
    provider = _oauth_provider(gemini)

    def _raise(*_a, **_k):
        raise _make_http_error(429, '{"error":{"message":"quota","status":"RESOURCE_EXHAUSTED"}}')

    monkeypatch.setattr(gemini.urllib.request, "urlopen", _raise)

    with pytest.raises(ProviderError) as ei:
        provider._post_rest({}, {"Authorization": "Bearer x"}, "Gemini OAuth API")
    err = ei.value
    assert err.code == "rate_limit"
    assert err.retryable is True
    # The classifier must agree (ProviderError passes through) — proving the
    # "oauth" label no longer poisons it into a non-retryable auth failure.
    classified = classify_provider_error(err)
    assert classified.code == "rate_limit" and classified.retryable is True


@pytest.mark.parametrize("code", [500, 502, 503, 529])
def test_oauth_5xx_is_retryable_server_error_not_auth(monkeypatch, code):
    from orchestrator.providers.base import classify_provider_error, ProviderError

    gemini = load_module(monkeypatch)
    provider = _oauth_provider(gemini)

    def _raise(*_a, **_k):
        raise _make_http_error(code, '{"error":{"message":"overloaded","status":"UNAVAILABLE"}}')

    monkeypatch.setattr(gemini.urllib.request, "urlopen", _raise)

    with pytest.raises(ProviderError) as ei:
        provider._post_rest({}, {"Authorization": "Bearer x"}, "Gemini OAuth API")
    err = ei.value
    assert err.code == "server_error" and err.retryable is True
    classified = classify_provider_error(err)
    assert classified.code == "server_error" and classified.retryable is True


def test_oauth_403_still_classifies_as_auth(monkeypatch):
    from orchestrator.providers.base import classify_provider_error

    gemini = load_module(monkeypatch)
    provider = _oauth_provider(gemini)

    def _raise(*_a, **_k):
        raise _make_http_error(403, '{"error":{"message":"permission denied","status":"PERMISSION_DENIED"}}')

    monkeypatch.setattr(gemini.urllib.request, "urlopen", _raise)

    with pytest.raises(RuntimeError) as ei:
        provider._post_rest({}, {"Authorization": "Bearer x"}, "Gemini OAuth API")
    classified = classify_provider_error(ei.value)
    assert classified.code == "auth" and classified.retryable is False


# ── F-INH-1: the OAuth token provider must re-read a desktop-managed token
# FILE every call, so a long-lived session survives token rotation (os.environ
# is frozen at process spawn). ──

def test_env_token_provider_reads_refreshable_file(monkeypatch, tmp_path):
    import json as _json
    import time as _time

    gemini = load_module(monkeypatch)
    token_file = tmp_path / "google_token.json"
    token_file.write_text(_json.dumps({
        "access_token": "ya29.first",
        "expires_at": _time.time() + 3600,
    }))
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN_FILE", str(token_file))
    # A stale/expired env token must NOT win over the fresh file token.
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN", "ya29.stale-env")

    provider = gemini.EnvOAuthAccessTokenProvider()
    assert provider().token == "ya29.first"

    # Desktop rotates the token in place — the SAME provider picks it up with
    # no respawn (the frozen-env bug this fixes would keep returning the old).
    token_file.write_text(_json.dumps({
        "access_token": "ya29.rotated",
        "expires_at": _time.time() + 3600,
    }))
    assert provider().token == "ya29.rotated"


def test_env_token_provider_falls_back_to_env_when_file_absent(monkeypatch, tmp_path):
    gemini = load_module(monkeypatch)
    missing = tmp_path / "not_written_yet.json"
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN_FILE", str(missing))
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN", "ya29.env-fallback")

    provider = gemini.EnvOAuthAccessTokenProvider()
    assert provider().token == "ya29.env-fallback"


def test_env_token_provider_file_expiry_still_enforced(monkeypatch, tmp_path):
    import json as _json
    import time as _time

    gemini = load_module(monkeypatch)
    token_file = tmp_path / "google_token.json"
    token_file.write_text(_json.dumps({
        "access_token": "ya29.almost-dead",
        "expires_at": _time.time() + 10,  # inside the 60s guard band
    }))
    monkeypatch.setenv("KIM_GOOGLE_ACCESS_TOKEN_FILE", str(token_file))

    provider = gemini.EnvOAuthAccessTokenProvider()
    with pytest.raises(EnvironmentError, match="expired or too close"):
        provider()
