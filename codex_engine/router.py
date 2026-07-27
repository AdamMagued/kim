"""
Hybrid Router for Kim Engine.

Classifies incoming user tasks into one of two execution modes:
1. PATCH: Pure codebase editing, refactoring, test execution, bug fixing.
   -> Zips workspace, executes task in sandbox VM/container, applies unified `git diff` patch.
2. LOCAL: System OS actions, Mac file search, brew install, desktop apps, screencapture, live Chrome.
   -> Routes directly to live Mac terminal OS bridge.
"""

import logging
import re

logger = logging.getLogger("kim.hybrid_router")

# Keywords that indicate local OS, host filesystem, or desktop system actions
LOCAL_OS_KEYWORDS = [
    "brew", "apt", "install", "screencapture", "screenshot", "desktop",
    "chrome", "browser tab", "mac", "system", "os", "downloads", "desktop",
    "open app", "killall", "launch", "port ", "lsof", "pkill"
]

# Keywords that indicate code editing / refactoring / codebase patching
PATCH_KEYWORDS = [
    "refactor", "fix bug", "add function", "write test", "vitest", "pytest",
    "patch", "edit file", "update component", "modify", "implement", "feature"
]


def classify_task_mode(prompt: str) -> str:
    """Classify user prompt into 'PATCH' or 'LOCAL'.

    Fast deterministic + keyword analysis:
    - If prompt requests Mac system actions, external paths, or browser GUI -> 'LOCAL'
    - If prompt requests code edits, test suites, or features -> 'PATCH'
    - Default to 'PATCH' for coding requests to leverage clean git apply.
    """
    if not prompt or not isinstance(prompt, str):
        return "PATCH"

    low_prompt = prompt.lower().strip()

    # Check for strong Local OS indicators
    for kw in LOCAL_OS_KEYWORDS:
        if kw in low_prompt:
            logger.info("Hybrid Router: Classified '%s' -> LOCAL (matched '%s')", prompt[:50], kw)
            return "LOCAL"

    # Check for strong Patch indicators
    for kw in PATCH_KEYWORDS:
        if kw in low_prompt:
            logger.info("Hybrid Router: Classified '%s' -> PATCH (matched '%s')", prompt[:50], kw)
            return "PATCH"

    # Default to PATCH for clean workspace safety
    logger.info("Hybrid Router: Classified '%s' -> PATCH (default)", prompt[:50])
    return "PATCH"
