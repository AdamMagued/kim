"""
Anthropic Claude provider.

Uses native tool-use (input_schema / tool_use blocks) for reliable structured
responses.  Transforms canonical messages and tools to Anthropic API format
and back.
"""

import json
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
        self._model = models.get("claude", "claude-opus-4-6")
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
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return {"type": "text", "content": f"API_ERROR: {e}"}

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

        for block in response.content:
            if block.type == "tool_use":
                return {
                    "type": "tool_call",
                    "tool": block.name,
                    "args": dict(block.input),
                    "usage": usage,
                }
            if block.type == "text":
                return {"type": "text", "content": block.text, "usage": usage}

        # Fallback: concatenate all text blocks
        text = " ".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        )
        return {"type": "text", "content": text or "", "usage": usage}
