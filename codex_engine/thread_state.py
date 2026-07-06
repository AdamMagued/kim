"""Cross-task browser-thread state for the codex bridge route.

Each Code-tab / CLI code-mode task runs in its own ``codex_bridge_service``
process, so continuity of the browser thread (system prompt sent, running
token estimate, pending compact handoff) must live on disk. One JSON sidecar
per (project cwd, provider) pair, stored under the Kim repo's ``kim_sessions/``
— never inside the user's project.

State shape:
    {
        "sent_instructions": bool,  # codex system prompt already in the thread
        "turns": int,               # browser sends since the thread started
        "est_tokens": int,          # cumulative estimated in+out tokens
        "handoff": str | None,      # compact summary to seed the next fresh chat
        "sandbox": str,             # permission fingerprint ("default"/"bypass")
                                    # the thread's instructions described
        "updated_at": str,          # ISO timestamp of last write
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kim.codex_thread_state")

_REPO = Path(__file__).resolve().parent.parent
_STATE_DIR = _REPO / "kim_sessions" / "codex_threads"


def _state_path(cwd: str, provider: str) -> Path:
    digest = hashlib.sha256(f"{cwd}|{provider}".encode("utf-8")).hexdigest()[:16]
    return _STATE_DIR / f"{digest}.json"


def load_thread_state(cwd: str, provider: str) -> dict:
    path = _state_path(cwd, provider)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("Could not read codex thread state %s: %s", path, e)
    return {}


def save_thread_state(cwd: str, provider: str, state: dict) -> None:
    path = _state_path(cwd, provider)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.warning("Could not write codex thread state %s: %s", path, e)


def reset_thread_state(cwd: str, provider: str, handoff: Optional[str] = None) -> dict:
    """Reset accounting for a fresh thread, optionally carrying a handoff."""
    state = {
        "sent_instructions": False,
        "turns": 0,
        "est_tokens": 0,
        "handoff": (handoff or "").strip() or None,
    }
    save_thread_state(cwd, provider, state)
    return state
