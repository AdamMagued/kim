"""
OpenAI provider (GPT-4o and compatible models).

Uses function calling (tools API) for structured responses.
Transforms canonical messages/tools to OpenAI format and back.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import openai

from orchestrator.providers.base import BaseProvider, finalize_text_content

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """
    OpenAI-compatible provider.

    Supports any OpenAI-compatible API (Cerebras, Groq, Together, etc.) by
    setting `openai_base_url` in config.yaml.  The API key env-var name can
    be overridden with `openai_api_key_env` (defaults to OPENAI_API_KEY).

    Example config.yaml for Cerebras:
        openai_base_url: "https://api.cerebras.ai/v1"
        openai_api_key_env: "CEREBRAS_API_KEY"
        model:
          openai: "llama-4-scout-17b-16e-instruct"

    DeepSeekProvider subclasses this with its own _BASE_URL class attribute.
    """

    _BASE_URL: str | None = None  # Subclass override (takes precedence over config)

    def __init__(self, config: dict):
        # Resolve base URL: subclass attr > config > None (= official OpenAI)
        base_url = self._BASE_URL or config.get("openai_base_url") or None

        # Resolve API key: configurable env-var name, default OPENAI_API_KEY
        key_env = config.get("openai_api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(key_env, "")
        if not api_key and not self._BASE_URL:
            # Also try the plain OPENAI_API_KEY as a final fallback
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key and base_url is None:
            raise EnvironmentError(
                f"{key_env} is not set. "
                "Set it in .env or use openai_api_key_env in config.yaml."
            )

        # F-B-12: a REMOTE OpenAI-compatible endpoint (Cerebras/Groq/Together)
        # with no key still gets api_key="placeholder", turning a fixable config
        # gap into a cryptic first-call 401. Only token-less LOCAL proxies are a
        # legitimate placeholder case, so warn loudly when the host is remote.
        if not api_key and base_url is not None and not _is_localhost_url(base_url):
            logger.warning(
                "%s is not set and openai_base_url=%s is remote — using a placeholder "
                "key. If that host needs authentication you will get an HTTP 401; set "
                "%s in .env (or openai_api_key_env in config.yaml).",
                key_env, base_url, key_env,
            )

        kwargs: dict = {"api_key": api_key or "placeholder"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        models = config.get("model", {})
        self._model = models.get("openai", "gpt-4o")
        self._max_tokens = int(config.get("max_tokens", 4096))
        # F-INH-2: o-series reasoning models and the GPT-5 family reject the
        # legacy `max_tokens` field and require `max_completion_tokens`. Pick the
        # right field up front for known families; complete() also self-corrects
        # once on the specific 400 so an unknown future model still works.
        self._token_param = _default_token_param(self._model)
        logger.info(f"OpenAIProvider: model={self._model} base_url={base_url or 'openai'}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict[str, Any]:
        oai_messages = [{"role": "system", "content": system}] + self._to_oai_messages(messages)
        oai_tools = self._to_oai_tools(tools)

        kwargs: dict = dict(
            model=self._model,
            messages=oai_messages,
        )
        kwargs[self._token_param] = self._max_tokens
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"
        kwargs["timeout"] = 180.0

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.RateLimitError:
            raise
        except openai.AuthenticationError:
            raise  # 401 — bad key; non-retryable
        except openai.PermissionDeniedError:
            raise  # 403 — non-retryable
        except openai.APITimeoutError as e:
            # Re-raise as builtin TimeoutError so classify_provider_error marks it
            # retryable regardless of Python version (isinstance check works on all).
            raise TimeoutError(str(e) or "OpenAI API timed out") from e
        except openai.BadRequestError as e:
            # F-INH-2: a model that requires max_completion_tokens 400s on
            # max_tokens. Swap the field and retry once, remembering the choice
            # for the rest of the session so the wasted call happens at most once.
            if self._token_param == "max_tokens" and _is_max_tokens_param_error(e):
                logger.info(
                    "OpenAI model %s rejected max_tokens; retrying with max_completion_tokens",
                    self._model,
                )
                self._token_param = "max_completion_tokens"
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = self._max_tokens
                response = await self._client.chat.completions.create(**kwargs)
            else:
                logger.error(f"OpenAI API error: {e}")
                raise
        except openai.APIError as e:
            logger.error(f"OpenAI API error: {e}")
            raise

        return self._parse_response(response)

    # ------------------------------------------------------------------
    # Format transforms
    # ------------------------------------------------------------------

    def _to_oai_messages(self, messages: list[dict]) -> list[dict]:
        result = []
        for msg in messages:
            content = msg["content"]
            role = msg["role"]

            if isinstance(content, list):
                oai_content = []
                for item in content:
                    if item["type"] == "text":
                        oai_content.append({"type": "text", "text": item["text"]})
                    elif item["type"] == "image":
                        mt = item.get("media_type", "image/png")
                        oai_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mt};base64,{item['data']}"},
                        })
                result.append({"role": role, "content": oai_content})
            else:
                result.append({"role": role, "content": str(content)})

        return result

    def _to_oai_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response) -> dict:
        choice = response.choices[0]
        msg = choice.message
        # "stop" is normal; "length" means truncated and "content_filter" a
        # safety block — surface it so neither is mistaken for a complete
        # answer (finding 3.1).
        finish_reason = getattr(choice, "finish_reason", None)

        # Extract token usage.
        # OpenAI's prompt_tokens INCLUDES cached_tokens (cache_read is a subset, not additive).
        # cache_creation_tokens is not reported by OpenAI — always 0.
        # Note: DeepSeek (subclass) reports cache via prompt_cache_hit_tokens at the top level
        # rather than prompt_tokens_details.cached_tokens, so cache_read_tokens will be 0 for
        # DeepSeek even when caching is active — known limitation, fix in a later slice.
        usage = {}
        if hasattr(response, "usage") and response.usage:
            details = getattr(response.usage, "prompt_tokens_details", None)
            usage = {
                "input": getattr(response.usage, "prompt_tokens", 0),
                "output": getattr(response.usage, "completion_tokens", 0),
                "cache_creation_tokens": 0,
                "cache_read_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
            }

        if msg.tool_calls:
            def _parse_one(tc):
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    # M9: never silently swap malformed args for {} — log what
                    # the model actually produced so the confusing downstream
                    # "missing argument" failure has a trace.
                    logger.warning(
                        "Malformed tool-call arguments for %r (%r) — using {}",
                        tc.function.name, str(tc.function.arguments)[:200],
                    )
                    args = {}
                return {"tool": tc.function.name, "args": args}

            # H2: keep any assistant narration that accompanies the tool call —
            # the agent reads response["content"] for PLAN/STEP markers.
            narration = msg.content or ""

            if len(msg.tool_calls) > 1:
                # Surface multi-tool requests as a `batch` call so the agent
                # can sequence them (including mutating tools, which go through
                # the normal preview/HITL gate in the batch dispatcher).
                return {
                    "type": "tool_call",
                    "tool": "batch",
                    "args": {"calls": [_parse_one(tc) for tc in msg.tool_calls]},
                    "content": narration,
                    "stop_reason": finish_reason,
                    "usage": usage,
                }

            tc = msg.tool_calls[0]
            parsed = _parse_one(tc)
            return {
                "type": "tool_call",
                "tool": parsed["tool"],
                "args": parsed["args"],
                "content": narration,
                "stop_reason": finish_reason,
                "usage": usage,
            }

        return {
            "type": "text",
            "content": finalize_text_content(msg.content or "", finish_reason),
            "stop_reason": finish_reason,
            "usage": usage,
        }


def _is_localhost_url(url: str) -> bool:
    """True for loopback hosts (token-less local proxies are a valid no-key case)."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost")


def _default_token_param(model: str) -> str:
    """Field name for the output-token limit (F-INH-2).

    o-series reasoning models (o1/o3/o4/…) and the GPT-5 family require
    ``max_completion_tokens``; everything else still takes ``max_tokens``.
    An unknown model defaults to max_tokens and complete() self-corrects on the
    specific 400.
    """
    m = (model or "").lower().strip()
    # Strip a provider prefix some proxies use, e.g. "openai/o3-mini".
    m = m.rsplit("/", 1)[-1]
    if m.startswith(("o1", "o3", "o4", "o5")) or m.startswith("gpt-5"):
        return "max_completion_tokens"
    return "max_tokens"


def _is_max_tokens_param_error(exc: Exception) -> bool:
    """True when a 400 says max_tokens is unsupported / to use max_completion_tokens."""
    msg = str(exc).lower()
    return "max_completion_tokens" in msg or (
        "max_tokens" in msg and ("unsupported" in msg or "not supported" in msg)
    )
