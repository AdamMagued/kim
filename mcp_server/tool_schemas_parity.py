"""Schemas for the codex-parity tool set (issue #60).

Extracted from tool_registry.py to keep that file at its size baseline
(file-size gate Q6). Dispatch entries stay in tool_registry.py — this
module is schemas only, concatenated into the per-domain tool lists.
"""

from mcp.types import Tool

from mcp_server.tools.browser_parity import (
    handle_ask_user,
    handle_background_cancel,
    handle_background_poll,
    handle_background_start,
    handle_web_search,
)

PARITY_FILE_TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description=(
            "Read the text content of a file. Path can be absolute or relative to "
            "PROJECT_ROOT. By default returns the whole file verbatim. Pass offset "
            "(1-based line number) and/or limit (number of lines) to read a window "
            "of a large file instead: the result is then line-numbered, one line "
            "per row in the format '<n>→<line text>', with a footer noting the "
            "window and total line count when truncated (e.g. 'showing lines "
            "120-180 of 2368 total lines'). Line numbers from a windowed read let "
            "you target a follow-up edit_file call precisely."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "offset": {"type": "integer", "description": "Optional 1-based line number to start reading from (default 1). When offset or limit is given, output is line-numbered as '<n>→<line>'."},
                "limit": {"type": "integer", "description": "Optional maximum number of lines to return (default: all remaining lines)."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="edit_file",
        description=(
            "Make a surgical, str-replace edit to an existing file without rewriting "
            "the whole thing. Give the EXACT text to find (old_string) and what to "
            "replace it with (new_string). old_string must match the file's current "
            "content byte-for-byte (including whitespace/indentation) — copy it "
            "verbatim from a prior read_file, don't retype it from memory. "
            "old_string MUST be unique in the file by default: if it matches more "
            "than once, the call is rejected so you don't accidentally edit the wrong "
            "occurrence — either add a few more lines of surrounding context to "
            "old_string to make it unique, or pass replace_all=true to intentionally "
            "replace every occurrence (e.g. renaming a variable). If you already know "
            "how many occurrences exist, pass expected_occurrences as a self-check: "
            "the edit only applies if the actual count matches, otherwise it errors "
            "instead of silently editing the wrong number of places. NOTE: when a "
            "matched expected_occurrences is greater than 1, ALL matched occurrences "
            "are replaced (same effect as replace_all). This is much "
            "cheaper than write_file for small changes because you don't have to "
            "reproduce the entire file content. Not for creating new files (use "
            "write_file for that) — old_string must already exist in the file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "old_string": {"type": "string", "description": "Exact text to find in the file. Must match verbatim (whitespace included) and must not be empty."},
                "new_string": {"type": "string", "description": "Text to replace old_string with. Must differ from old_string."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence of old_string instead of requiring exactly one match. Default false.", "default": False},
                "expected_occurrences": {"type": "integer", "description": "Optional self-check: the exact number of occurrences of old_string you expect. If the actual count differs, the edit is rejected instead of applied. When set and matched, satisfies the uniqueness requirement — and if greater than 1, ALL matched occurrences are replaced (same effect as replace_all)."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    ),
    Tool(
        name="view_image",
        description=(
            "View an image file from disk. Returns the image itself (as a "
            "data:<mime>;base64 payload, same shape as take_screenshot) so you can "
            "look at its actual visual content — screenshots saved earlier, design "
            "mockups, photos, charts. Supported extensions: .png, .jpg, .jpeg, "
            ".gif, .webp, .bmp. Files over 10 MB or 25 megapixels are rejected. "
            "Use this instead of read_file for image files — read_file returns "
            "unreadable binary text for images."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Image file path (absolute or relative to PROJECT_ROOT)"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="revert_changes",
        description=(
            "Undo this run's file changes by restoring the pre-image checkpoints "
            "Kim recorded before each mutation. SCOPE — be aware of what this can "
            "and cannot restore: ONLY files that were modified/created through "
            "Kim's file tools during a checkpointed run are captured (modified "
            "files are restored from their pre-image; files the run created are "
            "deleted). It cannot revert shell-command side effects, changes past "
            "the per-run 50 MB checkpoint cap (recorded as skipped), directory "
            "deletions, or anything from a run that was not checkpointed. Before "
            "reverting, the current state of each file is saved to "
            "<path>.kim-revert.bak so the revert itself is undoable. Returns a "
            "summary of restored/deleted/skipped paths."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Run id whose changes to revert. Defaults to the current run (KIM_RUN_ID)."},
            },
        },
    ),
]

PARITY_SHELL_TOOLS: list[Tool] = [
    Tool(
        name="background_start",
        description=(
            "Start a shell command in the background and immediately return a job_id. "
            "This uses the same sandbox, deny-list, approval policy, and command handler "
            "as run_command. Use background_poll to read its state/output and "
            "background_cancel to stop it. Prefer run_command for short commands."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (defaults to PROJECT_ROOT)",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Maximum runtime in seconds. Hard cap is 600 seconds; "
                        "larger values are rejected."
                    ),
                    "default": 300,
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="background_poll",
        description=(
            "Check a background command. Returns JSON with status=running, completed, "
            "failed, or cancelled; completed jobs include the run_command result."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id from background_start"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="background_cancel",
        description=(
            "Cancel a running background command by job_id. The underlying run_command "
            "handler is cancelled so its normal process-cleanup path can run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id from background_start"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
]

PARITY_SCREEN_TOOLS: list[Tool] = [
    Tool(
        name="ask_user",
        description=(
            "Pause the current task and ask the user one necessary question. Use only "
            "when missing information blocks safe progress. The tool returns a NEED_HELP "
            "message; echo that result exactly so Kim displays the question and the user "
            "can answer in the next turn. Optional choices make the question easier to answer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Clear, specific question for the user",
                    "maxLength": 2000,
                },
                "choices": {
                    "type": "array",
                    "description": "Optional answer choices",
                    "items": {"type": "string"},
                    "maxItems": 10,
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    ),
]

PARITY_WEB_TOOLS: list[Tool] = [
    Tool(
        name="web_search",
        description=(
            "Search the live web using the current browser chat provider's built-in "
            "search capability. Use for current, niche, or externally verified facts. "
            "The tool returns a provider-native search request; on the next turn, carry "
            "out that search in the chat UI, cite source names and URLs, and continue the "
            "original task. Do not use web_open to scrape a search engine when this tool "
            "is available."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Focused search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Approximate number of sources to consult",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "recency_days": {
                    "type": "integer",
                    "description": "Optional recency window in days (0 means today)",
                    "minimum": 0,
                    "maximum": 3650,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
]

PARITY_SHELL_DISPATCH = {
    "background_start": handle_background_start,
    "background_poll": handle_background_poll,
    "background_cancel": handle_background_cancel,
}
PARITY_SCREEN_DISPATCH = {"ask_user": handle_ask_user}
PARITY_WEB_DISPATCH = {"web_search": handle_web_search}
