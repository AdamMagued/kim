import importlib
import sys
import types

import pytest


def install_google_stubs(monkeypatch):
    """Install google.* stubs via monkeypatch.setitem so they are RESTORED
    after each test — the previous version overwrote sys.modules["google"]
    permanently, shadowing any real google namespace package for every later
    test in the session. (gemini.py is REST-based today, so these stubs are
    belt-and-braces for older import paths.)"""
    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.generativeai")
    protos_mod = types.ModuleType("google.generativeai.protos")

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
