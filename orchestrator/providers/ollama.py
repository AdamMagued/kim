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

from orchestrator.providers.base import (
    BaseProvider,
    finalize_text_content,
    malformed_tool_args_text,
    ProviderEnvironmentError,
)
from orchestrator.providers.ollama_context import (  # noqa: F401 — re-exported, tests import from here
    _parse_context_column,
    _parse_num_ctx,
    _parse_ollama_ps_context,
    _ps_context_column_span,
)
from orchestrator.providers.ollama_signin import trigger_signin_and_wait

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


class OllamaProvider(BaseProvider):
    native_tool_calling = True
    lean_system_prompt = True

    def __init__(self, config: dict):
        self._vision_cache: dict[str, bool] = {}
        ollama_cfg = dict(config.get("ollama") or {})
        self._base_url = str(
            _env_or_cfg(config, "KIM_OLLAMA_BASE_URL", "ollama", "base_url", default=ollama_cfg.get("base_url") or DEFAULT_OLLAMA_BASE_URL)  # noqa: E501
        ).rstrip("/")
        self._mode = str(
            _env_or_cfg(config, "KIM_OLLAMA_MODE", "ollama", "mode", default=ollama_cfg.get("mode") or "local")
        ).strip().lower()
        if self._mode not in {"local", "cloud"}:
            self._mode = "local"
        self._local_model = str(
            _env_or_cfg(config, "KIM_OLLAMA_LOCAL_MODEL", "ollama", "local_model", default=ollama_cfg.get("local_model") or "")  # noqa: E501
        ).strip()
        self._cloud_model = str(
            _env_or_cfg(config, "KIM_OLLAMA_CLOUD_MODEL", "ollama", "cloud_model", default=ollama_cfg.get("cloud_model") or DEFAULT_OLLAMA_CLOUD_MODEL)  # noqa: E501
        ).strip()
        self._context_override = _coerce_optional_int(
            _env_or_cfg(config, "KIM_OLLAMA_CONTEXT_LIMIT_OVERRIDE", "ollama", "context_limit_override", default=ollama_cfg.get("context_limit_override"))  # noqa: E501
        )
        self._keep_alive = str(
            _env_or_cfg(config, "KIM_OLLAMA_KEEP_ALIVE", "ollama", "keep_alive", default=ollama_cfg.get("keep_alive") or "5m")  # noqa: E501
        ).strip()
        self._timeout_s = 600.0
        # L11: True when _local_model was auto-picked (not user-configured).
        self._auto_selected_model = False
        # M13: per-model context-limit cache so every completion doesn't shell
        # out to `ollama ps` (and POST /api/show) again.
        self._context_limit_cache: dict[str, tuple[int | None, str | None]] = {}
        # F-INH-4: cache the per-turn liveness probe (/api/version) and tag list
        # (/api/tags) for the session so every turn doesn't pay two extra HTTP
        # round-trips. Both are invalidated on a transport error (re-probe so a
        # stopped daemon still surfaces the friendly message) and the tag cache
        # is refetched once if a model is unexpectedly reported missing (pulled
        # or removed mid-session).
        self._daemon_alive = False
        self._session_tags: list[dict] | None = None
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
    ) -> dict[str, Any]:
        # Check the daemon is up FIRST (#31). _fetch_tags() calls
        # raise_for_status(), so probing it before the liveness check surfaced a
        # raw httpx.ConnectError instead of the friendly "Ollama is installed
        # but not running" message when the daemon was stopped.
        await self._ensure_daemon_running()
        # Fetch tags once for local mode; both _resolve_selected_model and
        # _validate_model need the tag list and would otherwise each make a
        # separate HTTP round-trip. F-INH-4: reuse the session tag cache across
        # turns, refetching once if the selected model looks missing (stale
        # cache after a mid-session pull/remove).
        cached_tags: list[dict] | None = None
        if self._mode == "local":
            cached_tags = await self._fetch_tags_cached()
        try:
            model = await self._resolve_selected_model(tags=cached_tags)
            await self._validate_model(model, tags=cached_tags)
        except ProviderEnvironmentError:
            if self._mode == "local" and self._session_tags is not None:
                self._session_tags = None
                cached_tags = await self._fetch_tags_cached()
                model = await self._resolve_selected_model(tags=cached_tags)
                await self._validate_model(model, tags=cached_tags)
            else:
                raise

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
        except PermissionError:
            # Cloud request needs Ollama sign-in (_stream_chat_inner's "sign
            # in"/"unauthorized"/"forbidden" detection). Trigger `ollama
            # signin` (opens the user's browser) and poll it to completion —
            # bounded, cancellable, not a dead-end raise — then retry this
            # SAME turn once so the terminal picks the sign-in up without the
            # user restarting. A signed-in user never reaches this branch at
            # all (the request just succeeds), so this is the not-signed-in
            # path only — never a tax on the fast path.
            if self._mode != "cloud":
                raise
            await trigger_signin_and_wait()
            try:
                final_obj, content, tool_calls = await self._stream_chat(_build_payload(messages))
            except (httpx.HTTPError, TimeoutError, ConnectionError):
                self._daemon_alive = False
                self._session_tags = None
                raise
        except EnvironmentError as exc:
            if _looks_like_vision_model_error(str(exc).lower()) and self._messages_have_images(messages):
                # Model doesn't support images — cache and retry without them.
                logger.info("OllamaProvider: %s doesn't support vision; retrying without images.", model)
                self._vision_cache[model] = False
                messages = self._strip_images_from_messages(messages)
                final_obj, content, tool_calls = await self._stream_chat(_build_payload(messages))
            else:
                raise
        except (httpx.HTTPError, TimeoutError, ConnectionError):
            # F-INH-4: a transport failure means our cached liveness/tag beliefs
            # may be wrong — re-probe next turn so a stopped daemon surfaces the
            # friendly "not running" message instead of a raw transport error.
            self._daemon_alive = False
            self._session_tags = None
            raise

        usage = await self._usage_from_final(final_obj, model)

        # F-B-3: Ollama's final chunk carries done_reason ("stop"|"length"|
        # "load"). Ollama was the only API provider that never surfaced it, so
        # a num_ctx-clipped / output-limited answer reached the agent as a
        # complete reply. Thread it through finalize_text_content (which maps
        # "length" → a truncation note) and expose stop_reason on every shape.
        done_reason = str(final_obj.get("done_reason") or "").strip() or None

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
                    "stop_reason": done_reason,
                    "usage": usage,
                }

            tc0 = tool_calls[0]
            fn0 = tc0.get("function") if isinstance(tc0, dict) else None
            name0 = str((fn0 or {}).get("name") or tc0.get("name") or "").strip()
            args0, arg_error = _normalize_tool_arguments_checked((fn0 or {}).get("arguments"))
            if arg_error:
                # F-INH-3: don't dispatch with silently-emptied args (an
                # all-optional schema would run with defaults and the model
                # never learns). Surface a re-emit nudge as the text turn.
                return {
                    "type": "text",
                    "content": malformed_tool_args_text(name0),
                    "stop_reason": done_reason,
                    "usage": usage,
                }
            return {
                "type": "tool_call",
                "tool": name0,
                "args": args0,
                "content": content,
                "stop_reason": done_reason,
                "usage": usage,
            }

        return {
            "type": "text",
            "content": finalize_text_content(content, done_reason),
            "stop_reason": done_reason,
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
                        "text": (
                            "[Screenshot unavailable: this model does not support vision. "
                            "Answer from the open-window fallback in the task message and begin with "
                            "\"I couldn't grab a screenshot, but you have these windows open:\".]"
                        ),
                    })
                cleaned.append({**msg, "content": new_content})
            else:
                cleaned.append(msg)
        return cleaned

    async def _ensure_daemon_running(self) -> None:
        # F-INH-4: probe /api/version once per session; a transport failure
        # elsewhere resets this flag so a daemon that dies mid-session is
        # re-probed and yields the friendly message again.
        if self._daemon_alive:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/version")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderEnvironmentError(
                    "Ollama is installed but not running. Start Ollama, then try again."
                ) from exc
        self._daemon_alive = True

    async def _fetch_tags_cached(self) -> list[dict]:
        """Session-cached /api/tags (F-INH-4). Invalidated on transport error
        and on a stale-model miss (see complete())."""
        if self._session_tags is None:
            self._session_tags = await self._fetch_tags()
        return self._session_tags

    async def _resolve_selected_model(self, tags: list[dict] | None = None) -> str:
        if self._mode == "cloud":
            return self._cloud_model or DEFAULT_OLLAMA_CLOUD_MODEL
        if self._local_model:
            # L11: an AUTO-selected model may be removed mid-session; re-check
            # it against the installed tags and re-select instead of failing
            # every later turn with "not installed". A user-configured model
            # is never second-guessed here (_validate_model reports it).
            if not self._auto_selected_model:
                return self._local_model
            if tags is None:
                tags = await self._fetch_tags()
            names = {
                str(item.get("name") or "").strip().lower()
                for item in tags
                if isinstance(item, dict)
            }
            if self._local_model.lower() in names:
                return self._local_model
            logger.info(
                "OllamaProvider: auto-selected model %r is no longer installed — re-selecting.",
                self._local_model,
            )
            self._local_model = ""

        if tags is None:
            tags = await self._fetch_tags()
        if tags:
            first = str((tags[0] or {}).get("name") or "").strip()
            if first:
                self._local_model = first
                self._auto_selected_model = True
                return first
        raise ProviderEnvironmentError(
            "No local Ollama models are installed. Pull a model in Settings → AI → Ollama, then try again."
        )

    async def _validate_model(self, model: str, tags: list[dict] | None = None) -> None:
        if self._mode == "cloud":
            if not model.strip():
                raise ProviderEnvironmentError(
                    "No Ollama cloud model selected. Pick a model in Settings → AI → Ollama, then try again."
                )
            return

        if tags is None:
            tags = await self._fetch_tags()
        names = {
            str(item.get("name") or "").strip().lower()
            for item in tags
            if isinstance(item, dict)
        }
        if model.strip().lower() not in names:
            raise ProviderEnvironmentError(
                f"Ollama local model {model!r} is not installed. Pull it in Settings → AI → Ollama or pick another model."  # noqa: E501
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
        # Assign a unique id to each assistant tool call and reuse it on the
        # matching tool-result message, so strict servers can pair them. The
        # pending pair is (tool_name, call_id) for the most recent unanswered call.
        call_seq = 0
        # FIFO of unanswered (tool_name, call_id) pairs. A list (not a single
        # slot) so interleaved/parallel tool calls each keep their own id: a
        # result matches the oldest pending call of the same name (#40).
        pending_calls: list[tuple[str, str]] = []

        def _match_pending(result_text: str) -> tuple[str, str] | None:
            name = _tool_result_name(result_text)
            if name is None:
                return None
            for i, (nm, _cid) in enumerate(pending_calls):
                if nm == name:
                    return pending_calls.pop(i)
            return None

        if system.strip():
            out.append({"role": "system", "content": system})
        for msg in messages:
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            if role == "assistant" and isinstance(content, str):
                native_tool_call = _assistant_tool_call_message(content, call_id=f"call_{call_seq}")
                if native_tool_call:
                    call_seq += 1
                    tc = native_tool_call["tool_calls"][0]
                    pending_calls.append((tc["function"]["name"], tc["id"]))
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
                text_content = "\n".join([p for p in text_parts if p]).strip()

                # F-B-4: detect a tool result INDEPENDENT of whether it carries
                # an image. A screenshot tool result (text + image) previously
                # skipped pairing entirely — the assistant tool_call it answered
                # stayed pending forever (strict-server 400 on an unanswered
                # tool_call, plus an off-by-one id cascade as the stale entry
                # was popped by the next same-named result). Only convert when a
                # pending call of that name exists; an unmatched [Tool result: x]
                # (user-pasted transcript, or a trimmed-history orphan) stays a
                # plain message. Ollama accepts images on any message, so attach
                # them to the role:"tool" message.
                matched = _match_pending(text_content)
                if matched is not None:
                    tool_result = _tool_result_message(role, text_content, pending=matched)
                    if tool_result:
                        if images:
                            tool_result["images"] = images
                        out.append(tool_result)
                        continue

                converted: dict[str, Any] = {"role": role, "content": text_content}
                if images:
                    converted["images"] = images
                out.append(converted)
            else:
                text = str(content or "")
                matched = _match_pending(text)
                if matched is not None:
                    tool_result = _tool_result_message(role, text, pending=matched)
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
        try:
            return await self._stream_chat_inner(payload)
        except httpx.TimeoutException as exc:
            # F-B-5: re-raise httpx transport timeouts as a builtin TimeoutError
            # (mirroring claude.py/openai_provider.py) so classify_provider_error
            # marks the single most transient failure a local daemon has —
            # model cold-load exceeding the connect window, or the 600s read
            # ceiling — retryable instead of a non-retryable "unknown".
            raise TimeoutError(
                f"Ollama request to {self._base_url} timed out: {exc or type(exc).__name__}"
            ) from exc

    async def _stream_chat_inner(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        pieces: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        final_obj: dict[str, Any] | None = None

        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s, connect=10.0)) as client:
            async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", errors="replace").strip()
                    lowered = detail.lower()
                    if _looks_like_vision_model_error(lowered):
                        raise ProviderEnvironmentError(
                            "The selected Ollama model does not appear to support images. "
                            "Pick a vision-capable Ollama model or use structured UI observation instead of screenshots."  # noqa: E501
                        )
                    if "not found" in lowered or "pull" in lowered:
                        raise ProviderEnvironmentError(
                            f"Ollama model {payload.get('model')!r} is unavailable. Pull it in Settings → AI → Ollama or pick another model."  # noqa: E501
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
                            raise ProviderEnvironmentError(
                                "The selected Ollama model does not appear to support images. "
                                "Pick a vision-capable Ollama model or use structured UI observation instead of screenshots."  # noqa: E501
                            )
                        if "not found" in lowered or "pull" in lowered:
                            raise ProviderEnvironmentError(
                                f"Ollama model {payload.get('model')!r} is unavailable. Pull it in Settings → AI → Ollama or pick another model."  # noqa: E501
                            )
                        raise RuntimeError(detail)

                    msg = item.get("message") if isinstance(item.get("message"), dict) else {}
                    chunk = msg.get("content")
                    if isinstance(chunk, str) and chunk:
                        pieces.append(chunk)
                    tc_deltas = msg.get("tool_calls")
                    if isinstance(tc_deltas, list):
                        for position, delta in enumerate(tc_deltas):
                            if not isinstance(delta, dict):
                                continue
                            idx = _resolve_tool_call_index(delta, position)
                            while len(tool_calls) <= idx:
                                tool_calls.append({})
                            _accumulate_tool_call_delta(tool_calls[idx], delta)
                    if item.get("done") is True:
                        final_obj = item

        if final_obj is None:
            raise RuntimeError("Ollama stream ended without a final done response.")

        _raw_msg = final_obj.get("message")
        final_message: dict = _raw_msg if isinstance(_raw_msg, dict) else {}
        if not pieces and isinstance(final_message.get("content"), str):
            pieces.append(str(final_message.get("content") or ""))
        if not tool_calls and isinstance(final_message.get("tool_calls"), list):
            tool_calls = [x for x in (final_message.get("tool_calls") or []) if isinstance(x, dict)]
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
            "billing": "Local: no API billing" if self._mode == "local" else "Cloud account usage is managed by Ollama. Kim can show token usage, not remaining account balance.",  # noqa: E501
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
        else:
            # L10: no separate override branch — _resolve_context_limit already
            # returns (self._context_override, "override") when ps/show fail,
            # so context_limit is None only when there is no override either.
            usage["context_limit_source"] = context_source or "unknown"
        return usage

    async def _resolve_context_limit(self, model: str) -> tuple[int | None, str | None]:
        # M13: `ollama ps` + /api/show can add seconds per turn — resolve once
        # per model and reuse. Only positive answers are cached so a transient
        # failure doesn't pin (None, None) for the whole session.
        cached = self._context_limit_cache.get(model)
        if cached is not None:
            return cached

        ps_limit = await asyncio.to_thread(self._context_limit_from_ps_sync, model)
        if ps_limit:
            self._context_limit_cache[model] = (ps_limit, "ollama_ps")
            return ps_limit, "ollama_ps"

        show_limit = await self._context_limit_from_show(model)
        if show_limit:
            self._context_limit_cache[model] = (show_limit, "api_show")
            return show_limit, "api_show"

        if self._context_override:
            return self._context_override, "override"
        return None, None

    def _context_limit_from_ps_sync(self, model: str) -> int | None:
        # F-B-6: point the `ollama` CLI at the CONFIGURED daemon. Without
        # OLLAMA_HOST the CLI always queries localhost, so with a remote
        # base_url (KIM_OLLAMA_BASE_URL / ollama.base_url) the reported
        # context_limit could describe a different daemon's loaded model — or
        # the CLI errors when nothing runs locally while the remote is healthy.
        # If the CLI isn't installed we fall back to /api/show against base_url.
        env = {**os.environ, "OLLAMA_HOST": self._base_url}
        try:
            proc = subprocess.run(
                ["ollama", "ps"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=env,
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


def _resolve_tool_call_index(delta: dict, position: int) -> int:
    """Pick the accumulator slot for a streamed tool-call delta (#38).

    Ollama's native /api/chat usually omits an index and can return several
    complete tool calls in a single message's ``tool_calls`` array. The old code
    read ``delta.get("index", 0)`` and so collapsed every call in that array into
    slot 0 — losing all but one. Prefer an explicit index when present (top-level
    or nested under ``function``, for OpenAI-style servers), otherwise fall back
    to the delta's position in the array. Using the position preserves
    cross-chunk fragment accumulation (each chunk carries one entry at position
    0) while separating multiple whole calls that arrive together.
    """
    for candidate in (
        delta.get("index"),
        delta["function"].get("index") if isinstance(delta.get("function"), dict) else None,
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return position


def _accumulate_tool_call_delta(acc: dict, delta: dict) -> None:
    """Merge a streaming tool-call delta into an accumulator entry.

    Whole-block servers (current Ollama default) send a single chunk that
    contains the full tool call with a dict for ``arguments``.  Delta-streaming
    servers send name/arguments as string fragments across multiple chunks.
    Both cases are handled: dict arguments are stored as-is on the first
    occurrence; string fragments are concatenated.
    """
    _fn = delta.get("function")
    fn_delta: dict = _fn if isinstance(_fn, dict) else {}
    acc_fn: dict = acc.setdefault("function", {"name": "", "arguments": ""})

    name = str(fn_delta.get("name") or "").strip()
    if name:
        acc_fn["name"] = name

    args = fn_delta.get("arguments")
    if isinstance(args, str):
        # Delta-streaming: concatenate argument fragments.
        existing = acc_fn.get("arguments", "")
        acc_fn["arguments"] = (existing if isinstance(existing, str) else "") + args
    elif isinstance(args, dict) and not acc_fn.get("arguments"):
        # Whole-block: use the dict directly (only on first/only chunk).
        acc_fn["arguments"] = args

    # Preserve top-level fields (id, type) from the first chunk that has them.
    for key in ("id", "type"):
        if key in delta and key not in acc:
            acc[key] = delta[key]


def _normalize_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning(
                    "Ollama tool-call arguments are not an object (%r) — using {}",
                    raw[:200],
                )
                return {}
            return parsed
        except json.JSONDecodeError:
            # M9: never silently swap malformed args for {} — the tool then
            # fails with a confusing "missing argument" error and no trace of
            # what the model actually produced.
            logger.warning(
                "Ollama tool-call arguments are not valid JSON (%r) — using {}",
                raw[:200],
            )
            return {}
    return {}


def _normalize_tool_arguments_checked(raw: Any) -> tuple[dict[str, Any], bool]:
    """Like _normalize_tool_arguments, but reports a genuine JSON parse failure.

    Returns (args, arg_error). arg_error is True only when a string payload is
    unparseable JSON (F-INH-3) — a valid-JSON-but-non-object value still coerces
    to {} without signalling, matching the pre-existing lenient behavior.
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Ollama tool-call arguments are not valid JSON (%r) — signalling re-emit",
                raw[:200],
            )
            return {}, True
        if not isinstance(parsed, dict):
            logger.warning(
                "Ollama tool-call arguments are not an object (%r) — using {}",
                raw[:200],
            )
            return {}, False
        return parsed, False
    return _normalize_tool_arguments(raw), False


def _assistant_tool_call_message(raw_content: str, call_id: str | None = None) -> dict[str, Any] | None:
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
    tool_call: dict[str, Any] = {
        "type": "function",
        "function": {
            "index": 0,
            "name": name,
            "arguments": args,
        },
    }
    tool_call["id"] = call_id if call_id else name
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [tool_call],
    }


_TOOL_RESULT_RE = re.compile(r"^\s*\[Tool result:\s*([A-Za-z0-9_.:-]+)\]\s*\n?([\s\S]*)$")


def _tool_result_name(text: str) -> str | None:
    """Tool name if `text` is a ``[Tool result: <name>]`` payload, else None.

    Used to pair a result with the correct pending assistant tool call before
    building the tool message (#40).
    """
    match = _TOOL_RESULT_RE.match(text or "")
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def _tool_result_message(
    role: str,
    text: str,
    pending: tuple[str, str] | None = None,
) -> dict[str, Any] | None:
    if role != "user":
        return None
    match = _TOOL_RESULT_RE.match(text)
    if not match:
        return None
    name = match.group(1).strip()
    body = match.group(2).strip()
    if not name:
        return None
    # Reuse the id of the pending tool call when names match; orphan results
    # (resumed sessions, trimmed history) fall back to the tool name.
    call_id = pending[1] if pending and pending[0] == name else name
    return {
        "role": "tool",
        "tool_call_id": call_id,
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
