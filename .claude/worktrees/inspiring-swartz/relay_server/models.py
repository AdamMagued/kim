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
