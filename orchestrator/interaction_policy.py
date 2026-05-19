"""Lightweight interaction rails for Kim's tool loop."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class PolicyDecision:
    allowed: bool = True
    message: str = ""
    hard_block: bool = False
    suggested_action: str = ""


class InteractionPolicy:
    """Track recent UI observations and catch obviously weak action sequences."""

    _WEB_STATE_CHANGERS = {"web_click", "web_fill", "web_press"}
    _NATIVE_STATE_CHANGERS = {"click_ui", "type_text", "hotkey", "key_press"}
    _COORDINATE_TOOLS = {"click", "double_click", "right_click", "drag"}

    def __init__(self) -> None:
        self.web_generation = 0
        self.ui_generation = 0
        self.known_web_element_ids: set[str] = set()
        self.known_ui_element_ids: set[str] = set()
        self.web_state_dirty = False
        self.ui_state_dirty = False
        self.web_observe_attempted = False
        self.web_observe_failed = False
        self.ui_observe_attempted = False
        self.web_submit_may_exist = False

    def before_tool(self, name: str, args: dict[str, Any] | None = None) -> PolicyDecision:
        args = args or {}
        if name == "web_open":
            return PolicyDecision()
        if name == "web_observe":
            return PolicyDecision()
        if name == "observe_ui":
            return PolicyDecision()

        if name == "web_resolve":
            if not self.web_generation:
                return self._block(
                    "POLICY_WARNING",
                    "web_resolve needs a current web_observe snapshot.",
                    "Call web_observe first, then web_resolve with the semantic target.",
                )
            if self.web_state_dirty:
                return self._block(
                    "POLICY_WARNING",
                    "The browser state changed after the last observation.",
                    "Call web_observe again before resolving another element.",
                )

        if name in {"web_click", "web_fill"}:
            element_id = str(args.get("element_id", "")).strip()
            if not self.web_generation:
                return self._block(
                    "POLICY_WARNING",
                    f"{name} requires a prior web_observe snapshot.",
                    "Call web_observe, then web_resolve if the target is semantic.",
                )
            if self.web_state_dirty:
                return self._block(
                    "POLICY_WARNING",
                    "The browser state changed after the last observation, so element IDs are stale.",
                    "Call web_observe again before the next web_click/web_fill.",
                )
            if not element_id or element_id not in self.known_web_element_ids:
                return self._block(
                    "POLICY_BLOCK",
                    f"{name} was called with unknown or stale element_id {element_id!r}.",
                    "Call web_observe or web_resolve to get a valid element_id.",
                    hard=True,
                )

        if name == "web_press":
            key = str(args.get("key", "")).strip().lower()
            if key in {"enter", "return", "space", " "}:
                if not self.web_generation:
                    return self._block(
                        "POLICY_WARNING",
                        "web_press needs a current web_observe snapshot before using Enter or Space.",
                        "Call web_observe first, then use web_resolve/web_click for form submission.",
                    )
                if self.web_state_dirty:
                    return self._block(
                        "POLICY_WARNING",
                        "The browser state changed after the last observation; Enter/Space could submit the wrong state.",
                        "Call web_observe again before pressing Enter or Space.",
                    )
                if key in {"enter", "return"} and self.web_submit_may_exist:
                    return self._block(
                        "POLICY_WARNING",
                        "A form submit/create button is present, so pressing Enter to submit is not allowed.",
                        "Resolve the submit button with web_resolve, then click it by element_id.",
                    )

        if name == "click_ui":
            element_id = str(args.get("element_id", "")).strip()
            if not self.ui_generation:
                return self._block(
                    "POLICY_WARNING",
                    "click_ui requires a prior observe_ui snapshot.",
                    "Call observe_ui before click_ui.",
                )
            if self.ui_state_dirty:
                return self._block(
                    "POLICY_WARNING",
                    "The native UI may have changed since the last observation.",
                    "Call observe_ui again before click_ui.",
                )
            if not element_id or element_id not in self.known_ui_element_ids:
                return self._block(
                    "POLICY_BLOCK",
                    f"click_ui was called with unknown or stale element_id {element_id!r}.",
                    "Call observe_ui to get a valid element_id.",
                    hard=True,
                )

        if name in self._COORDINATE_TOOLS and not (self.web_observe_attempted or self.ui_observe_attempted):
            return self._block(
                "POLICY_WARNING",
                "Raw coordinate mouse actions are a last resort and no DOM/accessibility observation has been tried.",
                "Call web_observe for browser pages or observe_ui for native apps first.",
            )

        if name in {"web_screenshot", "take_screenshot", "take_annotated_screenshot"}:
            if not (self.web_observe_attempted or self.ui_observe_attempted or self.web_observe_failed):
                return PolicyDecision(
                    allowed=True,
                    message=self._message(
                        "POLICY_WARNING",
                        "Screenshot-first interaction should be a fallback after DOM/accessibility observation.",
                        "Call web_observe or observe_ui first unless this is genuinely visual inspection.",
                    ),
                    suggested_action="Call web_observe or observe_ui first unless this is visual.",
                )

        return PolicyDecision()

    def after_tool(self, name: str, args: dict[str, Any] | None, result_text: str) -> None:
        failed = str(result_text or "").startswith(("ERROR", "PERMISSION_ERROR"))
        if name == "web_open":
            self.known_web_element_ids.clear()
            self.web_generation = 0
            self.web_state_dirty = False
            self.web_observe_attempted = False
            self.web_observe_failed = False
            self.web_submit_may_exist = False
            return

        if name == "web_observe":
            self.web_observe_attempted = True
            self.web_observe_failed = result_text.startswith("ERROR:")
            self.web_state_dirty = False
            if self.web_observe_failed:
                self.known_web_element_ids.clear()
                self.web_generation = 0
                return
            ids, generation, submit_may_exist = self._parse_web_observe(result_text)
            self.known_web_element_ids = ids
            self.web_generation = generation or (self.web_generation + 1)
            self.web_submit_may_exist = submit_may_exist
            return

        if name == "observe_ui":
            self.ui_observe_attempted = True
            self.ui_state_dirty = False
            ids = set(re.findall(r"\b(e\d+)\s*:", result_text))
            self.known_ui_element_ids = ids
            self.ui_generation += 1
            return

        if name in self._WEB_STATE_CHANGERS:
            if failed:
                return
            self.web_state_dirty = True
            return

        if name in self._NATIVE_STATE_CHANGERS:
            if failed:
                return
            self.ui_state_dirty = True

    def _block(
        self,
        severity: str,
        reason: str,
        suggested_action: str,
        *,
        hard: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            message=self._message(severity, reason, suggested_action),
            hard_block=hard,
            suggested_action=suggested_action,
        )

    def _message(self, severity: str, reason: str, suggested_action: str) -> str:
        return (
            f"{severity}: {reason}\n"
            f"Last web_observe generation: {self.web_generation}; "
            f"known web IDs: {len(self.known_web_element_ids)}; "
            f"last observe dirty: {self.web_state_dirty}.\n"
            f"Last observe_ui generation: {self.ui_generation}; "
            f"known UI IDs: {len(self.known_ui_element_ids)}; "
            f"last UI observe dirty: {self.ui_state_dirty}.\n"
            f"Suggested next action: {suggested_action}"
        )

    @staticmethod
    def _parse_web_observe(result_text: str) -> tuple[set[str], int, bool]:
        marker = "WEB_OBSERVATION_JSON:"
        if marker in result_text:
            payload = result_text.split(marker, 1)[1].strip()
            try:
                data = json.loads(payload)
                ids = {
                    str(el.get("element_id"))
                    for el in data.get("visible_interactive_elements", [])
                    if el.get("element_id")
                }
                submit_may_exist = False
                for form in data.get("forms", []):
                    for button in form.get("buttons", []):
                        label = " ".join(
                            str(button.get(k, ""))
                            for k in ("name", "label", "role", "type")
                        ).lower()
                        if any(word in label for word in ("submit", "create", "save", "send", "publish")):
                            submit_may_exist = True
                            break
                    if submit_may_exist:
                        break
                return ids, int(data.get("observation_generation") or 0), submit_may_exist
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                pass
        submit_may_exist = bool(re.search(r"\b(submit|create|save|send|publish)\b", result_text, re.IGNORECASE))
        return set(re.findall(r"^- (w\d+):", result_text, flags=re.MULTILINE)), 0, submit_may_exist
