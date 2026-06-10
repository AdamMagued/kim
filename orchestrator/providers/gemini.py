"""
Google Gemini provider.

All three auth modes call the Generative Language REST API directly
(generativelanguage.googleapis.com) — no Google SDK dependency. The
deprecated google-generativeai package was removed after its EOL notice;
the REST request/response transforms below are the single wire format.

Supported authentication modes:

1. API key (legacy/dev): GOOGLE_API_KEY or config["api_key"], sent via the
   x-goog-api-key header.
2. Kim Google OAuth (shared quota): a short-lived bearer token passed by Tauri
   in KIM_GOOGLE_ACCESS_TOKEN, using Kim's shared project quota via x-goog-user-project.
3. User-owned free-tier project (oauth_user_project): OAuth bearer token +
   user-created Google Cloud project ID, so usage counts against the user's free tier,
   not Kim's shared project.

The three modes are intentionally mutually exclusive so Kim does not accidentally
fall back or switch modes unexpectedly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# Official Gemini OAuth quickstart/cookbook currently documents this narrow
# Generative Language scope for user-managed credentials. Do not broaden it in
# code without checking the Google docs again.
GEMINI_OAUTH_SCOPE = "https://www.googleapis.com/auth/generative-language.retriever"
OAUTH_TOKEN_ENV = "KIM_GOOGLE_ACCESS_TOKEN"
OAUTH_TOKEN_EXPIRY_ENV = "KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT"
OAUTH_QUOTA_PROJECT_ENV = "KIM_GOOGLE_CLOUD_PROJECT"
OAUTH_REQUEST_TIMEOUT_S = 180

# For user-owned free-tier project mode, also supports:
OAUTH_USER_PROJECT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
OAUTH_USER_PROJECT_ENV = "KIM_GOOGLE_USER_PROJECT_ID"


@dataclass(frozen=True)
class _OAuthAccessToken:
    token: str
    expires_at: float | None = None


class EnvOAuthAccessTokenProvider:
    """Reads the short-lived OAuth access token injected by the desktop shell."""

    def __call__(self) -> _OAuthAccessToken:
        token = os.environ.get(OAUTH_TOKEN_ENV, "").strip()
        if not token:
            raise EnvironmentError(
                "Google for Kim is not connected. Sign in with Google in Settings, "
                f"then retry. Missing {OAUTH_TOKEN_ENV}."
            )

        expires_raw = os.environ.get(OAUTH_TOKEN_EXPIRY_ENV, "").strip()
        expires_at: float | None = None
        if expires_raw:
            try:
                expires_at = float(expires_raw)
            except ValueError as exc:
                raise EnvironmentError(f"Invalid {OAUTH_TOKEN_EXPIRY_ENV}; expected epoch seconds.") from exc
            if expires_at <= time.time() + 60:
                raise EnvironmentError("Google access token is expired or too close to expiry. Please reconnect Google for Kim.")  # noqa: E501

        return _OAuthAccessToken(token=token, expires_at=expires_at)


class GeminiProvider(BaseProvider):
    def __init__(self, config: dict):
        models = config.get("model", {})
        self._model_name = models.get("gemini", "gemini-2.0-flash")
        self._max_tokens = int(config.get("max_tokens", 4096))
        self._api_version = str(config.get("gemini_api_version") or os.environ.get("KIM_GEMINI_API_VERSION") or "v1beta")  # noqa: E501
        self._quota_project = str(
            config.get("google_cloud_project")
            or os.environ.get(OAUTH_QUOTA_PROJECT_ENV)
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or ""
        ).strip()

        self._user_project_id = str(
            config.get("user_project_id")
            or os.environ.get(OAUTH_USER_PROJECT_ENV)
            or ""
        ).strip()

        explicit_mode = str(config.get("gemini_auth_mode") or os.environ.get("KIM_GEMINI_AUTH_MODE") or "auto").lower().strip()  # noqa: E501
        api_key = str(config.get("api_key") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        oauth_token = str(config.get("oauth_access_token") or os.environ.get(OAUTH_TOKEN_ENV, "")).strip()
        oauth_provider = config.get("oauth_access_token_provider")

        if explicit_mode not in {"auto", "api_key", "oauth", "oauth_user_project"}:
            raise ValueError("gemini_auth_mode must be one of: auto, api_key, oauth, oauth_user_project")

        has_api_key = bool(api_key)
        has_oauth = bool(oauth_token) or oauth_provider is not None
        has_user_project = bool(self._user_project_id)

        # Check for ambiguous configs
        auth_sources = sum([has_api_key, has_oauth])
        if auth_sources > 1:
            raise EnvironmentError(
                "Gemini auth is ambiguous: configure exactly one of GOOGLE_API_KEY or "
                f"{OAUTH_TOKEN_ENV}/oauth_access_token_provider."
            )

        wants_api_key = explicit_mode == "api_key" or (explicit_mode == "auto" and has_api_key)
        wants_oauth_user_project = explicit_mode == "oauth_user_project"
        wants_oauth = explicit_mode == "oauth" or (explicit_mode == "auto" and has_oauth)

        # Validate oauth_user_project mode requirements
        if wants_oauth_user_project:
            if not has_oauth:
                raise EnvironmentError(
                    "oauth_user_project mode requires a valid OAuth access token. "
                    f"Set {OAUTH_TOKEN_ENV} or sign in with Google in Settings → Gemini → Use Google free tier."
                )
            if not has_user_project:
                raise EnvironmentError(
                    "oauth_user_project mode requires a user Google Cloud project ID. "
                    f"Set {OAUTH_USER_PROJECT_ENV} or configure via Settings."
                )

        if not wants_api_key and not wants_oauth and not wants_oauth_user_project:
            raise EnvironmentError(
                "Gemini auth is not configured. Use Settings → Google for Kim (API), "
                "or set GOOGLE_API_KEY for development."
            )

        self._auth_mode = "oauth_user_project" if wants_oauth_user_project else ("oauth" if wants_oauth else "api_key")
        if self._auth_mode == "api_key":
            self._api_key = api_key
            self._oauth_access_token_provider: Callable[[], _OAuthAccessToken] | None = None
        elif self._auth_mode == "oauth_user_project":
            if oauth_provider is not None and not callable(oauth_provider):
                raise TypeError("oauth_access_token_provider must be callable")
            if oauth_provider is None:
                if oauth_token:
                    expires_at = _parse_optional_expiry(config.get("oauth_access_token_expires_at") or os.environ.get(OAUTH_TOKEN_EXPIRY_ENV))  # noqa: E501

                    def _static_provider_user_project() -> _OAuthAccessToken:
                        if expires_at is not None and expires_at <= time.time() + 60:
                            raise EnvironmentError("Google access token is expired or too close to expiry. Please reconnect Google for Kim.")  # noqa: E501
                        return _OAuthAccessToken(token=oauth_token, expires_at=expires_at)

                    self._oauth_access_token_provider = _static_provider_user_project
                else:
                    self._oauth_access_token_provider = EnvOAuthAccessTokenProvider()
            else:
                self._oauth_access_token_provider = oauth_provider  # type: ignore[assignment]
            # For user-project mode, use the user project ID instead of Kim's quota project
            self._quota_project = self._user_project_id
        else:  # oauth mode
            if oauth_provider is not None and not callable(oauth_provider):
                raise TypeError("oauth_access_token_provider must be callable")
            if oauth_provider is None:
                # If config supplied oauth_access_token directly, expose it via env-compatible closure.
                if oauth_token:
                    expires_at = _parse_optional_expiry(config.get("oauth_access_token_expires_at") or os.environ.get(OAUTH_TOKEN_EXPIRY_ENV))  # noqa: E501

                    def _static_provider() -> _OAuthAccessToken:
                        if expires_at is not None and expires_at <= time.time() + 60:
                            raise EnvironmentError("Google access token is expired or too close to expiry. Please reconnect Google for Kim.")  # noqa: E501
                        return _OAuthAccessToken(token=oauth_token, expires_at=expires_at)

                    self._oauth_access_token_provider = _static_provider
                else:
                    self._oauth_access_token_provider = EnvOAuthAccessTokenProvider()
            else:
                self._oauth_access_token_provider = oauth_provider  # type: ignore[assignment]

        logger.info("GeminiProvider: model=%s auth=%s", self._model_name, self._auth_mode)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict[str, Any]:
        if not messages:
            return {"type": "text", "content": "SYSTEM ERROR: No messages provided."}

        if self._auth_mode in ("oauth", "oauth_user_project"):
            return await self._complete_oauth(messages, tools, system)

        return await self._complete_api_key(messages, tools, system)

    async def _complete_api_key(self, messages: list[dict], tools: list[dict], system: str) -> dict:
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        body = self._to_rest_request(messages, tools, system)
        response = await asyncio.to_thread(self._post_rest, body, headers, "Gemini API")
        return self._parse_rest_response(response)

    async def _complete_oauth(self, messages: list[dict], tools: list[dict], system: str) -> dict:
        assert self._oauth_access_token_provider is not None
        token = self._oauth_access_token_provider()

        # Strict validation for user-project mode
        if self._auth_mode == "oauth_user_project":
            if not self._quota_project:
                raise EnvironmentError(
                    "oauth_user_project mode requires a valid Google Cloud project ID. "
                    f"Please set {OAUTH_USER_PROJECT_ENV} or reconfigure in Settings."
                )

        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }
        if self._quota_project:
            headers["x-goog-user-project"] = self._quota_project

        body = self._to_rest_request(messages, tools, system)
        try:
            response = await asyncio.to_thread(self._post_rest, body, headers, "Gemini OAuth API")
        except Exception:
            logger.exception("Gemini OAuth request failed")
            raise
        return self._parse_rest_response(response)

    def _post_rest(self, body: dict[str, Any], headers: dict[str, str], error_label: str) -> dict[str, Any]:
        """POST a generateContent request. Blocking — call via asyncio.to_thread."""
        url = self._generate_content_url()
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=OAUTH_REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            # Redact body enough for logs; never include credentials.
            logger.error("%s error: HTTP %s: %s", error_label, exc.code, _truncate(raw, 2000))

            # Enhanced error message for user-project mode
            if self._auth_mode == "oauth_user_project":
                if exc.code == 429:
                    raise RuntimeError(
                        f"Your Google Gemini free-tier quota has been exceeded for project {self._quota_project}. "
                        f"Wait for your quota to reset, upgrade to a paid plan, or use an API key."
                    ) from exc
                elif exc.code in (400, 403):
                    raise RuntimeError(
                        f"Your Google Cloud project {self._quota_project} is not properly configured. "
                        f"Ensure the Gemini API is enabled and billing is set up if using paid models."
                    ) from exc

            raise RuntimeError(f"{error_label} error: HTTP {exc.code}: {_safe_google_error(raw)}") from exc

    # ------------------------------------------------------------------
    # Format transforms
    # ------------------------------------------------------------------

    def _to_rest_request(self, messages: list[dict], tools: list[dict], system: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": [self._to_rest_content(msg) for msg in messages],
            "generationConfig": {"maxOutputTokens": self._max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        rest_tools = self._to_rest_tools(tools)
        if rest_tools:
            body["tools"] = rest_tools
        return body

    def _to_rest_content(self, msg: dict) -> dict[str, Any]:
        role = "user" if msg.get("role") == "user" else "model"
        return {"role": role, "parts": self._to_rest_parts(msg.get("content", ""))}

    def _to_rest_parts(self, content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}]
        parts: list[dict[str, Any]] = []
        for item in content or []:
            if item.get("type") == "text":
                parts.append({"text": item.get("text", "")})
            elif item.get("type") == "image":
                parts.append({
                    "inlineData": {
                        "mimeType": item.get("media_type", "image/png"),
                        "data": item.get("data", ""),
                    }
                })
        return parts or [{"text": ""}]

    def _to_rest_tools(self, tools: list[dict]) -> list[dict[str, Any]]:
        declarations: list[dict[str, Any]] = []
        for t in tools:
            declaration: dict[str, Any] = {
                "name": t["name"],
                "description": t.get("description", ""),
            }
            params = t.get("parameters") or {}
            if params:
                declaration["parameters"] = self._convert_schema_json(params)
            declarations.append(declaration)
        return [{"functionDeclarations": declarations}] if declarations else []

    def _convert_schema_json(self, schema: dict) -> dict[str, Any]:
        raw_type = str(schema.get("type", "object" if "properties" in schema else "string")).lower()
        out: dict[str, Any] = {"type": raw_type.upper()}
        if "description" in schema:
            out["description"] = schema["description"]
        if "enum" in schema:
            out["enum"] = [str(v) for v in schema["enum"]]
        if raw_type == "array" and "items" in schema:
            out["items"] = self._convert_schema_json(schema["items"])
        if raw_type == "object":
            props = schema.get("properties") or {}
            if props:
                out["properties"] = {k: self._convert_schema_json(v) for k, v in props.items()}
            required = schema.get("required") or []
            if required:
                out["required"] = required
        return out

    def _generate_content_url(self) -> str:
        model = self._model_name if self._model_name.startswith("models/") else f"models/{self._model_name}"
        return (
            f"https://generativelanguage.googleapis.com/{self._api_version}/"
            f"{urllib.parse.quote(model, safe='/')}:generateContent"
        )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_rest_response(self, response: dict[str, Any]) -> dict:
        # cachedContentTokenCount is a SUBSET of input (tokens served from
        # cache, billed at ~0.25x) — non-zero when CachedContent is in play.
        usage_meta = response.get("usageMetadata") or {}
        usage = {
            "input": usage_meta.get("promptTokenCount", 0) or 0,
            "output": usage_meta.get("candidatesTokenCount", 0) or 0,
            "cache_read_tokens": usage_meta.get("cachedContentTokenCount", 0) or 0,
        }
        candidates = response.get("candidates") or []
        if not candidates:
            return {"type": "text", "content": "", "usage": usage}

        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text_chunks: list[str] = []
        for part in parts:
            function_call = part.get("functionCall")
            if function_call and function_call.get("name"):
                return {
                    "type": "tool_call",
                    "tool": function_call["name"],
                    "args": function_call.get("args") or {},
                    "usage": usage,
                }
            if part.get("text"):
                text_chunks.append(str(part["text"]))
        return {"type": "text", "content": "".join(text_chunks), "usage": usage}


def _parse_optional_expiry(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise EnvironmentError("oauth_access_token_expires_at must be epoch seconds") from exc


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _safe_google_error(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        message = parsed.get("error", {}).get("message")
        status = parsed.get("error", {}).get("status")
        if message and status:
            return f"{status}: {message}"
        if message:
            return str(message)
    except Exception:
        pass
    return _truncate(raw, 500)
