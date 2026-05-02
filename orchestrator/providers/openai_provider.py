"""
OpenAI provider (GPT-4o and compatible models).

Uses function calling (tools API) for structured responses.
Transforms canonical messages/tools to OpenAI format and back.
"""

import json
import logging
import os

import openai

from orchestrator.providers.base import BaseProvider

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

        kwargs: dict = {"api_key": api_key or "placeholder"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        models = config.get("model", {})
        self._model = models.get("openai", "gpt-4o")
        self._max_tokens = int(config.get("max_tokens", 4096))
        logger.info(f"OpenAIProvider: model={self._model} base_url={base_url or 'openai'}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        oai_messages = [{"role": "system", "content": system}] + self._to_oai_messages(messages)
        oai_tools = self._to_oai_tools(tools)

        try:
            kwargs: dict = dict(
                model=self._model,
                messages=oai_messages,
                max_tokens=self._max_tokens,
            )
            if oai_tools:
                kwargs["tools"] = oai_tools
                kwargs["tool_choice"] = "auto"

            response = await self._client.chat.completions.create(**kwargs)
        except openai.RateLimitError:
            raise
        except openai.AuthenticationError:
            raise  # 401 — bad key; non-retryable
        except openai.PermissionDeniedError:
            raise  # 403 — non-retryable
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

        # Extract token usage
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input": getattr(response.usage, "prompt_tokens", 0),
                "output": getattr(response.usage, "completion_tokens", 0),
            }

        if msg.tool_calls:
            if len(msg.tool_calls) > 1:
                return {
                    "type": "text",
                    "content": (
                        f"SYSTEM ERROR: You requested {len(msg.tool_calls)} "
                        "parallel tool calls, but only 1 is supported at a time. "
                        "Please pick the most important one and try again."
                    ),
                    "usage": usage
                }
            tc = msg.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            return {
                "type": "tool_call",
                "tool": tc.function.name,
                "args": args,
                "usage": usage,
            }

        return {"type": "text", "content": msg.content or "", "usage": usage}
