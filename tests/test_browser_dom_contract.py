"""DOM-fixture contract tests for BrowserProvider (K3 + T3).

Drives ``BrowserProvider.complete()`` END TO END through an injected
FakePageDriver (the K3 seam) against recorded/synthesized response DOM in
``tests/fixtures/dom/``:

  * chatgpt_tool_call.html    — div.markdown + <pre><code class="language-json">
                                (hljs per-token spans): fenced tool-call JSON,
                                completion-hash exit path
  * chatgpt_code_fences.html  — two fenced blocks (language- class AND the
                                header-div language fallback), NO sentinel:
                                stop-button appeared→cleared exit path
  * gemini_text_response.html — model-response Angular nesting, prose answer
  * gemini_tool_call.html     — model-response nesting where the serializer
                                flattens to innerText and the tool call must
                                survive as BARE JSON (known_tools guard)

The fixtures' ``__COMPLETION_HASH__`` placeholder is materialized with the
live per-turn sentinel the provider embeds in the injected prompt, so the
tests genuinely exercise: prompt injection + editor-readback verification,
the two-phase wait (_wait_for_new_response + _wait_for_generation_complete),
the markdown serializer's fence-reconstruction contract (mirrored over a
parsed DOM), and response_parser.parse_response.

No unconditional module stubs here — the provider chain imports cleanly
without playwright (lazy import) and without touching mcp.
"""

from __future__ import annotations

import os
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

from orchestrator.providers.browser import provider as bp
from orchestrator.providers.browser.provider import BrowserProvider
from orchestrator.providers.browser.site_configs import MOD_KEY, SITE_CONFIGS

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dom"

_HASH_RE = re.compile(r"\[END_OF_RESPONSE_[0-9a-f]{8}\]")

TOOLS = [
    {
        "name": "run_command",
        "description": "Run a shell command",
        "parameters": {"properties": {"command": {"type": "string"}}},
    },
    {
        "name": "click_ui",
        "description": "Click a UI element",
        "parameters": {"properties": {"element_id": {}, "double": {}}},
    },
]

MESSAGES = [{"role": "user", "content": "Open the pong game and start it"}]


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Minimal DOM: parse fixture HTML into a tree the fakes can serve
# ---------------------------------------------------------------------------

_VOID_TAGS = {"br", "img", "input", "hr", "meta", "link"}
# Elements whose boundaries produce rendered line breaks (innerText semantics).
_BLOCK_TAGS = {
    "p", "div", "pre", "li", "ul", "ol", "table", "tr", "blockquote",
    "section", "article", "button", "h1", "h2", "h3", "h4", "h5", "h6",
    "model-response", "message-content",
}


class _Node:
    def __init__(self, tag: str, attrs):
        self.tag = tag
        self.attrs = {k: (v or "") for k, v in attrs}
        self.children: list = []  # _Node | str

    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()


