"""
API key authentication for the Kim relay server.

Two static keys plus per-device tokens are enforced:
  RELAY_PHONE_API_KEY  — legacy master key for phone endpoints. Kept so older
                         clients (and integration tests) keep working without
                         going through the pairing dance.
  RELAY_PC_API_KEY     — used by the PC agent to dequeue tasks and post results.
  device_token         — issued to a phone by `POST /pair/complete`. Treated as
                         equivalent to the phone master key for `/prompt`,
                         `/result/{id}`, `/status`, and the WebSocket.

The static keys are read from environment variables.  Missing the phone key
is fine if at least one device is paired; missing the PC key still means the
PC endpoints reject everything.

FastAPI dependency functions
────────────────────────────
  require_phone_key  — 401 if header doesn't match RELAY_PHONE_API_KEY *and*
                       isn't a registered device_token
  require_pc_key     — 401 if header doesn't match RELAY_PC_API_KEY
  require_any_key    — 401 if header matches neither phone-side credential nor
                       the PC key (used for /status)
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from relay_server.queue import db

logger = logging.getLogger(__name__)

# ── Load keys ─────────────────────────────────────────────────────────────────

_PHONE_KEY = os.environ.get("RELAY_PHONE_API_KEY", "")
_PC_KEY    = os.environ.get("RELAY_PC_API_KEY", "")

if not _PHONE_KEY:
    logger.info(
        "RELAY_PHONE_API_KEY is not set — phone endpoints will only accept "
        "device tokens issued through the pairing flow."
    )
if not _PC_KEY:
    logger.warning(
        "RELAY_PC_API_KEY is not set — all /prompt/next and /result endpoints "
        "will reject every request.  Set the env-var before deployment."
    )

# ── Header extractor ──────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── Dependency functions ──────────────────────────────────────────────────────

def _reject() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-API-Key header",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def _matches_phone_master(key: str | None) -> bool:
    return bool(_PHONE_KEY and key and secrets.compare_digest(key, _PHONE_KEY))


def _matches_pc(key: str | None) -> bool:
    return bool(_PC_KEY and key and secrets.compare_digest(key, _PC_KEY))


async def _matches_device(key: str | None) -> bool:
    if not key:
        return False
    # Defence in depth — never let an attacker probe with the empty string or
    # a literal "None" string and hit a row.
    if len(key) < 16:
        return False
    device = await db.lookup_device_by_token(key)
    if device is None:
        return False
    # Re-verify with constant-time comparison at the Python layer, mirroring
    # the pattern used for static keys above (secrets.compare_digest).
    # `device["device_token"]` is the value fetched from the DB for the matched
    # row; the comparison will always be True here, but using compare_digest
    # ensures the check is timing-safe even if the DB equality is not.
    stored = device.get("device_token", "")
    return bool(stored and secrets.compare_digest(key, stored))


async def require_phone_key(key: str | None = Security(_api_key_header)) -> None:
    """Phone-side credential: legacy master key OR a paired device_token."""
    if _matches_phone_master(key):
        return
    if await _matches_device(key):
        return
    _reject()


def require_pc_key(key: str | None = Security(_api_key_header)) -> None:
    """Dependency: request must carry the PC API key."""
    if not _matches_pc(key):
        _reject()


async def require_any_key(key: str | None = Security(_api_key_header)) -> None:
    """Either side: phone master, device_token, or PC key."""
    if _matches_pc(key) or _matches_phone_master(key):
        return
    if await _matches_device(key):
        return
    _reject()
