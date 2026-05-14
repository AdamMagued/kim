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
import subprocess
from typing import Any

import httpx

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_CLOUD_MODEL = "gpt-oss:120b-cloud"
KNOWN_CLOUD_MODELS = {
    "gpt-oss:20b-cloud",
    DEFAULT_OLLAMA_CLOUD_MODEL,
}


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

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": True,
        }
        if tools:
            payload["tools"] = self._to_ollama_tools(tools)
        if self._context_override:
            payload["options"] = {"num_ctx": self._context_override}

        final_obj, content, tool_calls = await self._stream_chat(payload)
        usage = await self._usage_from_final(final_obj, model)

        if tool_calls:
            if len(tool_calls) > 1:
                return {
                    "type": "text",
                    "content": (
                        f"SYSTEM ERROR: You requested {len(tool_calls)} parallel tool calls, "
                        "but only 1 is supported at a time. Please pick the most important one and try again."
                    ),
                    "usage": usage,
                }
            tc = tool_calls[0]
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = str((fn or {}).get("name") or tc.get("name") or "").strip()
            args_raw = (fn or {}).get("arguments")
            args = _normalize_tool_arguments(args_raw)
            return {
                "type": "tool_call",
                "tool": name,
                "args": args,
                "usage": usage,
            }

        return {
            "type": "text",
            "content": content,
            "usage": usage,
        }

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
            if model not in KNOWN_CLOUD_MODELS and not model.endswith("-cloud"):
                raise EnvironmentError(
                    f"Ollama cloud model {model!r} is not recognized. Pick a cloud model in Settings → AI → Ollama."
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
            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(str(item.get("text") or ""))
                    elif isinstance(item, dict):
                        text_parts.append(json.dumps(item, ensure_ascii=False))
                    else:
                        text_parts.append(str(item))
                out.append({"role": role, "content": "\n".join([p for p in text_parts if p]).strip()})
            else:
                out.append({"role": role, "content": str(content or "")})
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


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