class _DomBuilder(HTMLParser):
    """Builds a _Node tree. Whitespace-only text containing a newline is
    dropped OUTSIDE <pre> (matching innerText's collapsing of inter-tag
    markup indentation) and preserved inside <pre> (rendered verbatim)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", [])
        self._stack = [self.root]
        self._pre_depth = 0

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)
            if tag == "pre":
                self._pre_depth += 1

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                if tag == "pre":
                    self._pre_depth -= 1
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._pre_depth == 0 and "\n" in data and not data.strip():
            return
        self._stack[-1].children.append(data)


def parse_html(html: str) -> _Node:
    """Parse and return the first top-level element (the response root)."""
    builder = _DomBuilder()
    builder.feed(html)
    for child in builder.root.children:
        if isinstance(child, _Node):
            return child
    raise ValueError("fixture has no root element")


def inner_text(node: _Node) -> str:
    out: list[str] = []

    def _newline():
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    def walk(n: _Node):
        for c in n.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "br":
                out.append("\n")
            else:
                if c.tag in _BLOCK_TAGS:
                    _newline()
                walk(c)
                if c.tag in _BLOCK_TAGS:
                    _newline()

    walk(node)
    return "".join(out)


def _matches(node: _Node, sel: str) -> bool:
    sel = sel.strip()
    if not sel or " " in sel or "[" in sel or "," in sel or sel.startswith("xpath="):
        return False
    tag, _, cls = sel.partition(".")
    if tag and node.tag != tag:
        return False
    if cls and cls not in node.classes():
        return False
    return True


def query(root: _Node, sel: str) -> list[_Node]:
    found = []

    def walk(n: _Node):
        if _matches(n, sel):
            found.append(n)
        for c in n.children:
            if isinstance(c, _Node):
                walk(c)

    walk(root)
    return found


def find_first(root: _Node, tag: str) -> Optional[_Node]:
    for c in root.children:
        if isinstance(c, _Node):
            if c.tag == tag:
                return c
            deeper = find_first(c, tag)
            if deeper is not None:
                return deeper
    return None


# ---------------------------------------------------------------------------
# Python mirror of provider._MARKDOWN_SERIALIZER_JS (same algorithm over the
# parsed DOM: descend through single-child wrapper chains (FIX 7 — Gemini's
# Angular nesting) to the level where fenced code blocks are actual direct
# siblings of paragraph text; PRE -> ```lang fence from the code's
# language- class or the header div's first line; else innerText).
# ---------------------------------------------------------------------------

_LANG_CLASS_RE = re.compile(r"language-([\w+#.-]+)")
_HDR_LANG_RE = re.compile(r"^[a-z0-9+#.-]{1,15}$")


def serialize_markdown(el: _Node) -> str:
    root = el
    for _guard in range(20):
        root_kids = [c for c in root.children if isinstance(c, _Node)]
        if len(root_kids) != 1:
            break
        only = root_kids[0]
        if only.tag == "pre":
            break
        only_kids = [c for c in only.children if isinstance(c, _Node)]
        if not only_kids:
            break
        root = only
    kids = [c for c in root.children if isinstance(c, _Node)]
    if not kids:
        return inner_text(el)
    parts = []
    for child in kids:
        if child.tag == "pre":
            code = find_first(child, "code")
            lang = ""
            if code is not None:
                m = _LANG_CLASS_RE.search(code.attrs.get("class", ""))
                if m:
                    lang = m.group(1)
            if not lang:
                hdr = find_first(child, "div")
                if hdr is not None:
                    first = inner_text(hdr).strip().split("\n")[0].strip().lower()
                    if _HDR_LANG_RE.match(first):
                        lang = first
            code_text = inner_text(code) if code is not None else inner_text(child)
            parts.append("```" + lang + "\n" + re.sub(r"\n+$", "", code_text) + "\n```")
        else:
            parts.append(inner_text(child))
    joined = "\n\n".join(parts)
    return joined if joined.strip() else inner_text(el)


# ---------------------------------------------------------------------------
# FakePageDriver — page-shaped state machine backed by the fixture DOM
# ---------------------------------------------------------------------------

class _DomElement:
    def __init__(self, node: _Node):
        self._node = node

    async def is_visible(self):
        return True

    async def inner_text(self):
        return inner_text(self._node)

    async def text_content(self):
        return inner_text(self._node)

    async def evaluate(self, script):
        if "tagName === 'PRE'" in script:
            return serialize_markdown(self._node)
        raise RuntimeError(f"unsupported element script: {script[:60]!r}")


class _DomLocator:
    def __init__(self, nodes: list[_Node]):
        self._nodes = nodes

    async def count(self):
        return len(self._nodes)

    def nth(self, i):
        return _DomElement(self._nodes[i])

    @property
    def first(self):
        return _DomElement(self._nodes[0])

    async def all(self):
        return [_DomElement(n) for n in self._nodes]


class _InputLocator:
    async def count(self):
        return 1

    def nth(self, _i):
        return self

    async def is_visible(self):
        return True


class _SendLocator:
    def __init__(self, page: "FakeChatPage"):
        self._page = page

    async def count(self):
        return 1

    def nth(self, _i):
        return self

    @property
    def first(self):
        return self

    async def is_visible(self):
        return True

    async def click(self):
        self._page.submit()


class _StopLocator:
    def __init__(self, page: "FakeChatPage"):
        self._page = page

    async def count(self):
        return 1

    def nth(self, _i):
        return self

    async def is_visible(self):
        return self._page.stop_visible()


class _FakeKeyboard:
    def __init__(self, page: "FakeChatPage"):
        self._page = page
        self._select_all = False

    async def press(self, key: str):
        if key == f"{MOD_KEY}+a":
            self._select_all = True
        elif key == "Backspace":
            if self._select_all:
                self._page.editor = ""
            self._select_all = False
        elif key == f"{MOD_KEY}+v":
            self._page.set_editor(self._page.clipboard)


class FakeChatPage:
    """Page-shaped object satisfying the PageLike surface, backed by a
    fixture-DOM response that materializes when the send button is clicked
    (with __COMPLETION_HASH__ replaced by the sentinel found in the injected
    prompt — mirroring a model that echoes the transport marker)."""

    def __init__(self, site: str, fixture_html: str, *,
                 stop_visibility: Optional[list[bool]] = None,
                 corrupt_paste: bool = False):
        self.site = site
        self.cfg = SITE_CONFIGS[site]
        self.url = f"https://{self.cfg['url_pattern']}/c/fixture-thread"
        self.keyboard = _FakeKeyboard(self)
        self._fixture_html = fixture_html
        self.docs: list[_Node] = []
        self.editor = ""
        self.clipboard = ""
        self._corrupt_paste = corrupt_paste
        self._stop_schedule = list(stop_visibility or [])
        self._stop_i = 0
        self._generating = False
        self.submitted_prompts: list[str] = []
        self.completion_hash = ""

    # -- state helpers ----------------------------------------------------
    def set_editor(self, text: str):
        self.editor = text[: len(text) // 2] if self._corrupt_paste else text

    def submit(self):
        prompt = self.editor
        self.submitted_prompts.append(prompt)
        m = _HASH_RE.search(prompt)
        self.completion_hash = m.group(0) if m else ""
        html = self._fixture_html.replace("__COMPLETION_HASH__", self.completion_hash)
        self.docs.append(parse_html(html))
        self.editor = ""
        self._generating = True
        self._stop_i = 0

    def stop_visible(self) -> bool:
        if not self._generating or not self._stop_schedule:
            return False
        i = self._stop_i
        self._stop_i += 1
        return self._stop_schedule[min(i, len(self._stop_schedule) - 1)]

    # -- PageLike surface ---------------------------------------------------
    def locator(self, sel: str):
        if sel in self.cfg["input_selectors"]:
            return _InputLocator()
        if sel in self.cfg["send_selectors"]:
            return _SendLocator(self)
        if sel in self.cfg["stop_selectors"]:
            return _StopLocator(self)
        return _DomLocator([n for doc in self.docs for n in query(doc, sel)])

    async def evaluate(self, script: str, arg=None):
        if "navigator.clipboard.writeText" in script:
            self.clipboard = arg if isinstance(arg, str) else ""
            return None
        if "new ClipboardEvent" in script:
            self.set_editor(str((arg or ["", ""])[1]))
            return None
        if "nativeInputValueSetter" in script:
            self.set_editor(str((arg or ["", ""])[1]))
            return True
        if "el.value ?? el.innerText" in script:
            return self.editor
        if "document.hasFocus" in script:
            return True
        return None

    async def click(self, _sel: str, **_kw):
        return None

    async def wait_for_timeout(self, _ms):
        return None

    async def goto(self, url: str, **_kw):
        self.url = url


class FakePageDriver:
    """The K3 injectable driver: acquire() yields the ready chat page."""

    def __init__(self, page: Optional[FakeChatPage]):
        self.page = page
        self.acquire_calls = 0

    async def acquire(self):
        self.acquire_calls += 1
        if self.page is None:
            return None, None
        return self.page, self.page.site


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_ENV_KEYS = (
    "KIM_WEBVIEW_BRIDGE_URL", "KIM_WEBVIEW_BRIDGE_TOKEN",
    "KIM_PREFERRED_SITE", "KIM_BROWSER_RESTORE_STATUS",
)


class _DomContractCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = patch.dict(os.environ)
        self._env.start()
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        # FIX 6: isolate from any REAL ~/.../kim/account.json on the machine
        # running the tests (self._gemini_authuser is loaded from it).
        os.environ["KIM_ACCOUNT_PATH"] = "/nonexistent/kim-account-test-isolation.json"

    def tearDown(self):
        self._env.stop()

    @staticmethod
    def _provider(**kwargs) -> BrowserProvider:
        return BrowserProvider({"project_root": ".", "browser_provider": {}}, **kwargs)

    async def _complete(self, provider: BrowserProvider, **kwargs) -> dict:
        with patch.object(bp.asyncio, "sleep", AsyncMock()), \
             patch.object(bp, "RESPONSE_WAIT_S", 5.0), \
             patch.object(bp, "GENERATION_WAIT_S", 5.0):
            return await provider.complete(
                messages=kwargs.pop("messages", MESSAGES),
                tools=kwargs.pop("tools", TOOLS),
                system=kwargs.pop("system", "You are Kim."),
                **kwargs,
            )


# ---------------------------------------------------------------------------
# End-to-end contract tests
# ---------------------------------------------------------------------------

class TestChatGPTToolCallFixture(_DomContractCase):
    async def test_tool_call_parsed_via_completion_hash_path(self):
        page = FakeChatPage("chatgpt", load_fixture("chatgpt_tool_call.html"))
        driver = FakePageDriver(page)
        result = await self._complete(self._provider(), page_driver=driver)

        self.assertEqual(driver.acquire_calls, 1)
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "run_command")
        self.assertEqual(result["args"], {"command": "open pong.html"})
        # Prose around the JSON block survives as content (PLAN/STEP markers).
        self.assertIn("STEP 1", result.get("content", ""))
        self.assertIn("usage", result)

    async def test_injected_prompt_carries_system_block_and_sentinel(self):
        page = FakeChatPage("chatgpt", load_fixture("chatgpt_tool_call.html"))
        await self._complete(self._provider(), page_driver=FakePageDriver(page))

        self.assertEqual(len(page.submitted_prompts), 1)
        prompt = page.submitted_prompts[0]
        self.assertIn("[SYSTEM]", prompt)
        self.assertIn("run_command", prompt)          # tool schema listed
        self.assertIn("Open the pong game", prompt)   # user message
        self.assertRegex(prompt, _HASH_RE)            # transport sentinel
        self.assertTrue(_HASH_RE.fullmatch(page.completion_hash))

    async def test_second_turn_skips_system_prompt_and_scrapes_new_element(self):
        page = FakeChatPage("chatgpt", load_fixture("chatgpt_tool_call.html"))
        driver = FakePageDriver(page)
        provider = self._provider()
        await self._complete(provider, page_driver=driver)
        followup = MESSAGES + [
            {"role": "assistant", "content": "ran it"},
            {"role": "user", "content": "[Tool result: ok] continue"},
        ]
        result = await self._complete(provider, messages=followup, page_driver=driver)

        self.assertEqual(len(page.submitted_prompts), 2)
        self.assertNotIn("[SYSTEM]", page.submitted_prompts[1])
        self.assertEqual(len(page.docs), 2)  # scraped the NEW response element
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "run_command")


class TestChatGPTFenceReconstruction(_DomContractCase):
    async def test_fences_rebuilt_via_stop_button_transition(self):
        # NO sentinel in this fixture: completion must come from the
        # stop-control appeared→cleared transition.
        page = FakeChatPage(
            "chatgpt",
            load_fixture("chatgpt_code_fences.html"),
            stop_visibility=[True, True, False, False, False, False],
        )
        result = await self._complete(self._provider(), page_driver=FakePageDriver(page))

        self.assertEqual(result["type"], "text")
        content = result["content"]
        # Fence 1: language from the <code class="language-python"> class,
        # per-line hljs spans reassembled with their newlines intact.
        self.assertIn(
            "```python\ndef move_paddle(y):\n"
            "    paddle.y = clamp(y, 0, HEIGHT - PADDLE_H)\n"
            "    return paddle.y\n```",
            content,
        )
        # Fence 2: no language- class — recovered from the header div.
        self.assertIn(
            "```bash\npython -m http.server 8000 --directory .\n"
            "open http://localhost:8000/pong.html\n```",
            content,
        )
        # Prose between the blocks keeps its position.
        self.assertIn("Then restart the server:", content)
        self.assertIn("The paddle will now stay inside the canvas.", content)

    def test_serializer_mirror_matches_fixture_dom(self):
        # Guard the mirror itself: the fixture's DOM serializes to fenced
        # markdown exactly the way _MARKDOWN_SERIALIZER_JS documents.
        root = parse_html(load_fixture("chatgpt_tool_call.html"))
        md = serialize_markdown(root)
        self.assertIn("```json\n{\n", md)
        self.assertIn('"tool": "run_command",', md)
        self.assertTrue(md.startswith("STEP 1:"))


class TestGeminiFixtures(_DomContractCase):
    async def test_text_response_through_model_response_nesting(self):
        page = FakeChatPage("gemini", load_fixture("gemini_text_response.html"))
        result = await self._complete(self._provider(), page_driver=FakePageDriver(page))

        self.assertEqual(result["type"], "text")
        self.assertTrue(result["content"].startswith("TASK_COMPLETE:"))
        self.assertIn("paddle movement was verified", result["content"])
        # Sentinel stripped by strip_transport_markers.
        self.assertNotRegex(result["content"], _HASH_RE)

    async def test_tool_call_survives_as_bare_json_when_fences_flatten(self):
        # Regardless of whether the serializer reconstructs a ```json fence
        # around this PRE block (FIX 7 now descends through model-response's
        # single-child wrapper chain, so it does) or flattens to innerText,
        # the non-greedy fenced-JSON regex can't span the tool call's nested
        # {"args": {...}} braces and falls through to the balanced-brace
        # bare-JSON scan — so the tool call must survive via THAT path
        # either way, gated by known_tools.
        page = FakeChatPage("gemini", load_fixture("gemini_tool_call.html"))
        result = await self._complete(self._provider(), page_driver=FakePageDriver(page))

        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "click_ui")
        self.assertEqual(result["args"], {"element_id": 4, "double": False})
        self.assertIn("STEP 2", result.get("content", ""))

    async def test_prose_reply_keeps_code_fence(self):
        # FIX 7: gemini_prose_with_code.html is an ORDINARY prose answer (not
        # a tool call) with a code block nested the same number of Angular
        # wrapper layers deep as gemini_tool_call.html / gemini_text_response
        # html. Before the markdown-serializer descent fix, this flattened to
        # plain innerText and the ```python fence around the <pre> block was
        # silently dropped — exactly the audit finding (fixture comment in
        # gemini_tool_call.html) this fix addresses.
        page = FakeChatPage("gemini", load_fixture("gemini_prose_with_code.html"))
        result = await self._complete(self._provider(), page_driver=FakePageDriver(page))

        self.assertEqual(result["type"], "text")
        self.assertIn("```python", result["content"])
        self.assertIn("def clamp(y):", result["content"])
        self.assertIn("return max(0, min(y, HEIGHT - PADDLE_H))", result["content"])
        self.assertIn("```", result["content"])
        self.assertIn("Call it every frame before drawing the paddle.", result["content"])

    def test_serializer_mirror_descends_wrapper_chain_for_prose_with_code(self):
        # Guard the mirror itself against the fixture's nested DOM.
        root = parse_html(load_fixture("gemini_prose_with_code.html"))
        md = serialize_markdown(root)
        self.assertIn("```python\ndef clamp(y):", md)
        self.assertIn("return max(0, min(y, HEIGHT - PADDLE_H))\n```", md)

    def test_serializer_mirror_unaffected_for_flat_chatgpt_root(self):
        # Regression guard for FIX 7: ChatGPT's response root already has
        # multiple direct children (p, pre, p) — the new descent loop must
        # not touch it (it only descends single-child wrapper chains).
        root = parse_html(load_fixture("chatgpt_tool_call.html"))
        md = serialize_markdown(root)
        self.assertIn("```json\n{\n", md)
        self.assertTrue(md.startswith("STEP 1:"))


class TestDriverSeam(_DomContractCase):
    async def test_constructor_injected_driver_is_used(self):
        page = FakeChatPage("gemini", load_fixture("gemini_text_response.html"))
        driver = FakePageDriver(page)
        provider = self._provider(page_driver=driver)
        result = await self._complete(provider)  # no call-site driver
        self.assertEqual(driver.acquire_calls, 1)
        self.assertEqual(result["type"], "text")
        self.assertTrue(result["content"].startswith("TASK_COMPLETE:"))

    async def test_call_site_driver_overrides_constructor(self):
        ctor_driver = FakePageDriver(
            FakeChatPage("gemini", load_fixture("gemini_text_response.html"))
        )
        call_driver = FakePageDriver(
            FakeChatPage("chatgpt", load_fixture("chatgpt_tool_call.html"))
        )
        provider = self._provider(page_driver=ctor_driver)
        result = await self._complete(provider, page_driver=call_driver)
        self.assertEqual(ctor_driver.acquire_calls, 0)
        self.assertEqual(call_driver.acquire_calls, 1)
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "run_command")

    async def test_driver_without_page_maps_to_need_help(self):
        result = await self._complete(self._provider(), page_driver=FakePageDriver(None))
        self.assertEqual(result["type"], "text")
        self.assertIn("NEED_HELP", result["content"])
        self.assertIn("lost the active browser chat", result["content"])

    async def test_injection_verification_gate_refuses_partial_prompt(self):
        # The fake editor corrupts every paste (echoes only half the prompt):
        # all injection strategies fail readback verification, the provider
        # raises rather than sending, and complete() maps it to NEED_HELP.
        page = FakeChatPage(
            "chatgpt", load_fixture("chatgpt_tool_call.html"), corrupt_paste=True
        )
        result = await self._complete(self._provider(), page_driver=FakePageDriver(page))
        self.assertEqual(result["type"], "text")
        self.assertIn("NEED_HELP", result["content"])
        self.assertIn("injection verification failed", result["content"].lower())
        self.assertEqual(page.submitted_prompts, [])  # never sent


if __name__ == "__main__":
    unittest.main()
