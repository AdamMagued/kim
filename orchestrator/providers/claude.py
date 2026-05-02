"""
Anthropic Claude provider.

Uses native tool-use (input_schema / tool_use blocks) for reliable structured
responses.  Transforms canonical messages and tools to Anthropic API format
and back.
"""

import logging
import os

import anthropic

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    def __init__(self, config: dict):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        models = config.get("model", {})
        self._model = models.get("claude", "claude-opus-4-5")
        self._max_tokens = int(config.get("max_tokens", 4096))
        logger.info(f"AnthropicProvider: model={self._model}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        claude_messages = self._to_claude_messages(messages)
        claude_tools = self._to_claude_tools(tools)

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                tools=claude_tools,
                messages=claude_messages,
            )
        except anthropic.RateLimitError:
            raise
        except anthropic.AuthenticationError:
            raise  # 401 — bad key; non-retryable
        except anthropic.PermissionDeniedError:
            raise  # 403 — non-retryable
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
        # Extract token usage if available
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input": getattr(response.usage, "input_tokens", 0),
                "output": getattr(response.usage, "output_tokens", 0),
            }

        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        if len(tool_blocks) == 1:
            b = tool_blocks[0]
            return {
                "type": "tool_call",
                "tool": b.name,
                "args": dict(b.input),
                "usage": usage,
            }

        if len(tool_blocks) > 1:
            # Claude returned multiple tool_use blocks — wrap as a batch call
            # so the agent executes all of them rather than silently dropping extras.
            logger.debug("Claude returned %d parallel tool_use blocks; wrapping as batch", len(tool_blocks))
            return {
                "type": "tool_call",
                "tool": "batch",
                "args": {"calls": [{"tool": b.name, "args": dict(b.input)} for b in tool_blocks]},
                "usage": usage,
            }

        # Fallback: concatenate all text blocks
        text = " ".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        )
        return {"type": "text", "content": text or "", "usage": usage}
