"""
Visual-task detection helpers extracted from orchestrator/agent.py.

_VISUAL_TASK_RE / _looks_visual detect 'what's on my screen?'-style tasks.
_SCREEN_READ_TOOLS is the frozenset of tools withheld on the first turn when a
proactive screenshot has already been attached to the first message (issue #4).
"""

import re

_VISUAL_TASK_RE = re.compile(
    r"\b(?:"
    r"on (?:my|the) (?:screen|desktop)|what'?s on (?:my|the)|what (?:do|can) you see|"
    r"describe (?:my|the|this) (?:screen|desktop|display)|see (?:my|the) (?:screen|desktop)|"
    r"look at (?:my|the) (?:screen|desktop)|what'?s open|which (?:apps?|windows?)|"
    r"my screen|the screen|screenshot|screen ?shot|what am i (?:looking at|seeing)"
    r")\b",
    re.IGNORECASE,
)


def _looks_visual(task: str) -> bool:
    """Heuristic: does this task ask about what's currently on the user's screen?"""
    return bool(_VISUAL_TASK_RE.search(task or ""))


# Screen-reading tools that become redundant once a screenshot is already attached
# to the first message. Browser web-chat models (Gemini/etc.) tend to reach for
# get_windows/take_screenshot anyway, which forces a second LLM round-trip back into
# the chat thread — the exact path that hangs (issue #4). Withholding them on the
# first turn forces the model to answer directly from the attached image.
_SCREEN_READ_TOOLS = frozenset({"take_screenshot", "get_windows"})
