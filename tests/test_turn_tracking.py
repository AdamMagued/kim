"""Unit tests for codex_engine/turn_tracking.py — the pure turn-boundary and
conversation-reset detectors backing _CodexProxy's two TUI fixes (relay-cap
reset at user-turn boundaries; delta-cursor reset on a fresh /new conversation)."""

import unittest

from codex_engine.turn_tracking import (
    contains_new_user_turn,
    detect_conversation_reset,
    is_continue_keepalive_text,
)


class IsContinueKeepaliveTextTests(unittest.TestCase):
    def test_continue_dot_is_keepalive(self):
        self.assertTrue(is_continue_keepalive_text("Continue."))

    def test_continue_dot_with_suffix_is_keepalive(self):
        self.assertTrue(is_continue_keepalive_text("Continue. (finish with a token)"))

    def test_continue_without_period_is_not_keepalive(self):
        self.assertFalse(is_continue_keepalive_text("Continue working on the game"))

    def test_unrelated_text_is_not_keepalive(self):
        self.assertFalse(is_continue_keepalive_text("please add a test"))


class ContainsNewUserTurnTests(unittest.TestCase):
    def test_empty_delta_has_no_new_turn(self):
        self.assertFalse(contains_new_user_turn([]))

    def test_genuine_user_message_is_new_turn(self):
        delta = [{"role": "user", "content": "add a new feature"}]
        self.assertTrue(contains_new_user_turn(delta))

    def test_continue_keepalive_is_not_new_turn(self):
        delta = [{"role": "user", "content": "Continue."}]
        self.assertFalse(contains_new_user_turn(delta))

    def test_tool_result_only_is_not_new_turn(self):
        delta = [{"type": "function_call_output", "output": "ok"}]
        self.assertFalse(contains_new_user_turn(delta))

    def test_tool_result_plus_continue_is_not_new_turn(self):
        delta = [
            {"type": "function_call_output", "output": "ok"},
            {"role": "user", "content": "Continue."},
        ]
        self.assertFalse(contains_new_user_turn(delta))

    def test_mixed_delta_with_real_user_text_is_new_turn(self):
        delta = [
            {"type": "function_call_output", "output": "ok"},
            {"role": "user", "content": "now also add tests"},
        ]
        self.assertTrue(contains_new_user_turn(delta))

    def test_list_content_blocks_are_read(self):
        delta = [{"role": "user", "content": [{"type": "input_text", "text": "do the next thing"}]}]
        self.assertTrue(contains_new_user_turn(delta))

    def test_blank_user_text_is_not_new_turn(self):
        delta = [{"role": "user", "content": "   "}]
        self.assertFalse(contains_new_user_turn(delta))

    def test_non_dict_items_are_ignored(self):
        self.assertFalse(contains_new_user_turn(["not a dict", 42, None]))


class DetectConversationResetTests(unittest.TestCase):
    def test_first_ever_call_is_not_a_reset(self):
        items = [{"role": "user", "content": "hi"}]
        is_reset, fp = detect_conversation_reset(items, last_sent_count=0, last_first_fingerprint=None)
        self.assertFalse(is_reset)
        self.assertIsNotNone(fp)

    def test_growing_list_with_same_first_item_is_not_a_reset(self):
        first = {"role": "user", "content": "task A"}
        items = [first, {"role": "assistant", "content": "ok"}]
        _, fp0 = detect_conversation_reset([first], last_sent_count=0, last_first_fingerprint=None)
        is_reset, fp1 = detect_conversation_reset(items, last_sent_count=1, last_first_fingerprint=fp0)
        self.assertFalse(is_reset)
        self.assertEqual(fp0, fp1)

    def test_shorter_list_is_a_reset(self):
        first = {"role": "user", "content": "task A"}
        _, fp0 = detect_conversation_reset([first, {"role": "assistant", "content": "ok"}], 0, None)
        # /new: a brand new, SHORTER item list.
        new_first = {"role": "user", "content": "task B"}
        is_reset, fp1 = detect_conversation_reset([new_first], last_sent_count=2, last_first_fingerprint=fp0)
        self.assertTrue(is_reset)
        self.assertNotEqual(fp0, fp1)

    def test_diverged_first_item_is_a_reset_even_if_longer(self):
        first = {"role": "user", "content": "task A"}
        _, fp0 = detect_conversation_reset([first], 0, None)
        # Same length or longer, but item[0] no longer matches — a discontinuous restart.
        diverged = [{"role": "user", "content": "totally different task"}, {"role": "assistant", "content": "x"}]
        is_reset, fp1 = detect_conversation_reset(diverged, last_sent_count=1, last_first_fingerprint=fp0)
        self.assertTrue(is_reset)
        self.assertNotEqual(fp0, fp1)

    def test_empty_items_returns_none_fingerprint(self):
        is_reset, fp = detect_conversation_reset([], last_sent_count=0, last_first_fingerprint=None)
        self.assertFalse(is_reset)
        self.assertIsNone(fp)


if __name__ == "__main__":
    unittest.main()
