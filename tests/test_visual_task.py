from orchestrator.visual_task import _uses_proactive_visual_context


class BrowserProvider:
    pass


class OllamaProvider:
    pass


class FakeProvider:
    pass


def test_browser_and_ollama_get_proactive_visual_context():
    task = "What's on my screen?"
    assert _uses_proactive_visual_context(BrowserProvider(), task)
    assert _uses_proactive_visual_context(OllamaProvider(), task)


def test_other_providers_and_nonvisual_tasks_do_not_get_context():
    assert not _uses_proactive_visual_context(FakeProvider(), "What's on my screen?")
    assert not _uses_proactive_visual_context(OllamaProvider(), "Write a unit test")
