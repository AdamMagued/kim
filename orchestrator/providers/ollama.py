"""
Ollama provider.

Routes all requests through the local Ollama daemon. Kim never stores Ollama
credentials and never talks directly to ollama.com APIs for the default flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Any

import httpx

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_CLOUD_MODEL = "gpt-oss:120b-cloud"


def _env_or_cfg(config: dict, env_key: str, *path: str, default: Any = None) -> Any:
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val
    cur: Any = config
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur not in (None, "") else default


def _parse_num_ctx(text: str | None) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for line in raw.splitlines():
        s = line.strip().replace("=", " ").replace(":", " ")
        parts = [p for p in s.split() if p]
        for i, part in enumerate(parts[:-1]):
            if part == "num_ctx":
                try:
                    return max(0, int(parts[i + 1]))
                except ValueError:
                    continue
    return None


def _parse_context_column(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    mult = 1
    if text[-1].lower() == "k":
        mult = 1000
        text = text[:-1]
    elif text[-1].lower() == "m":
        mult = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def _parse_ollama_ps_context(output: str, model: str) -> int | None:
    wanted = model.strip().lower()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        lowered = stripped.lower()
        if not lowered.startswith(wanted):
            continue
        cols = stripped.split()
        if len(cols) < 2:
            continue
        return _parse_context_column(cols[-1])
    return None


class OllamaProvider(BaseProvider):
    native_tool_calling = True
    lean_system_prompt = True

    def __init__(self, config: dict):
        self._vision_cache: dict[str, bool] = {}
        ollama_cfg = dict(config.get("ollama") or {})
        self._base_url = str(
            _env_or_cfg(config, "KIM_OLLAMA_BASE_URL", "ollama", "base_url", default=ollama_cfg.get("base_url") or DEFAULT_OLLAMA_BASE_URL)
        ).rstrip("/")
        self._mode = str(
            _env_or_cfg(config, "KIM_OLLAMA_MODE", "ollama", "mode", default=ollama_cfg.get("mode") or "local")
        ).strip().lower()
        if self._mode not in {"local", "cloud"}:
            self._mode = "local"
        self._local_model = str(
            _env_or_cfg(config, "KIM_OLLAMA_LOCAL_MODEL", "ollama", "local_model", default=ollama_cfg.get("local_model") or "")
        ).strip()
        self._cloud_model = str(
            _env_or_cfg(config, "KIM_OLLAMA_CLOUD_MODEL", "ollama", "cloud_model", default=ollama_cfg.get("cloud_model") or DEFAULT_OLLAMA_CLOUD_MODEL)
        ).strip()
        self._context_override = _coerce_optional_int(
            _env_or_cfg(config, "KIM_OLLAMA_CONTEXT_LIMIT_OVERRIDE", "ollama", "context_limit_override", default=ollama_cfg.get("context_limit_override"))
        )
        self._keep_alive = str(
            _env_or_cfg(config, "KIM_OLLAMA_KEEP_ALIVE", "ollama", "keep_alive", default=ollama_cfg.get("keep_alive") or "5m")
        ).strip()
        self._timeout_s = 600.0
        logger.info(
            "OllamaProvider: base_url=%s mode=%s local_model=%s cloud_model=%s",
            self._base_url,
            self._mode,
            self._local_model or "(auto)",
            self._cloud_model,
        )

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        model = await self._resolve_selected_model()
        await self._ensure_daemon_running()
        await self._validate_model(model)

        # Proactively strip images for models we know don't support vision.
        if self._vision_cache.get(model) is False:
            messages = self._strip_images_from_messages(messages)

        def _build_payload(msgs: list[dict]) -> dict[str, Any]:
            p: dict[str, Any] = {
                "model": model,
                "messages": self._to_ollama_messages(msgs, system),
                "stream": True,
            }
            if self._keep_alive:
                p["keep_alive"] = self._keep_alive
            if tools:
                p["tools"] = self._to_ollama_tools(tools)
            if self._context_override:
                p["options"] = {"num_ctx": self._context_override}
            return p

        try:
            final_obj, content, tool_calls = await self._stream_chat(_build_payload(messages))
        except EnvironmentError as exc:
            if _looks_like_vision_model_error(str(exc).lower()) and self._messages_have_images(messages):
                # Model doesn't support images — cache and retry without them.
                logger.info("OllamaProvider: %s doesn't support vision; retrying without images.", model)
                self._vision_cache[model] = False
                messages = self._strip_images_from_messages(messages)
                final_obj, content, tool_calls = await self._stream_chat(_build_payload(messages))
            else:
                raise

        usage = await self._usage_from_final(final_obj, model)

        def _parse_one(tc):
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = str((fn or {}).get("name") or tc.get("name") or "").strip()
            args = _normalize_tool_arguments((fn or {}).get("arguments"))
            return {"tool": name, "args": args}

        if tool_calls:
            if len(tool_calls) > 1:
                # Wrap as `batch` so the agent's batch dispatcher executes
                # the calls sequentially. Previously this returned a text
                # error which the agent treated as a stuck-loop turn.
                return {
                    "type": "tool_call",
                    "tool": "batch",
                    "args": {"calls": [_parse_one(tc) for tc in tool_calls]},
                    "content": content,
                    "usage": usage,
                }

            parsed = _parse_one(tool_calls[0])
            return {
                "type": "tool_call",
                "tool": parsed["tool"],
                "args": parsed["args"],
                "content": content,
                "usage": usage,
            }

        return {
            "type": "text",
            "content": content,
            "usage": usage,
        }

    def _messages_have_images(self, messages: list[dict]) -> bool:
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                if any(isinstance(item, dict) and item.get("type") == "image" for item in content):
                    return True
        return False

    def _strip_images_from_messages(self, messages: list[dict]) -> list[dict]:
        cleaned: list[dict] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = [item for item in content if not (isinstance(item, dict) and item.get("type") == "image")]
                had_image = len(new_content) < len(content)
                if had_image:
                    new_content.append({
                        "type": "text",
                        "text": "[Screenshot captured but this model does not support vision. Use observe_ui or run_command to get UI context instead.]",
                    })
                cleaned.append({**msg, "content": new_content})
            else:
                cleaned.append(msg)
        return cleaned

    async def _ensure_daemon_running(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/version")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise EnvironmentError(
                    "Ollama is installed but not running. Start Ollama, then try again."
                ) from exc

    async def _resolve_selected_model(self) -> str:
        if self._mode == "cloud":
            return self._cloud_model or DEFAULT_OLLAMA_CLOUD_MODEL
        if self._local_model:
            return self._local_model

        tags = await self._fetch_tags()
        if tags:
            first = str((tags[0] or {}).get("name") or "").strip()
            if first:
                self._local_model = first
                return first
        raise EnvironmentError(
            "No local Ollama models are installed. Pull a model in Settings → AI → Ollama, then try again."
        )

    async def _validate_model(self, model: str) -> None:
        if self._mode == "cloud":
            if not model.strip():
                raise EnvironmentError(
                    "No Ollama cloud model selected. Pick a model in Settings → AI → Ollama, then try again."
                )
            return

        tags = await self._fetch_tags()
        names = {
            str(item.get("name") or "").strip().lower()
            for item in tags
            if isinstance(item, dict)
        }
        if model.strip().lower() not in names:
            raise EnvironmentError(
                f"Ollama local model {model!r} is not installed. Pull it in Settings → AI → Ollama or pick another model."
            )

    async def _fetch_tags(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{self._base_url}/api/tags")
            resp.raise_for_status()
            payload = resp.json()
        models = payload.get("models")
        return models if isinstance(models, list) else []

    def _to_ollama_messages(self, messages: list[dict], system: str) -> list[dict]:
        out: list[dict] = []
        if system.strip():
            out.append({"role": "system", "content": system})
        for msg in messages:
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            if role == "assistant" and isinstance(content, str):
                native_tool_call = _assistant_tool_call_message(content)
                if native_tool_call:
                    out.append(native_tool_call)
                    continue
            if isinstance(content, list):
                text_parts: list[str] = []
                images: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(str(item.get("text") or ""))
                    elif isinstance(item, dict) and item.get("type") == "image":
                        image_data = _normalize_image_data(item.get("data"))
                        if image_data:
                            images.append(image_data)
                    elif isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if text:
                            text_parts.append(str(text))
                    else:
                        text_parts.append(str(item))
                converted: dict[str, Any] = {
                    "role": role,
                    "content": "\n".join([p for p in text_parts if p]).strip(),
                }
                if images:
                    converted["images"] = images
                else:
                    tool_result = _tool_result_message(role, converted["content"])
                    if tool_result:
                        out.append(tool_result)
                        continue
                out.append(converted)
            else:
                text = str(content or "")
                tool_result = _tool_result_message(role, text)
                if tool_result:
                    out.append(tool_result)
                    continue
                out.append({"role": role, "content": text})
        return out

    def _to_ollama_tools(self, tools: list[dict]) -> list[dict]:
        converted = []
        for tool in tools:
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
        return converted

    async def _stream_chat(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        pieces: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        final_obj: dict[str, Any] | None = None

        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s, connect=10.0)) as client:
            async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", errors="replace").strip()
                    lowered = detail.lower()
                    if _looks_like_vision_model_error(lowered):
                        raise EnvironmentError(
                            "The selected Ollama model does not appear to support images. "
                            "Pick a vision-capable Ollama model or use structured UI observation instead of screenshots."
                        )
                    if "not found" in lowered or "pull" in lowered:
                        raise EnvironmentError(
                            f"Ollama model {payload.get('model')!r} is unavailable. Pull it in Settings → AI → Ollama or pick another model."
                        )
                    if "sign in" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
                        raise PermissionError("Sign in to Ollama to use cloud models.")
                    raise RuntimeError(detail or f"Ollama returned HTTP {resp.status_code}.")

                async for line in resp.aiter_lines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = json.loads(stripped)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON Ollama stream line: %s", stripped)
                        continue
                    if item.get("error"):
                        detail = str(item["error"]).strip()
                        lowered = detail.lower()
                        if "sign in" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
                            raise PermissionError("Sign in to Ollama to use cloud models.")
                        if _looks_like_vision_model_error(lowered):
                            raise EnvironmentError(
                                "The selected Ollama model does not appear to support images. "
                                "Pick a vision-capable Ollama model or use structured UI observation instead of screenshots."
                            )
                        if "not found" in lowered or "pull" in lowered:
                            raise EnvironmentError(
                                f"Ollama model {payload.get('model')!r} is unavailable. Pull it in Settings → AI → Ollama or pick another model."
                            )
                        raise RuntimeError(detail)

                    msg = item.get("message") if isinstance(item.get("message"), dict) else {}
                    chunk = msg.get("content")
                    if isinstance(chunk, str) and chunk:
                        pieces.append(chunk)
                    tc = msg.get("tool_calls")
                    if isinstance(tc, list) and tc:
                        tool_calls = [x for x in tc if isinstance(x, dict)]
                    if item.get("done") is True:
                        final_obj = item

        if final_obj is None:
            raise RuntimeError("Ollama stream ended without a final done response.")

        final_message = final_obj.get("message") if isinstance(final_obj.get("message"), dict) else {}
        if not pieces and isinstance(final_message.get("content"), str):
            pieces.append(str(final_message.get("content") or ""))
        if not tool_calls and isinstance(final_message.get("tool_calls"), list):
            tool_calls = [x for x in final_message.get("tool_calls") if isinstance(x, dict)]
        return final_obj, "".join(pieces).strip(), tool_calls

    async def _usage_from_final(self, final_obj: dict[str, Any], model: str) -> dict[str, Any]:
        prompt_eval_count = _coerce_optional_int(final_obj.get("prompt_eval_count"))
        eval_count = _coerce_optional_int(final_obj.get("eval_count"))
        total_duration = _coerce_optional_int(final_obj.get("total_duration"))
        load_duration = _coerce_optional_int(final_obj.get("load_duration"))
        prompt_eval_duration = _coerce_optional_int(final_obj.get("prompt_eval_duration"))
        eval_duration = _coerce_optional_int(final_obj.get("eval_duration"))
        context_limit, context_source = await self._resolve_context_limit(model)

        usage_available = prompt_eval_count is not None and eval_count is not None
        usage: dict[str, Any] = {
            "provider": "ollama",
            "source": "ollama",
            "model": str(final_obj.get("model") or model),
            "mode": self._mode,
            "usage_available": usage_available,
            "forbid_fallback": True,
            "billing": "Local: no API billing" if self._mode == "local" else "Cloud account usage is managed by Ollama. Kim can show token usage, not remaining account balance.",
        }
        if prompt_eval_count is not None:
            usage["input"] = prompt_eval_count
            usage["prompt_eval_count"] = prompt_eval_count
        if eval_count is not None:
            usage["output"] = eval_count
            usage["eval_count"] = eval_count
        if total_duration is not None:
            usage["total_duration"] = total_duration
        if load_duration is not None:
            usage["load_duration"] = load_duration
        if prompt_eval_duration is not None:
            usage["prompt_eval_duration"] = prompt_eval_duration
        if eval_duration is not None:
            usage["eval_duration"] = eval_duration
            if eval_count is not None and eval_duration > 0:
                usage["tokens_per_second"] = round(eval_count / (eval_duration / 1_000_000_000), 2)
        if context_limit is not None:
            usage["context_limit"] = context_limit
            usage["context_limit_source"] = context_source
        elif self._context_override:
            usage["context_limit"] = self._context_override
            usage["context_limit_source"] = "override"
        else:
            usage["context_limit_source"] = context_source or "unknown"
        return usage

    async def _resolve_context_limit(self, model: str) -> tuple[int | None, str | None]:
        ps_limit = await asyncio.to_thread(self._context_limit_from_ps_sync, model)
        if ps_limit:
            return ps_limit, "ollama_ps"

        show_limit = await self._context_limit_from_show(model)
        if show_limit:
            return show_limit, "api_show"

        if self._context_override:
            return self._context_override, "override"
        return None, None

    def _context_limit_from_ps_sync(self, model: str) -> int | None:
        try:
            proc = subprocess.run(
                ["ollama", "ps"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return _parse_ollama_ps_context(proc.stdout, model)

    async def _context_limit_from_show(self, model: str) -> int | None:
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.post(f"{self._base_url}/api/show", json={"model": model})
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
            payload = resp.json()
        return (
            _parse_num_ctx(payload.get("parameters"))
            or _parse_num_ctx(payload.get("modelfile"))
        )


def _normalize_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _assistant_tool_call_message(raw_content: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "tool_call":
        return None
    name = str(parsed.get("tool") or "").strip()
    if not name:
        return None
    args = _normalize_tool_arguments(parsed.get("args"))
    content = str(parsed.get("content") or "").strip()
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "index": 0,
                    "name": name,
                    "arguments": args,
                },
            }
        ],
    }


def _tool_result_message(role: str, text: str) -> dict[str, Any] | None:
    if role != "user":
        return None
    match = re.match(r"^\s*\[Tool result:\s*([A-Za-z0-9_.:-]+)\]\s*\n?([\s\S]*)$", text)
    if not match:
        return None
    name = match.group(1).strip()
    body = match.group(2).strip()
    if not name:
        return None
    return {
        "role": "tool",
        "tool_name": name,
        "content": body,
    }


def _normalize_image_data(raw: Any) -> str:
    data = str(raw or "").strip()
    if not data or data == "[image data stripped]":
        return ""
    if data.startswith("data:") and "," in data:
        return data.split(",", 1)[1].strip()
    return data


def _looks_like_vision_model_error(lowered_detail: str) -> bool:
    return (
        ("image" in lowered_detail or "vision" in lowered_detail or "multimodal" in lowered_detail)
        and (
            "support" in lowered_detail
            or "unsupported" in lowered_detail
            or "does not" in lowered_detail
            or "cannot" in lowered_detail
        )
    )


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
