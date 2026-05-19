import json
import unittest

from orchestrator.interaction_policy import InteractionPolicy


def _web_observe_text(*element_ids):
    payload = {
        "observation_generation": 7,
        "visible_interactive_elements": [{"element_id": element_id} for element_id in element_ids],
        "forms": [],
    }
    lines = ["WEB_OBSERVATION"]
    lines.extend(f"- {element_id}: <button role=button> 'Example'" for element_id in element_ids)
    lines.append("WEB_OBSERVATION_JSON:")
    lines.append(json.dumps(payload))
    return "\n".join(lines)


class InteractionPolicyTests(unittest.TestCase):
    def test_web_click_before_web_observe_is_blocked(self):
        policy = InteractionPolicy()
        decision = policy.before_tool("web_click", {"element_id": "w1"})
        self.assertFalse(decision.allowed)
        self.assertIn("web_observe", decision.message)

    def test_web_fill_before_web_observe_is_blocked(self):
        policy = InteractionPolicy()
        decision = policy.before_tool("web_fill", {"element_id": "w1", "text": "hello"})
        self.assertFalse(decision.allowed)
        self.assertIn("web_observe", decision.message)

    def test_coordinate_click_before_dom_observe_is_blocked(self):
        policy = InteractionPolicy()
        decision = policy.before_tool("click", {"x": 10, "y": 10})
        self.assertFalse(decision.allowed)
        self.assertIn("Raw coordinate", decision.message)

    def test_web_click_after_observe_with_known_id_is_allowed(self):
        policy = InteractionPolicy()
        policy.after_tool("web_observe", {}, _web_observe_text("w1"))
        decision = policy.before_tool("web_click", {"element_id": "w1"})
        self.assertTrue(decision.allowed)

    def test_web_click_after_state_change_requires_observe(self):
        policy = InteractionPolicy()
        policy.after_tool("web_observe", {}, _web_observe_text("w1"))
        policy.after_tool("web_fill", {"element_id": "w1", "text": "x"}, "Filled w1 with 1 chars")
        decision = policy.before_tool("web_click", {"element_id": "w1"})
        self.assertFalse(decision.allowed)
        self.assertIn("state", decision.message.lower())

    def test_web_press_enter_after_state_change_is_blocked(self):
        policy = InteractionPolicy()
        policy.after_tool("web_observe", {}, _web_observe_text("w1"))
        policy.after_tool("web_fill", {"element_id": "w1", "text": "x"}, "Filled w1 with 1 chars")
        decision = policy.before_tool("web_press", {"key": "Enter"})
        self.assertFalse(decision.allowed)
        self.assertIn("Enter", decision.message)

    def test_web_press_enter_with_submit_button_is_blocked(self):
        payload = {
            "observation_generation": 8,
            "visible_interactive_elements": [{"element_id": "w1"}],
            "forms": [{"buttons": [{"name": "Create repository", "label": "Create repository"}]}],
        }
        policy = InteractionPolicy()
        policy.after_tool("web_observe", {}, "WEB_OBSERVATION\nWEB_OBSERVATION_JSON:\n" + json.dumps(payload))
        decision = policy.before_tool("web_press", {"key": "Enter"})
        self.assertFalse(decision.allowed)
        self.assertIn("submit", decision.message.lower())


if __name__ == "__main__":
    unittest.main()
