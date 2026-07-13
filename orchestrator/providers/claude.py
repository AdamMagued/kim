"""
Anthropic Claude provider.

Uses native tool-use (input_schema / tool_use blocks) for reliable structured
responses.  Transforms canonical messages and tools to Anthropic API format
and back.
"""

import logging
import os
from typing import Any

import anthropic

from orchestrator.providers.base import BaseProvider, finalize_text_content

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    def __init__(self, config: dict):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        models = config.get("model", {})
        self._model = models.get("claude", "claude-opus-4-6")
        self._max_tokens = int(config.get("max_tokens", 4096))
        logger.info(f"AnthropicProvider: model={self._model}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict[str, Any]:
        claude_messages = self._to_claude_messages(messages)
        claude_tools = self._to_claude_tools(tools)

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                tools=claude_tools,  # pyright: ignore[reportArgumentType]
                messages=claude_messages,  # pyright: ignore[reportArgumentType]
                timeout=180.0,
            )
        except anthropic.RateLimitError:
            raise
        except anthropic.AuthenticationError:
            raise  # 401 — bad key; non-retryable
        except anthropic.PermissionDeniedError:
            raise  # 403 — non-retryable
        except anthropic.APITimeoutError as e:
            # Re-raise as builtin TimeoutError so classify_provider_error marks it
            # retryable regardless of Python version (isinstance check works on all).
            raise TimeoutError(str(e) or "Anthropic API timed out") from e
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            raise

        return self._parse_response(response)

    # ------------------------------------------------------------------
    # Format transforms
    # ------------------------------------------------------------------

    def _to_claude_messages(self, messages: list[dict]) -> list[dict]:
        result = []
        for msg in messages:
            content = msg["content"]
            role = msg["role"]

            if isinstance(content, list):
                claude_content = []
                for item in content:
                    if item["type"] == "text":
                        claude_content.append({"type": "text", "text": item["text"]})
                    elif item["type"] == "image":
                        claude_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": item.get("media_type", "image/png"),
                                "data": item["data"],
                            },
                        })
                result.append({"role": role, "content": claude_content})
            else:
                result.append({"role": role, "content": str(content)})

        # F-B-2: Anthropic rejects a message list whose first turn is not a user
        # turn ("first message must use the user role", HTTP 400 → non-retryable
        # invalid_request). A memory trim that walks the resume boundary back to
        # include an assistant tool-call turn yields [assistant, user, …], which
        # bricks the whole resumed session. Every other provider tolerates this
        # shape; prepend a synthetic user turn so claude.py does too. (Root fix
        # is memory.py — Team A; this is the provider-side belt-and-suspenders.)
        if result and result[0]["role"] == "assistant":
            result.insert(0, {"role": "user", "content": "[Conversation resumed mid-exchange.]"})

        return result

    def _to_claude_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response) -> dict:
        # Extract token usage if available.
        # Anthropic counts cache tokens separately from input_tokens (additive).
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input": getattr(response.usage, "input_tokens", 0),
                "output": getattr(response.usage, "output_tokens", 0),
                # Cache tokens: non-zero only when prompt caching is active.
                # cache_creation_tokens: tokens written to cache this call (billed ~1.25x).
                # cache_read_tokens: tokens read from an existing cache entry (billed ~0.1x).
                # Both are ADDITIVE to input_tokens (not a subset).
                "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            }

        # Why generation stopped: "end_turn"/"tool_use" are normal; "max_tokens"
        # means the reply was truncated and "refusal" a safety stop (finding 3.1).
        stop_reason = getattr(response, "stop_reason", None)

        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        # Concatenate all text blocks. Also attached to tool_call responses:
        # the agent reads response["content"] for the PLAN/STEP/THINKING
        # narration that precedes a tool call (H2 — it was silently dropped
        # for the API providers while Ollama/browser preserved it).
        text = " ".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        )

        if len(tool_blocks) == 1:
            b = tool_blocks[0]
            return {
                "type": "tool_call",
                "tool": b.name,
                "args": dict(b.input),
                "content": text,
                "stop_reason": stop_reason,
                "usage": usage,
            }

        if len(tool_blocks) > 1:
            # Claude returned multiple tool_use blocks — wrap as a batch call
            # so the agent executes all of them sequentially (including mutating
            # tools, which go through the normal preview/HITL gate).
            logger.debug("Claude returned %d parallel tool_use blocks; wrapping as batch", len(tool_blocks))
            return {
                "type": "tool_call",
                "tool": "batch",
                "args": {"calls": [{"tool": b.name, "args": dict(b.input)} for b in tool_blocks]},
                "content": text,
                "stop_reason": stop_reason,
                "usage": usage,
            }

        return {
            "type": "text",
            "content": finalize_text_content(text, stop_reason),
            "stop_reason": stop_reason,
            "usage": usage,
        }
