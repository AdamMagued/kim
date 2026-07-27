"""
In-app webview bridge client for the browser provider.

Handles completion via Kim desktop's Tauri-managed webview bridge,
using the split send/result API with legacy fallback.
"""

import asyncio
import logging
import re
import time
from typing import Callable, Optional

import httpx

from orchestrator.providers.browser.response_parser import parse_response
from orchestrator.providers.browser.site_configs import SITE_CONFIGS, _BRIDGE_TIMEOUT_S

logger = logging.getLogger(__name__)

# Structural honesty note appended when the bridge reports that NO attachment
# actually reached the provider even though we sent an image (7.2/7.3): the
# site model was then answering "describe what you see" WITHOUT the image, so
# the reply must not be presented as if it had vision.
_IMAGE_NOT_DELIVERED_NOTE = (
    "\n\n[Kim note: the screenshot could NOT be attached to the browser "
    "provider — the reply above was produced WITHOUT seeing the image and "
    "may describe the screen inaccurately.]"
)


def _apply_attachment_signal(result: dict, data: dict, sent_attachments: list[dict]) -> dict:
    """Gate the "image attached" claim on the bridge's REAL upload signal.

    bridge.js returns ``attachments_uploaded`` in its done payload. When the
    field is present (newer desktop builds; requires the Rust /v1/result
    handler to forward it — see results.rs) and it says 0 attachments landed
    while we sent an image, append a structural disclaimer to text replies so
    the confabulation risk is visible instead of silent. The raw count is
    also attached to the result dict for agent-side gating.

    When the field is absent (older bridge), behavior is unchanged.
    """
    uploaded = data.get("attachments_uploaded")
    if uploaded is None:
        return result
    try:
        uploaded_n = int(uploaded)
    except (TypeError, ValueError):
        return result

    result["attachments_uploaded"] = uploaded_n
    images_sent = sum(
        1 for a in sent_attachments
        if str(a.get("mime_type", "")).startswith("image/")
    )
    if images_sent and uploaded_n == 0 and result.get("type") == "text":
        content = str(result.get("content") or "")
        result["content"] = content + _IMAGE_NOT_DELIVERED_NOTE
        logger.warning(
            "Bridge reported attachments_uploaded=0 for %d image(s) — "
            "appended not-delivered disclaimer to the reply.",
            images_sent,
        )
    return result


# The Chrome Extension bridge talks to chatgpt.com and nothing else
# (chrome_extension/injected.js posts to /backend-api/conversation). Routing a
# gemini/deepseek request through it because a ChatGPT tab happened to be open
# would silently answer from the wrong provider — the user asked for one model
# and would get another with no notice.
_EXTENSION_BRIDGE_SITE = "chatgpt"


def _extension_bridge_serves(preferred_site: Optional[str]) -> bool:
    """True when `preferred_site` is one the Chrome Extension can actually serve.

    An unset site means "whatever the bridge defaults to", which is chatgpt —
    so that case routes through the extension too.
    """
    if not preferred_site:
        return True
    # Sites carry an optional model suffix ("chatgpt:gpt-5.6-sol").
    return str(preferred_site).split(":", 1)[0].strip().lower() == _EXTENSION_BRIDGE_SITE


