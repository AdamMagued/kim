"""K5: an image attachment in a user turn reaches the provider payload.

The composer's pasted image / region capture become an image ContentItem on the
user message; this verifies that item survives into get_messages() (exactly what
the provider .complete() receives).
"""

from orchestrator.memory import ConversationMemory

IMG = {"type": "image", "data": "iVBORw0KGgo=", "media_type": "image/png"}


def test_image_attachment_present_in_payload():
    mem = ConversationMemory(max_messages=40, keep_screenshots=4)
    mem.add_user([{"type": "text", "text": "what is this?"}, IMG], has_screenshot=True)
    payload = mem.get_messages()
    last = payload[-1]
    assert isinstance(last["content"], list)
    kinds = [b.get("type") for b in last["content"]]
    assert "image" in kinds
    img = next(b for b in last["content"] if b.get("type") == "image")
    assert img["data"] == IMG["data"]
    assert img["media_type"] == "image/png"


def test_recent_screenshot_not_stripped():
    mem = ConversationMemory(max_messages=40, keep_screenshots=2)
    mem.add_user([{"type": "text", "text": "look"}, IMG], has_screenshot=True)
    mem.add_assistant("ok")
    payload = mem.get_messages()
    user = payload[0]
    assert any(b.get("type") == "image" for b in user["content"])
