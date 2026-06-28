"""
Conversation memory with a sliding window and automatic screenshot pruning.

Canonical message format stored internally:
    {"role": "user" | "assistant", "content": str | list[ContentItem]}

ContentItem for multimodal:
    {"type": "text", "text": "..."}
    {"type": "image", "data": "<base64>", "media_type": "image/png"}

Providers receive messages in this canonical format and transform them to their
native API format internally.
"""

from __future__ import annotations

import copy
import logging
from typing import Union

logger = logging.getLogger(__name__)

ContentItem = dict
Content = Union[str, list[ContentItem]]


class ConversationMemory:
    """
    Sliding-window conversation history.  Automatically strips screenshots
    from older messages to keep token counts manageable.

    Args:
        max_messages:       Hard cap on stored messages (oldest dropped first).
        keep_screenshots:   Number of most-recent user messages whose screenshots
                            are preserved.  Older screenshots are stripped and
                            replaced with a "(screenshot removed)" text note.
    """

    def __init__(self, max_messages: int = 40, keep_screenshots: int = 4):
        self._messages: list[dict] = []
        self.max_messages = max_messages
        self.keep_screenshots = keep_screenshots

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def add_user(self, content: Content, *, has_screenshot: bool = False) -> None:
        """Add a user turn.  Pass has_screenshot=True if content contains an image."""
        self._messages.append(
            {"role": "user", "content": content, "_has_screenshot": has_screenshot}
        )
        self._enforce_limits()

    def add_assistant(self, content: Content) -> None:
        """Add an assistant turn."""
        self._messages.append({"role": "assistant", "content": content})
        self._enforce_limits()

    def clear(self) -> None:
        self._messages.clear()

    def load_from_messages(self, messages: list[dict]) -> None:
        """
        Bulk-load messages from a saved session (for --resume).

        Each message should have at minimum ``{"role": ..., "content": ...}``.
        Internal metadata keys (``_has_screenshot``) are preserved if present.
        Records without a ``"role"`` key (e.g. ``{"type": "run_result", ...}``)
        are silently skipped so typed metadata records in session JSONL files
        do not pollute the conversation stack or cause KeyError in get_messages.
        """
        self._messages = [m for m in messages if "role" in m]
        self._enforce_limits()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    @property
    def compact_summary(self) -> str | None:
        """Return the compact summary text if one is pinned, else None."""
        if self._messages and self._messages[0].get("role") == "compact_summary":
            content = self._messages[0].get("content", "")
            return str(content) if content else None
        return None

    def get_messages(self) -> list[dict]:
        """
        Return messages in canonical format, with screenshots already pruned
        from older turns.  The returned list is a deep copy — safe to modify.

        The leading compact_summary sentinel (if present) is excluded from the
        returned list — callers that need it should read ``compact_summary``
        and merge it into the system prompt instead.
        """
        pruned = self._apply_screenshot_policy()
        result = []
        for m in pruned:
            role = m["role"]
            if role == "compact_summary":
                continue  # exposed via self.compact_summary instead
            result.append({"role": role, "content": copy.deepcopy(m["content"])})
        return result

    def __len__(self) -> int:
        return len(self._messages)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enforce_limits(self) -> None:
        """Trim to max_messages, keeping a coherent user/assistant sequence.

        A leading compact_summary sentinel is always pinned — it is never
        dropped regardless of the message limit.
        """
        has_summary = bool(self._messages and self._messages[0].get("role") == "compact_summary")
        if len(self._messages) <= self.max_messages:
            return

        # Pin the summary at index 0 and trim the rest
        if has_summary:
            summary = self._messages[0]
            rest = self._messages[1:]
            max_rest = self.max_messages - 1
            excess = len(rest) - max_rest
            for i in range(excess, len(rest)):
                if rest[i]["role"] == "user":
                    # If this user message is a tool result, include the
                    # preceding assistant tool_call to avoid orphaning it.
                    start = i
                    if self._is_tool_result(rest[i]) and i > 0:
                        start = i - 1
                    self._messages = [summary] + rest[start:]
                    return
            self._messages = [summary] + rest[-1:]
            return

        excess = len(self._messages) - self.max_messages
        # Find the first user message within the allowed window
        for i in range(excess, len(self._messages)):
            if self._messages[i]["role"] == "user":
                # If this user message is a tool result, include the
                # preceding assistant tool_call to avoid orphaning it.
                start = i
                if self._is_tool_result(self._messages[i]) and i > 0:
                    start = i - 1
                self._messages = self._messages[start:]
                return

        # If no user message is found in the trailing portion,
        # keep at least the very last message rather than emptying.
        self._messages = self._messages[-1:]

    def _is_tool_result(self, msg: dict) -> bool:
        """Return True if *msg* is a user-role tool-result message.

        Tool results are user messages whose text content begins with
        ``[Tool result:`` — the format used by the orchestrator's tool
        dispatcher when it injects results back into the conversation.
        """
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.startswith("[Tool result:")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    if item.get("text", "").startswith("[Tool result:"):
                        return True
        return False

    def _apply_screenshot_policy(self) -> list[dict]:
        """
        Return a list where screenshots in all but the last
        `keep_screenshots` user turns are replaced with a text note.

        Optimised: only deep-copies messages that will actually be mutated
        (the ones whose screenshots are being stripped).  Untouched messages
        are shallow-referenced, avoiding expensive duplication of base64 data.
        """
        # Shallow copy of the list — individual dicts are NOT copied unless needed
        messages = list(self._messages)

        # Find indices of user messages that have screenshots
        screenshot_indices = [
            i for i, m in enumerate(messages) if m.get("_has_screenshot")
        ]

        # Strip from all but the most recent `keep_screenshots`
        strip_indices = set(screenshot_indices[: max(0, len(screenshot_indices) - self.keep_screenshots)])

        for i in strip_indices:
            # Deep-copy only the message we're about to mutate
            messages[i] = copy.deepcopy(messages[i])
            messages[i]["content"] = _strip_images(messages[i]["content"])
            messages[i]["_has_screenshot"] = False

        return messages


def _strip_images(content: Content) -> Content:
    """Remove image items from a content list; keep text items.

    A list input always returns a list — never collapses to a plain string —
    so a message's content type stays stable across turns (providers branch
    on isinstance(content, list)).
    """
    if isinstance(content, str):
        return content
    kept = [item for item in content if item.get("type") != "image"]
    if not kept:
        return [{"type": "text", "text": "(screenshot removed — not in active window)"}]
    kept.append({"type": "text", "text": "(screenshot removed)"})
    return kept