async def _try_extension_bridge(
    *,
    prompt: str,
    completion_hash: str,
    known_tools: Optional[set[str]],
    clear_chat: bool,
    attachments: list[dict],
    on_delta: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[dict], bool]:
    """Attempt the Chrome Extension WebSocket path.

    Returns ``(reply, attempted)``. ``reply`` is None when the caller should
    fall through. ``attempted`` distinguishes "the extension was never
    connected, so this deployment isn't using it" from "the extension WAS
    connected and the turn failed" — only the latter should be reported as a
    bridge failure rather than silently retried on another transport.
    """
    attempted = False
    try:
        from orchestrator.providers.browser.extension_bridge import get_extension_bridge
        bridge = await get_extension_bridge()
        if not await bridge.wait_for_connection(timeout=15.0):
            return {
                "type": "text",
                "content": "NEED_HELP: Chrome Extension bridge is not connected. Open chatgpt.com in Chrome."
            }, True
        attempted = True

        if attachments:
            logger.info(f"[Kim Bridge] Forwarding {len(attachments)} attachment(s) to Chrome Extension over WebSocket bridge.")

        accumulated_delta: list[str] = []
        last_printed_prose: list[str] = [""]
        last_print_time: list[float] = [0.0]

        def _on_extension_delta(delta: str):
            if not delta:
                return
            accumulated_delta.append(delta)
            full_so_far = "".join(accumulated_delta)
            
            # Clean JSON syntax or code fences from prose
            prose = full_so_far
            if prose.startswith("{"):
                m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', prose)
                prose = m.group(1).replace('\\"', '"').replace('\\n', '\n') if m else ""
            if "```" in prose:
                prose = prose.split("```", 1)[0]
            prose = re.sub(r"\[END_OF_RESPONSE(?:_[A-Za-z0-9-]+)?\]", "", prose, flags=re.IGNORECASE).strip()
            
            if not prose:
                return
            clean_prose = " ".join(prose.split())[:200]
            now = time.time()
            is_boundary = delta.endswith(("\n", ". ", "! ", "? ", ": ")) or "\n" in delta
            time_elapsed = (now - last_print_time[0]) >= 1.5
            if clean_prose and clean_prose != last_printed_prose[0] and (is_boundary or time_elapsed):
                last_printed_prose[0] = clean_prose
                last_print_time[0] = now
                print(f"[STATUS] {clean_prose}", flush=True)

            if on_delta is not None:
                try:
                    on_delta(delta)
                except Exception as exc:
                    logger.warning(f"[Kim Bridge] External on_delta callback raised: {exc}")

        logger.info("[Kim Bridge] Dispatching prompt to Chrome Extension over WebSocket...")
        res = await bridge.send_completion(prompt, on_delta=_on_extension_delta, clear_chat=clear_chat, attachments=attachments)
    except Exception as exc:
        logger.warning(f"[Kim Bridge] Extension bridge send failed: {exc or exc.__class__.__name__}")
        return {
            "type": "text",
            "content": f"NEED_HELP: Extension bridge error ({exc}). Check chatgpt.com in Chrome."
        }, attempted

    full_text = res.get("full_text", "")
    if not isinstance(full_text, str) or not full_text.strip():
        # Mirrors the webview path's empty-response guard: an empty string
        # parses as a blank final answer, so codex ends the turn having done
        # nothing and the user sees a silent no-op instead of a diagnosis.
        logger.warning("[Kim Bridge] Extension returned an empty response.")
        return {
            "type": "text",
            "content": (
                "NEED_HELP: The Chrome Extension bridge returned an empty response. "
                "Check that the chatgpt.com tab is open and signed in."
            ),
        }, attempted

    result = parse_response(full_text, completion_hash, known_tools=known_tools)
    if attachments:
        result["attachments_uploaded"] = 0
    return result, attempted


