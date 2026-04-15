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

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_messages(self) -> list[dict]:
        """
        Return messages in canonical format, with screenshots already pruned
        from older turns.  The returned list is a deep copy — safe to modify.
        """
        pruned = self._apply_screenshot_policy()
        return [{"role": m["role"], "content": m["content"]} for m in pruned]

    def __len__(self) -> int:
        return len(self._messages)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enforce_limits(self) -> None:
        """Trim to max_messages, keeping a coherent user/assistant sequence."""
        if len(self._messages) <= self.max_messages:
            return
        excess = len(self._messages) - self.max_messages
        self._messages = self._messages[excess:]
        # Ensure first message is from "user" (API requirement for many providers)
        while self._messages and self._messages[0]["role"] != "user":
            self._messages.pop(0)

    def _apply_screenshot_policy(self) -> list[dict]:
        """
        Return a deep-copied list where screenshots in all but the last
        `keep_screenshots` user turns are replaced with a text note.
        """
        messages = copy.deepcopy(self._messages)

        # Find indices of user messages that have screenshots
        screenshot_indices = [
            i for i, m in enumerate(messages) if m.get("_has_screenshot")
        ]

        # Strip from all but the most recent `keep_screenshots`
        strip_indices = set(screenshot_indices[: max(0, len(screenshot_indices) - self.keep_screenshots)])

        for i in strip_indices:
            messages[i]["content"] = _strip_images(messages[i]["content"])
            messages[i]["_has_screenshot"] = False

        return messages


def _strip_images(content: Content) -> Content:
    """Remove image items from a content list; keep text items."""
    if isinstance(content, str):
        return content
    kept = [item for item in content if item.get("type") != "image"]
    if not kept:
        return "(screenshot removed — not in active window)"
    # If only one text item remains, unwrap to string
    if len(kept) == 1 and kept[0].get("type") == "text":
        return kept[0]["text"] + "\n(screenshot removed)"
    # Append a note
    kept.append({"type": "text", "text": "(screenshot removed)"})
    return kept
