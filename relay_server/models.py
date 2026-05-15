"""
Pydantic schemas for the Kim relay server.

Phone  → relay:  PromptRequest / PromptResponse
PC     → relay:  ResultRequest / ResultResponse
Phone  ← relay:  TaskStatusResponse (polling or WebSocket push)
Anyone ← relay:  StatusResponse
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Maximum task string length — guards against prompt-injection abuse
_MAX_TASK_LEN = 10_000


class PromptRequest(BaseModel):
    task: str = Field(..., description="Natural-language task for the PC agent")
    priority: int = Field(0, ge=0, le=10, description="Higher = dequeued first (0–10)")

    @field_validator("task")
    @classmethod
    def task_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task must not be empty")
        if len(v) > _MAX_TASK_LEN:
            raise ValueError(f"task exceeds {_MAX_TASK_LEN} character limit")
        return v


class PromptResponse(BaseModel):
    task_id: str
    queued: bool = True


class ResultRequest(BaseModel):
    task_id: str
    summary: str = ""
    screenshot: str = Field("", description="Base64-encoded PNG (may include data: URI prefix)")
    success: bool


class ResultResponse(BaseModel):
    ok: bool


class TaskStatusResponse(BaseModel):
    task_id: str
    status: Literal["pending", "running", "done", "failed"]
    summary: Optional[str] = None
    screenshot: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class StatusResponse(BaseModel):
    pc_connected: bool
    last_seen: Optional[str] = Field(None, description="ISO-8601 UTC timestamp of last PC poll")
    queue_depth: int = Field(..., description="Number of tasks currently in 'pending' state")


# ── Pairing ───────────────────────────────────────────────────────────────────

# Limits on the pair_code shape. Real codes are uppercase alphanum, ~6 chars;
# we accept up to 12 to leave room if we lengthen the format later.
_PAIR_CODE_MIN = 4
_PAIR_CODE_MAX = 12


class PairInitResponse(BaseModel):
    """Returned to the PC after `POST /pair/init` succeeds."""
    pair_code: str = Field(..., description="Short code to render in the QR")
    expires_at: str = Field(..., description="ISO-8601 UTC timestamp when the code stops being valid")


class PairCompleteRequest(BaseModel):
    """What the phone POSTs to `/pair/complete` after scanning the QR."""
    pair_code: str = Field(..., min_length=_PAIR_CODE_MIN, max_length=_PAIR_CODE_MAX)
    device_name: str = Field(..., min_length=1, max_length=64,
                             description="Human-readable label for the paired phone")

    @field_validator("pair_code")
    @classmethod
    def _normalize_code(cls, v: str) -> str:
        # Codes are rendered uppercase but be forgiving about case + whitespace
        # — saves a frustrating "INVALID CODE" loop when the user retypes by hand.
        return v.strip().upper()

    @field_validator("device_name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("device_name must not be empty")
        return v


class PairCompleteResponse(BaseModel):
    """Returned to the phone after a successful pairing. The phone stores
    `device_token` and uses it as `X-API-Key` for every subsequent request."""
    device_token: str
    device_id: str


class PairStatusResponse(BaseModel):
    """Polled by the PC's pairing UI to know when the QR has been scanned."""
    claimed: bool
    expired: bool
    device_name: Optional[str] = None
    claimed_at: Optional[str] = None