async def complete_via_webview_bridge(
    *,
    bridge_url: str = "",
    bridge_token: str = "",
    preferred_site: Optional[str] = None,
    model_tier: Optional[str] = None,
    gemini_authuser: Optional[int] = None,
    prompt: str = "",
    attachments: Optional[list[dict]] = None,
    completion_hash: str = "",
    known_tools: Optional[set[str]] = None,
    clear_chat: bool = False,
    site_configs: Optional[dict] = None,
    effort: Optional[str] = None,
    on_delta: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run completion through Kim desktop's in-app webview bridge or Chrome Extension bridge."""
    attachments = list(attachments or [])

    if _extension_bridge_serves(preferred_site):
        result, attempted = await _try_extension_bridge(
            prompt=prompt,
            completion_hash=completion_hash,
            known_tools=known_tools,
            clear_chat=clear_chat,
            attachments=attachments,
            on_delta=on_delta,
        )
        if result is not None:
            return result
        if attempted and not bridge_url:
            # The extension bridge WAS the transport for this deployment and it
            # failed mid-turn (timeout, socket drop). Falling through to the
            # desktop webview bridge means POSTing to ""+"/v1/send", whose only
            # possible outcome is `Request URL is missing an 'http://' ...` —
            # a nonsense error that buries the real cause (observed live: a
            # 180s browser timeout surfaced to the user as a malformed-URL
            # message). Report what actually happened instead.
            return {
                "type": "text",
                "content": (
                    "NEED_HELP: The Chrome Extension bridge did not answer this turn "
                    "(browser timeout or disconnect) and no desktop webview bridge is "
                    "configured to fall back to. Check the chatgpt.com tab is open, "
                    "signed in, and not showing a rate-limit or verification prompt, "
                    "then retry."
                ),
            }

    known_sites = site_configs or SITE_CONFIGS
    # The browser:claude site was removed from SITE_CONFIGS entirely (feat/
    # kimcli-61) — "claude" is no longer a fallback candidate on the Python
    # side. Default to the first known site (chatgpt, ordered first in
    # SITE_CONFIGS) instead, so a missing preferred_site doesn't route
    # through a site Kim's own config no longer recognizes.
    _default_site = "chatgpt" if "chatgpt" in known_sites else next(iter(known_sites), "chatgpt")
    site = preferred_site or _default_site
    if site not in known_sites:
        # L5: never silently reroute an unknown/typo'd preferred_site — the
        # user asked for one provider and would get another with no notice.
        logger.warning(
            "Unknown preferred_site %r (known: %s) — falling back to %s.",
            site, ", ".join(sorted(known_sites)), _default_site,
        )
        site = _default_site

    bridge_attachments: list[dict] = []
    max_attachments = 8
    max_attachment_bytes = 10 * 1024 * 1024
    for i, attachment in enumerate(attachments[:max_attachments], start=1):
        data_b64 = str(attachment.get("data_base64", "")).strip()
        mime_type = str(attachment.get("mime_type", "application/octet-stream")).strip()
        if not data_b64:
            continue
        approx_size = (len(data_b64) * 3) // 4
        if approx_size > max_attachment_bytes:
            logger.warning(
                f"Skipping oversized bridge attachment #{i} ({approx_size} bytes, {mime_type})"
            )
            continue
        bridge_attachments.append(
            {
                "name": str(attachment.get("name", f"attachment_{i}")),
                "mime_type": mime_type,
                "data_base64": data_b64,
            }
        )

    if len(attachments) > max_attachments:
        prompt = (
            f"{prompt}\n\n"
            f"[Kim note: {len(attachments) - max_attachments} additional attachment(s) "
            "were omitted due to attachment limit.]"
        )

    headers = {"X-Kim-Token": bridge_token}
    payload = {
        "site": site,
        "prompt": prompt,
        "attachments": bridge_attachments,
        "completion_hash": completion_hash,
        "clear_chat": bool(clear_chat),
    }
    if model_tier:
        payload["model_tier"] = model_tier
    if effort:
        # FIX 1: forwarded to Rust's /v1/send, which threads it through to
        # bridge.js's on-page effort picker (see bridge_effort.js).
        payload["effort"] = effort

    if site == "gemini" and gemini_authuser is not None:
        payload["authuser"] = gemini_authuser
        logger.info("Routing Gemini WebView via authuser=%s", gemini_authuser)

    # ── Try split send/result API first ──────────────────────────────
    try:
        logger.info(f"[STATUS] Sending message to {site}…")
        async with httpx.AsyncClient(timeout=30) as send_client:
            send_resp = await send_client.post(
                f"{bridge_url}/v1/send",
                headers=headers,
                json=payload,
            )

        if send_resp.status_code == 404:
            logger.info("Bridge /v1/send returned 404, falling back to /v1/complete")
            return await _complete_via_webview_bridge_legacy(
                bridge_url, prompt, headers, payload, completion_hash, known_tools
            )

        try:
            send_data = send_resp.json()
        except ValueError:
            body_preview = send_resp.text[:300]
            return {
                "type": "text",
                "content": (
                    "NEED_HELP: Bridge /v1/send returned invalid JSON "
                    f"(status {send_resp.status_code}): {body_preview}"
                ),
            }

        if send_resp.status_code == 409:
            msg = send_data.get("error") or (
                "Kim opened the in-app browser window. Sign in and resend your task."
            )
            return {"type": "text", "content": f"NEED_HELP: {msg}"}

        if send_resp.status_code >= 400:
            msg = send_data.get("error") or f"HTTP {send_resp.status_code}"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge send error — {msg}",
            }

        req_id = send_data.get("req_id")
        if not req_id:
            return {
                "type": "text",
                "content": "NEED_HELP: Bridge /v1/send did not return a req_id.",
            }

        sent_confirmed = send_data.get("sent_confirmed", False)
        logger.info(
            f"Bridge send OK: req_id={req_id}, site={send_data.get('site')}, "
            f"confirmed={sent_confirmed}"
        )
        if sent_confirmed:
            logger.info("[STATUS] Kim is working…")
        else:
            logger.info("[STATUS] Kim is preparing the request…")

    except httpx.ReadTimeout:
        logger.warning("Bridge /v1/send timed out (prompt may already be injected)")
        return {
            "type": "text",
            "content": "NEED_HELP: Bridge /v1/send timed out. The prompt may have been partially sent. Please check the browser window and retry if needed.",  # noqa: E501
        }
    except Exception as e:
        logger.warning(f"Bridge /v1/send failed ({e})")
        return {
            "type": "text",
            "content": f"NEED_HELP: Bridge /v1/send failed — {e}",
        }

    # ── Long-poll for result ─────────────────────────────────────────
    # F-B-11: the message was already delivered by /v1/send, so re-polling
    # /v1/result/{req_id} is IDEMPOTENT (the Rust side still holds the result;
    # the send is NOT re-issued). A transient GET failure — connection reset,
    # bridge busy blip — must therefore retry a few times instead of abandoning
    # a delivered request with a terminal NEED_HELP (which drives the user to
    # resend and duplicate the message, F-B-8's user-driven twin). A genuine
    # long-poll timeout (the full window elapsed) is still terminal.
    logger.info("[STATUS] Kim is thinking…")
    result_resp = None
    last_transport_error: Optional[Exception] = None
    max_result_attempts = 3
    for attempt in range(1, max_result_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT_S) as result_client:
                result_resp = await result_client.get(
                    f"{bridge_url}/v1/result/{req_id}",
                    headers=headers,
                )
            break
        except httpx.TimeoutException as e:
            logger.error("Bridge /v1/result timed out", exc_info=True)
            detail = str(e).strip() or "Timed out waiting for provider response"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge timeout — {detail}",
            }
        except httpx.TransportError as e:
            last_transport_error = e
            logger.warning(
                "Bridge /v1/result attempt %d/%d failed (%s) — re-polling req_id=%s",
                attempt, max_result_attempts, e, req_id,
            )
            if attempt < max_result_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                continue
        except Exception as e:
            logger.error(f"Bridge /v1/result failed: {e}", exc_info=True)
            detail = str(e).strip() or e.__class__.__name__
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge result poll failed — {detail}",
            }

    if result_resp is None:
        detail = (
            str(last_transport_error).strip()
            or (last_transport_error.__class__.__name__ if last_transport_error else "unknown")
        )
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge result poll failed — {detail}",
        }

    try:
        data = result_resp.json()
    except ValueError:
        body_preview = result_resp.text[:300]
        return {
            "type": "text",
            "content": (
                "NEED_HELP: In-app browser bridge returned invalid JSON "
                f"(status {result_resp.status_code}): {body_preview}"
            ),
        }

    if result_resp.status_code >= 400:
        msg = data.get("error") or f"HTTP {result_resp.status_code}"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge error — {msg}",
        }

    if not data.get("ok", False):
        msg = data.get("error") or "Unknown in-app bridge failure"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser execution failed — {msg}",
        }

    logger.info(f"[STATUS] Reading {site}'s response…")
    raw_response = data.get("response")
    if not raw_response or not isinstance(raw_response, str) or not raw_response.strip():
        return {
            "type": "text",
            "content": "NEED_HELP: In-app browser bridge returned an empty response.",
        }

    result = parse_response(
        raw_response.strip(), completion_hash, known_tools=known_tools
    )
    return _apply_attachment_signal(result, data, bridge_attachments)


