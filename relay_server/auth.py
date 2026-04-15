"""
API key authentication for the Kim relay server.

Two separate keys are enforced:
  RELAY_PHONE_API_KEY  — used by the phone (or any external caller) to submit
                         tasks and poll results.
  RELAY_PC_API_KEY     — used by the PC agent to dequeue tasks and post results.

Both are read from environment variables.  Missing keys at startup are a fatal
configuration error — the server refuses to start.

FastAPI dependency functions
────────────────────────────
  require_phone_key  — 401 if header doesn't match RELAY_PHONE_API_KEY
  require_pc_key     — 401 if header doesn't match RELAY_PC_API_KEY
  require_any_key    — 401 if header matches neither key (used for /status)
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

# ── Load keys ─────────────────────────────────────────────────────────────────

_PHONE_KEY = os.environ.get("RELAY_PHONE_API_KEY", "")
_PC_KEY    = os.environ.get("RELAY_PC_API_KEY", "")

if not _PHONE_KEY:
    logger.warning(
        "RELAY_PHONE_API_KEY is not set — all /prompt and /result/{id} endpoints "
        "will reject every request.  Set the env-var before deployment."
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


def require_phone_key(key: str | None = Security(_api_key_header)) -> None:
    """Dependency: request must carry the phone API key."""
    if not _PHONE_KEY or not key or not secrets.compare_digest(key, _PHONE_KEY):
        _reject()


def require_pc_key(key: str | None = Security(_api_key_header)) -> None:
    """Dependency: request must carry the PC API key."""
    if not _PC_KEY or not key or not secrets.compare_digest(key, _PC_KEY):
        _reject()


def require_any_key(key: str | None = Security(_api_key_header)) -> None:
    """Dependency: request must carry either the phone or PC key."""
    phone_ok = bool(_PHONE_KEY and key and secrets.compare_digest(key, _PHONE_KEY))
    pc_ok    = bool(_PC_KEY    and key and secrets.compare_digest(key, _PC_KEY))
    if not (phone_ok or pc_ok):
        _reject()
