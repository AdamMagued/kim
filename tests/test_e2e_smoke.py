"""T6 — local, headless, offline E2E smoke for the codex relay loop.

Unlike the codex_bridge_harness tests (which stub ``_CodexProxy``), this test
runs the REAL proxy: a fake ``codex`` binary performs an actual HTTP request
against the aiohttp Responses endpoint the bridge started on loopback, the
proxy relays it to a canned FakeBrowserProvider, and the fake codex records
the SSE frames it received. That proves the full
``bridge → real proxy (auth, translation, SSE) → codex`` loop end-to-end with
no real browser and no network beyond 127.0.0.1.

Layer map:
    codex_bridge_service._run_async     (real)
    _CodexProxy + aiohttp server        (real, loopback port)
    bearer-token auth                   (real — fake codex uses CODEX_API_KEY)
    _provider_response_to_responses_api (real)
    SSE framing                         (real)
    BrowserProvider                     (canned fake — the only stub)
    codex binary                        (fake: python script doing real HTTP)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_bridge_harness import _SCRUBBED_VARS

# A fake codex that exercises the REAL relay contract: read the proxy base URL
# + bearer token from env (exactly like real codex), POST a streaming
# Responses-API request, record the SSE bytes, emit one codex-style JSONL
# event, and exit 0.
_FAKE_CODEX_TEMPLATE = """#!{python}
import json, os, sys, urllib.request

here = os.path.dirname(os.path.abspath(__file__))
base = os.environ["OPENAI_BASE_URL"].rstrip("/")
key = os.environ["CODEX_API_KEY"]
task = sys.argv[-1]

body = {{
    "model": "gpt-5",
    "stream": True,
    "instructions": "You are Codex running under test.",
    "input": [{{"role": "user", "content": task}}],
    "tools": [{{"type": "function", "name": "shell"}}],
}}
req = urllib.request.Request(
    base + "/responses",
    data=json.dumps(body).encode(),
    headers={{
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = resp.read().decode("utf-8", errors="replace")
with open(os.path.join(here, "sse.txt"), "w") as f:
    f.write(data)
print(json.dumps({{
    "type": "item.completed",
    "item": {{"type": "agent_message", "text": "smoke ok"}},
}}), flush=True)
sys.exit(0)
"""


class CannedProvider:
    """The one stub: returns scripted browser replies, records prompts."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._sent_system_prompt = True

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0) if self.replies else "DONE"
        return {"type": "text", "content": content}


async def _run_smoke(tmp: Path, replies: list[str]) -> SimpleNamespace:
    from orchestrator import codex_bridge_service as svc
    import codex_engine.thread_state as ts

    project = tmp / "project"
    project.mkdir()
    (project / ".git").mkdir()

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake-codex"
    script.write_text(_FAKE_CODEX_TEMPLATE.format(python=sys.executable))
    script.chmod(0o755)

    config_path = tmp / "config.yaml"
    # This smoke drives the legacy exec transport end-to-end (the fake codex
    # emulates `codex exec --json`); the service default is now app-server.
    config_path.write_text("browser_provider: {}\ncodex_bridge:\n  transport: exec\n")

    provider = CannedProvider(replies)

    parent_env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_VARS}
    parent_env["CODEX_BIN"] = str(script)

    args = Namespace(
        task="make a tiny pong page",
        cwd=str(project),
        provider="browser:gemini",
        model=None,
        config=str(config_path),
        verbose=False,
    )

    with (
        patch.object(os, "environ", parent_env),
        patch.object(ts, "_STATE_DIR", tmp / "state"),
        patch.object(svc, "create_provider", return_value=provider),
    ):
        rc = await svc._run_async(args)

    sse_file = bin_dir / "sse.txt"
    sse = sse_file.read_text() if sse_file.exists() else None
    return SimpleNamespace(rc=rc, sse=sse, provider=provider)


def _frames(sse: str) -> list[dict]:
    out = []
    for chunk in sse.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data: "):
            continue
        payload = chunk[len("data: "):]
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


class TestE2ESmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kim-e2e-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_full_loop_text_reply(self):
        """codex → real proxy over loopback HTTP → canned text reply → SSE."""
        result = await _run_smoke(self.tmp, ["All wired up.\nDONE"])
        self.assertEqual(result.rc, 0)
        self.assertIsNotNone(result.sse, "fake codex never received SSE")
        assert result.sse is not None
        self.assertIn("data: [DONE]", result.sse)

        frames = _frames(result.sse)
        types = [f.get("type") for f in frames]
        self.assertEqual(types[0], "response.created")
        self.assertIn("response.output_text.delta", types)
        self.assertEqual(types[-1], "response.completed")
        delta = next(f for f in frames if f["type"] == "response.output_text.delta")
        self.assertIn("All wired up.", delta["delta"])

        # The provider saw exactly one relay carrying the codex request.
        self.assertEqual(len(result.provider.calls), 1)
        prompt = str(result.provider.calls[0].get("messages"))
        self.assertIn("make a tiny pong page", prompt)

    async def test_full_loop_tool_call_reply(self):
        """A canned contract reply with a tool_call surfaces as real
        function_call SSE frames — the frames codex acts on."""
        contract = json.dumps(
            {
                "text": "Creating the file.",
                "tool_calls": [
                    {"name": "shell", "input": {"cmd": "echo pong > pong.html"}}
                ],
            }
        )
        result = await _run_smoke(self.tmp, [contract])
        self.assertEqual(result.rc, 0)
        assert result.sse is not None

        frames = _frames(result.sse)
        fn_done = [
            f for f in frames if f.get("type") == "response.function_call_arguments.done"
        ]
        self.assertEqual(len(fn_done), 1)
        self.assertIn("pong.html", fn_done[0]["arguments"])
        completed = next(f for f in frames if f["type"] == "response.completed")
        calls = [
            item
            for item in completed["response"]["output"]
            if item.get("type") == "function_call"
        ]
        self.assertEqual(calls[0]["name"], "shell")

    async def test_bad_bearer_token_is_rejected_by_real_auth(self):
        """The real proxy's auth gate: a wrong token gets 401, proving the
        bearer round-trip is live (not a stub)."""
        # Sabotage the token the fake codex will present.
        sabotaged = _FAKE_CODEX_TEMPLATE.replace(
            'key = os.environ["CODEX_API_KEY"]', 'key = "wrong-token"'
        )
        with patch.dict(globals(), {"_FAKE_CODEX_TEMPLATE": sabotaged}):
            result = await _run_smoke(self.tmp, ["DONE"])

        # urlopen raises on 401 → fake codex dies non-zero → bridge fails.
        self.assertNotEqual(result.rc, 0)
        self.assertIsNone(result.sse)
        self.assertEqual(result.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
