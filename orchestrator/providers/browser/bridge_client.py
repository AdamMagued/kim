"""
In-app webview bridge client for the browser provider.

Handles completion via Kim desktop's Tauri-managed webview bridge,
using the split send/result API with legacy fallback.
"""

import logging
from typing import Optional

import httpx

from orchestrator.providers.browser.response_parser import parse_response
from orchestrator.providers.browser.site_configs import SITE_CONFIGS, _BRIDGE_TIMEOUT_S

logger = logging.getLogger(__name__)


async def complete_via_webview_bridge(
    *,
    bridge_url: str,
    bridge_token: str,
    preferred_site: Optional[str],
    model_tier: Optional[str],
    gemini_authuser: Optional[int],
    prompt: str,
    attachments: list[dict],
    completion_hash: str,
    clear_chat: bool = False,
    site_configs: Optional[dict] = None,
) -> dict:
    """Run completion through Kim desktop's in-app webview bridge."""
    if not bridge_url or not bridge_token:
        return {
            "type": "text",
            "content": "NEED_HELP: In-app browser bridge is not configured.",
        }

    site = preferred_site or "claude"
    known_sites = site_configs or SITE_CONFIGS
    if site not in known_sites:
        site = "claude"

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
                bridge_url, prompt, headers, payload, completion_hash
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
    try:
        logger.info("[STATUS] Kim is thinking…")
        async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT_S) as result_client:
            result_resp = await result_client.get(
                f"{bridge_url}/v1/result/{req_id}",
                headers=headers,
            )
    except httpx.ReadTimeout as e:
        logger.error("Bridge /v1/result timed out", exc_info=True)
        detail = str(e).strip() or "Timed out waiting for provider response"
        return {
            "type": "text",
            "content": f"NEED_HELP: In-app browser bridge timeout — {detail}",
        }
    except Exception as e:
        logger.error(f"Bridge /v1/result failed: {e}", exc_info=True)
        detail = str(e).strip() or e.__class__.__name__
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

    return parse_response(raw_response.strip(), completion_hash)


async def _complete_via_webview_bridge_legacy(
    bridge_url: str,
    prompt: str,
    headers: dict,
    payload: dict,
    completion_hash: str,
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

    return parse_response(raw_response.strip(), completion_hash)
