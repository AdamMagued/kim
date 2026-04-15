"""
Google Gemini provider.

Uses google-generativeai SDK (>=0.8.0).  Transforms canonical messages and
tools into Gemini's Content / FunctionDeclaration format and back.
"""

import base64
import logging
import os
from typing import Any

import google.generativeai as genai
from google.generativeai import protos

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# Map JSON Schema type strings → Gemini Type enum
_TYPE_MAP: dict[str, int] = {
    "string": protos.Type.STRING,
    "integer": protos.Type.INTEGER,
    "number": protos.Type.NUMBER,
    "boolean": protos.Type.BOOLEAN,
    "array": protos.Type.ARRAY,
    "object": protos.Type.OBJECT,
}


class GeminiProvider(BaseProvider):
    def __init__(self, config: dict):
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY is not set")
        genai.configure(api_key=api_key)
        models = config.get("model", {})
        self._model_name = models.get("gemini", "gemini-2.0-flash")
        self._max_tokens = int(config.get("max_tokens", 4096))
        logger.info(f"GeminiProvider: model={self._model_name}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        gemini_tools = self._to_gemini_tools(tools)
        model = genai.GenerativeModel(
            model_name=self._model_name,
            tools=gemini_tools,
            system_instruction=system,
            generation_config=genai.GenerationConfig(max_output_tokens=self._max_tokens),
        )

        # Split into history (all but last message) + current message
        history = self._to_gemini_contents(messages[:-1])
        current_parts = self._to_parts(messages[-1]["content"])

        chat = model.start_chat(history=history)
        try:
            response = await chat.send_message_async(current_parts)
        except Exception as e:
            logger.error(f"Gemini API error: {e}", exc_info=True)
            return {"type": "text", "content": f"API_ERROR: {e}"}

        return self._parse_response(response)

    # ------------------------------------------------------------------
    # Format transforms
    # ------------------------------------------------------------------

    def _to_gemini_contents(self, messages: list[dict]) -> list[protos.Content]:
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            parts = self._to_parts(msg["content"])
            contents.append(protos.Content(role=role, parts=parts))
        return contents

    def _to_parts(self, content: Any) -> list[protos.Part]:
        if isinstance(content, str):
            return [protos.Part(text=content)]
        parts = []
        for item in content:
            if item["type"] == "text":
                parts.append(protos.Part(text=item["text"]))
            elif item["type"] == "image":
                img_bytes = base64.b64decode(item["data"])
                parts.append(protos.Part(
                    inline_data=protos.Blob(
                        mime_type=item.get("media_type", "image/png"),
                        data=img_bytes,
                    )
                ))
        return parts or [protos.Part(text="")]

    def _to_gemini_tools(self, tools: list[dict]) -> list[protos.Tool]:
        declarations = []
        for t in tools:
            params = t.get("parameters", {})
            props: dict[str, protos.Schema] = {}
            for prop_name, prop_schema in params.get("properties", {}).items():
                props[prop_name] = self._convert_schema(prop_schema)

            fd = protos.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=protos.Schema(
                    type=protos.Type.OBJECT,
                    properties=props,
                    required=params.get("required", []),
                ) if props else None,
            )
            declarations.append(fd)

        return [protos.Tool(function_declarations=declarations)] if declarations else []

    def _convert_schema(self, schema: dict) -> protos.Schema:
        raw_type = schema.get("type", "string")
        gemini_type = _TYPE_MAP.get(raw_type, protos.Type.STRING)
        s = protos.Schema(type=gemini_type)
        if "description" in schema:
            s.description = schema["description"]
        if "enum" in schema:
            s.enum[:] = [str(v) for v in schema["enum"]]
        if raw_type == "array" and "items" in schema:
            s.items.CopyFrom(self._convert_schema(schema["items"]))
        if raw_type == "object" and "properties" in schema:
            for k, v in schema["properties"].items():
                s.properties[k].CopyFrom(self._convert_schema(v))
        return s

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response) -> dict:
        try:
            candidate = response.candidates[0]
        except (AttributeError, IndexError):
            return {"type": "text", "content": ""}

        for part in candidate.content.parts:
            if hasattr(part, "function_call") and part.function_call.name:
                return {
                    "type": "tool_call",
                    "tool": part.function_call.name,
                    "args": dict(part.function_call.args),
                }
            if hasattr(part, "text") and part.text:
                return {"type": "text", "content": part.text}

        # Fallback to response.text accessor
        try:
            return {"type": "text", "content": response.text}
        except Exception:
            return {"type": "text", "content": ""}
