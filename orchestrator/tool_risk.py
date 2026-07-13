"""
Tool risk classification for Kim's HITL (human-in-the-loop) foundation.

classify_tool_risk(name, args) -> dict maps every registered MCP tool to a
risk level so that traces, approval gates, and future recovery logic can act
on risk tier without duplicating knowledge about individual tools.

Risk levels:
  high    — Irreversible or high blast-radius actions. Require human approval
             before autonomous execution in risk-aware HITL configurations.
             Includes arbitrary code execution, file deletion, permanent git
             history changes, and remote resource creation.
  medium  — State-changing but generally recoverable with effort. Includes
             file writes, form interactions, OS-level input injection, and
             web navigation.
  low     — Read-only or purely observational. Safe to execute autonomously.

Reason codes (stable strings for downstream branching):
  arbitrary_code_execution  — run_command, run_powershell, run_python, run_node
  file_deletion             — delete_file
  git_history_change        — git_commit (permanent commit written)
  git_branch_change         — git_checkout (can discard uncommitted changes)
  remote_resource_creation  — github_create_repo
  file_write                — write_file, edit_file
  git_stage                 — git_add
  form_interaction          — web_fill, web_press (could submit forms)
  web_navigation            — web_open, web_close, web_back, open_url
  input_injection           — type_text, hotkey, key_press
  native_click              — click, double_click, right_click, drag, scroll
  web_interaction           — web_click
  ui_interaction            — click_ui (native/accessibility click by element ID)
  window_management         — focus_window, resize_window
  memory_write              — write_memory
  read_only                 — all remaining tools (read, observe, search, lint, git queries)

Arg-level refinement (e.g. detecting force-push in run_command args, or
password fields in web_fill) is intentionally deferred — this module
classifies by tool name only to avoid duplicating shell.py's deny-list logic
and to stay stable as tool implementations change.

Interactive approval gate (implemented in orchestrator/agent.py):
  - Chokepoint: KimAgent._execute_tool(), after InteractionPolicy.before_tool().
  - Config key: hitl_risk_threshold (e.g. "high") — when set, tools at or
    above the threshold pause for UIBridge.confirm_action() before execution.
  - Env var: KIM_HITL_RISK_THRESHOLD
  - Requires a UIBridge to be attached; headless runs are unaffected.
  - Emits {"type":"hitl_approval_request"} and {"type":"hitl_approval_result"} JSON
    lines so the frontend can display an approval UI in the future.
"""

from __future__ import annotations

from typing import Optional

# ── Risk-level sets (tool names exactly as registered in tool_registry.py) ──

_HIGH_RISK: frozenset = frozenset({
    "delete_file",
    "run_command",
    "run_powershell",
    "run_python",
    "run_node",
    "git_commit",
    "git_checkout",
    "github_create_repo",
})

_MEDIUM_RISK: frozenset = frozenset({
    "write_file",
    "edit_file",
    "git_add",
    "web_fill",
    "web_fill_form",
    "web_press",
    "web_click",
    "type_text",
    "hotkey",
    "key_press",
    "click",
    "double_click",
    "right_click",
    "drag",
    "scroll",
    "open_url",
    "web_open",
    "web_close",
    "web_back",
    "focus_window",
    "resize_window",
    "write_memory",
    # Accessibility click: state-changing even though it uses element IDs rather
    # than raw pixel coordinates.  Kept at medium (not high) because the target
    # is resolved from the accessibility tree, making accidental activation less
    # likely than raw-coordinate clicks.
    "click_ui",
})

# All tools not in the above sets are LOW by default.

# ── Reason codes per tool (absent = category default) ──

_TOOL_REASON: dict[str, str] = {
    # ── High-risk ────────────────────────────────────────────────────────────
    "delete_file": "file_deletion",
    "run_command": "arbitrary_code_execution",
    "run_powershell": "arbitrary_code_execution",
    "run_python": "arbitrary_code_execution",
    "run_node": "arbitrary_code_execution",
    "git_commit": "git_history_change",
    "git_checkout": "git_branch_change",
    "github_create_repo": "remote_resource_creation",
    # ── Medium-risk ──────────────────────────────────────────────────────────
    "write_file": "file_write",
    "edit_file": "file_write",
    "git_add": "git_stage",
    "web_fill": "form_interaction",
    "web_press": "form_interaction",
    "web_click": "web_interaction",
    "click_ui": "ui_interaction",
    "type_text": "input_injection",
    "hotkey": "input_injection",
    "key_press": "input_injection",
    "click": "native_click",
    "double_click": "native_click",
    "right_click": "native_click",
    "drag": "native_click",
    "scroll": "native_click",
    "open_url": "web_navigation",
    "web_open": "web_navigation",
    "web_close": "web_navigation",
    "web_back": "web_navigation",
    "focus_window": "window_management",
    "resize_window": "window_management",
    "write_memory": "memory_write",
    # ── Low-risk (explicit so every registered tool has a classification) ────
    # File operations
    "read_file": "read_only",
    "list_dir": "read_only",
    # Screen / UI observation
    "take_screenshot": "read_only",
    "get_screen_info": "read_only",
    "observe_ui": "read_only",
    "take_annotated_screenshot": "read_only",
    # Web read / passive wait
    "web_observe": "read_only",
    "web_resolve": "read_only",
    "web_text": "read_only",
    "web_screenshot": "read_only",
    "web_wait_for": "read_only",
    "web_wait_for_url": "read_only",
    # Window queries
    "get_windows": "read_only",
    # Git queries
    "git_status": "read_only",
    "git_diff": "read_only",
    "git_log": "read_only",
    # Code analysis (non-executing)
    "lint_file": "read_only",
    # Search
    "search_in_files": "read_only",
    "find_files": "read_only",
    # Memory read
    "read_memory": "read_only",
}


def classify_tool_risk(name: str, args: Optional[dict] = None) -> dict:
    """Return a risk classification dict for the given tool call.

    Returns a dict with keys:
      "level"  — "high", "medium", or "low"
      "reason" — stable reason code (see module docstring)

    The args parameter enables arg-level refinement (e.g. lint_file --fix).
    Pass it anyway so callers don't need updating.
    """
    # Arg-level refinement: lint_file is read-only for a plain check, but
    # fix=True rewrites the file in place (ruff --fix). Treat that as a file
    # write so HITL gating applies — previously it was always classified
    # read_only and silently auto-approved despite mutating the file.
    if name == "lint_file" and args is not None and coerce_hitl_bool(args.get("fix")):
        return {"level": "high", "reason": "file_write"}
    if name in _HIGH_RISK:
        return {"level": "high", "reason": _TOOL_REASON.get(name, "irreversible")}
    if name in _MEDIUM_RISK:
        return {"level": "medium", "reason": _TOOL_REASON.get(name, "state_changing")}
    return {"level": "low", "reason": _TOOL_REASON.get(name, "read_only")}


def coerce_hitl_bool(value: object) -> bool:
    """Return True only for explicit truthy HITL config/env values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