async def _complete_via_webview_bridge_legacy(
    bridge_url: str,
    prompt: str,
    headers: dict,
    payload: dict,
    completion_hash: str,
    known_tools: Optional[set[str]] = None,
) -> dict:
    """Monolithic /v1/complete fallback for older Rust binaries."""
    try:
        logger.info("[STATUS] Sending to AI provider…")
        async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{bridge_url}/v1/complete",
                headers=headers,
                json=payload,
            )
    except httpx.ReadTimeout as e:
        logger.error("In-app bridge request timed out", exc_info=True)
        detail = str(e).strip() or "Bridge request timed out while waiting for provider response"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge timeout — {detail}",
        }
    except Exception as e:
        logger.error(f"In-app bridge request failed: {e}", exc_info=True)
        detail = str(e).strip() or e.__class__.__name__
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge request failed — {detail}",
        }

    try:
        data = resp.json()
    except ValueError:
        body_preview = resp.text[:300]
        return {
            "type": "text",
            "content": (
                "NEED_HELP: In-app browser bridge returned invalid JSON "
                f"(status {resp.status_code}): {body_preview}"
            ),
        }

    if resp.status_code == 409:
        msg = data.get("error") or (
            "Kim opened the in-app browser window. Sign in and resend your task."
        )
        return {"type": "text", "content": f"NEED_HELP: {msg}"}

    if resp.status_code >= 400:
        msg = data.get("error") or f"HTTP {resp.status_code}"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge error — {msg}",
        }

    if not data.get("ok", False):
        msg = data.get("error") or "Unknown in-app bridge failure"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser execution failed — {msg}",
        }

    raw_response = data.get("response")
    if not raw_response or not isinstance(raw_response, str) or not raw_response.strip():
        return {
            "type": "text",
            "content": "NEED_HELP: In-app browser bridge returned an empty response.",
        }

    result = parse_response(
        raw_response.strip(), completion_hash, known_tools=known_tools
    )
    return _apply_attachment_signal(
        result, data, list(payload.get("attachments") or [])
    )
