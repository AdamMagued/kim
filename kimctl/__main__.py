"""
kimctl — terminal control surface for Kim.

Usage:
    python -m kimctl status
    python -m kimctl chats [--json]
    python -m kimctl show <session_id> [--last N] [--json]
    python -m kimctl send "<task>" [--session <id>] [--provider <name>]
                                   [--timeout <sec>] [--detach] [--json]
    python -m kimctl cancel [--json]
    python -m kimctl browser show
    python -m kimctl browser hide
    python -m kimctl browser click "<selector>"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_NEED_HELP = 1
EXIT_TIMEOUT = 2
EXIT_TRANSPORT = 3

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def _kim_root() -> Path:
    """Best-effort Kim project root (where kim_sessions/ lives)."""
    # Walk up from this file: kimctl/__main__.py → kim/
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "kim_sessions").exists() or (candidate / "orchestrator").exists():
        return candidate
    return Path.cwd()


def _resolve_bridge() -> tuple[str, str]:
    """Return (base_url, token) for the bridge HTTP server."""
    url = os.environ.get("KIM_WEBVIEW_BRIDGE_URL", "").strip()
    token = os.environ.get("KIM_WEBVIEW_BRIDGE_TOKEN", "").strip()

    if url and token:
        return url, token

    # Try reading kim_sessions/.bridge_token
    root = _kim_root()
    token_file = root / "kim_sessions" / ".bridge_token"
    if token_file.exists():
        try:
            lines = token_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) >= 2:
                if not url:
                    url = lines[0]
                if not token:
                    token = lines[1]
        except Exception:
            pass

    if url and token:
        return url, token

    # Try reading config.yaml
    config_file = root / "config.yaml"
    if config_file.exists():
        try:
            import yaml  # type: ignore
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            bp = cfg.get("browser_provider", {})
            if not url:
                url = bp.get("bridge_url", "")
            if not token:
                token = bp.get("bridge_token", "")
        except Exception:
            pass

    if not url:
        url = "http://127.0.0.1:18991"
    return url, token


def _bridge_client() -> tuple[httpx.Client, str]:
    """Return (httpx.Client with auth header, base_url)."""
    base_url, token = _resolve_bridge()
    headers = {}
    if token:
        headers["X-Kim-Token"] = token
    client = httpx.Client(timeout=10.0, headers=headers)
    return client, base_url


def _sessions_dir() -> Path:
    """Return the kim_sessions directory."""
    env = os.environ.get("KIM_SESSIONS_DIR", "").strip()
    if env:
        return Path(env)
    return _kim_root() / "kim_sessions"


# ---------------------------------------------------------------------------
# Session helpers (read JSONL directly — no HTTP needed)
# ---------------------------------------------------------------------------

def _list_sessions() -> list[dict]:
    """Walk kim_sessions/*/*.jsonl and return metadata."""
    base = _sessions_dir()
    if not base.exists():
        return []

    sessions = []
    for date_dir in sorted(base.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for jsonl_file in sorted(date_dir.glob("*.jsonl"), reverse=True):
            session_id = jsonl_file.stem
            try:
                lines = jsonl_file.read_text(encoding="utf-8").strip().splitlines()
                msg_count = len(lines)
                # Find first user message for preview
                first_user = ""
                for line in lines:
                    try:
                        msg = json.loads(line)
                        if msg.get("role") == "user":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                # multimodal — find first text block
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        content = item.get("text", "")
                                        break
                                else:
                                    content = "(multimodal)"
                            first_user = str(content)[:60]
                            break
                    except json.JSONDecodeError:
                        continue
            except Exception:
                msg_count = 0
                first_user = ""

            sessions.append({
                "id": session_id,
                "date": date_dir.name,
                "messages": msg_count,
                "preview": first_user,
                "path": str(jsonl_file),
            })

    return sessions


def _load_session_messages(session_id: str) -> list[dict]:
    """Load all messages from a session JSONL."""
    base = _sessions_dir()
    if not base.exists():
        return []

    for date_dir in sorted(base.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        candidate = date_dir / f"{session_id}.jsonl"
        if candidate.exists():
            messages = []
            for line in candidate.read_text(encoding="utf-8").strip().splitlines():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return messages
    return []


def _find_session_file(session_id: str) -> Optional[Path]:
    """Find the JSONL file for a session ID."""
    base = _sessions_dir()
    if not base.exists():
        return None
    for date_dir in sorted(base.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        candidate = date_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _format_message(msg: dict) -> str:
    role = msg.get("role", "?")
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    parts.append("(screenshot)")
        content = "\n".join(parts)

    prefix = "\033[36muser\033[0m" if role == "user" else "\033[33massistant\033[0m"
    return f"{prefix}: {content}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args):
    try:
        client, base = _bridge_client()
        resp = client.get(f"{base}/v1/status")
        data = resp.json()
    except Exception as e:
        print(f"Error connecting to Kim bridge: {e}", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    if args.json:
        _print_json(data)
    else:
        running = "✅ Yes" if data.get("has_running_task") else "❌ No"
        browser = "👁 Visible" if data.get("browser_visible") else "🔒 Hidden"
        session = data.get("active_session_id") or "—"
        print(f"Kim Status:")
        print(f"  Running task:  {running}")
        print(f"  Session:       {session}")
        print(f"  Browser:       {browser}")


def cmd_chats(args):
    sessions = _list_sessions()
    if args.json:
        _print_json(sessions)
    else:
        if not sessions:
            print("No sessions found.")
            return
        print(f"{'ID':<12} {'Date':<12} {'Msgs':>5}  Preview")
        print("─" * 70)
        for s in sessions:
            preview = s["preview"] or "(empty)"
            print(f"{s['id']:<12} {s['date']:<12} {s['messages']:>5}  {preview}")


def cmd_show(args):
    messages = _load_session_messages(args.session_id)
    if not messages:
        print(f"Session '{args.session_id}' not found.", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    if args.last:
        messages = messages[-args.last:]

    if args.json:
        _print_json(messages)
    else:
        for msg in messages:
            print(_format_message(msg))
            print()


def cmd_send(args):
    client, base = _bridge_client()

    payload: dict = {"task": args.task}
    if args.session:
        payload["session_id"] = args.session
    if args.provider:
        payload["provider"] = args.provider

    try:
        resp = client.post(f"{base}/v1/task", json=payload)
        data = resp.json()
    except Exception as e:
        print(f"Error connecting to Kim bridge: {e}", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    if not data.get("ok"):
        err = data.get("error", "Unknown error")
        if args.json:
            _print_json({"ok": False, "error": err})
        else:
            print(f"Error: {err}", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    session_id = data.get("session_id", "")
    sessions_dir = data.get("sessions_dir", "")

    if args.json and args.detach:
        _print_json({"ok": True, "session_id": session_id})
        sys.exit(EXIT_OK)
    elif args.detach:
        print(f"Task started. Session: {session_id}")
        sys.exit(EXIT_OK)

    # Blocking mode: poll the session JSONL for completion
    if not args.json:
        print(f"Task started (session: {session_id}). Waiting for completion...")

    timeout = args.timeout or 300
    deadline = time.time() + timeout
    poll_interval = 0.5
    last_offset = 0
    session_file: Optional[Path] = None

    # Wait briefly for the JSONL file to appear
    for _ in range(20):
        session_file = _find_session_file(session_id)
        if session_file:
            break
        # Also check sessions_dir directly if provided
        if sessions_dir:
            import datetime
            today = datetime.date.today().isoformat()
            candidate = Path(sessions_dir) / today / f"{session_id}.jsonl"
            if candidate.exists():
                session_file = candidate
                break
        time.sleep(0.5)

    if not session_file:
        if args.json:
            _print_json({"ok": False, "error": "Session file not created within 10s."})
        else:
            print("Error: Session file not created within 10s.", file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)

    # Poll for TASK_COMPLETE / NEED_HELP
    while time.time() < deadline:
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                f.seek(last_offset)
                new_data = f.read()
                last_offset = f.tell()
        except FileNotFoundError:
            time.sleep(poll_interval)
            continue

        if new_data.strip():
            for line in new_data.strip().splitlines():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("role") != "assistant":
                    continue

                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    content = "\n".join(parts)

                # Check for completion
                m_complete = re.search(
                    r"\bTASK_COMPLETE:\s*(.+)$", content,
                    re.IGNORECASE | re.MULTILINE
                )
                if m_complete:
                    summary = m_complete.group(1).strip()
                    if args.json:
                        _print_json({"ok": True, "status": "complete", "summary": summary, "session_id": session_id})
                    else:
                        print(f"\n✅ TASK_COMPLETE: {summary}")
                    sys.exit(EXIT_OK)

                m_help = re.search(
                    r"\bNEED_HELP:\s*(.+)$", content,
                    re.IGNORECASE | re.MULTILINE
                )
                if m_help:
                    reason = m_help.group(1).strip()
                    if args.json:
                        _print_json({"ok": False, "status": "need_help", "reason": reason, "session_id": session_id})
                    else:
                        print(f"\n⚠️  NEED_HELP: {reason}")
                    sys.exit(EXIT_NEED_HELP)

        time.sleep(poll_interval)

    # Timeout
    if args.json:
        _print_json({"ok": False, "status": "timeout", "session_id": session_id})
    else:
        print(f"\n⏰ Timeout after {timeout}s. Task may still be running.", file=sys.stderr)
        print(f"   Check with: python -m kimctl status")
        print(f"   View logs:  python -m kimctl show {session_id}")
    sys.exit(EXIT_TIMEOUT)


def cmd_cancel(args):
    try:
        client, base = _bridge_client()
        resp = client.post(f"{base}/v1/cancel", json={})
        data = resp.json()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    if args.json:
        _print_json(data)
    else:
        msg = data.get("message", "Done")
        print(f"{'✅' if data.get('ok') else '❌'} {msg}")


def cmd_browser(args):
    client, base = _bridge_client()

    if args.browser_action == "show":
        resp = client.post(f"{base}/v1/browser/show", json={})
    elif args.browser_action == "hide":
        resp = client.post(f"{base}/v1/browser/hide", json={})
    elif args.browser_action == "new-chat":
        resp = client.post(f"{base}/v1/browser/new-chat", json={})
    elif args.browser_action == "click":
        if not args.selector:
            print("Error: --selector is required for 'click'", file=sys.stderr)
            sys.exit(1)
        resp = client.post(f"{base}/v1/browser/click", json={"selector": args.selector})
    else:
        print(f"Unknown browser action: {args.browser_action}", file=sys.stderr)
        sys.exit(1)

    try:
        data = resp.json()
    except Exception:
        data = {"ok": False, "error": resp.text[:200]}

    if hasattr(args, "json") and args.json:
        _print_json(data)
    else:
        if data.get("ok"):
            print("✅ Done")
        else:
            print(f"❌ {data.get('error', 'Failed')}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kimctl",
        description="Terminal control surface for Kim",
    )
    sub = p.add_subparsers(dest="command", help="Available commands")

    # status
    sp = sub.add_parser("status", help="Show Kim bridge status")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # chats
    sp = sub.add_parser("chats", help="List session history")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # show
    sp = sub.add_parser("show", help="Print messages from a session")
    sp.add_argument("session_id", help="Session ID to display")
    sp.add_argument("--last", type=int, help="Show only the last N messages")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # send
    sp = sub.add_parser("send", help="Send a task to Kim")
    sp.add_argument("task", help="Task description")
    sp.add_argument("--session", metavar="ID", help="Resume an existing session")
    sp.add_argument("--provider", metavar="NAME", help="LLM provider (e.g. browser, claude)")
    sp.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    sp.add_argument("--detach", action="store_true", help="Don't wait for completion")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # cancel
    sp = sub.add_parser("cancel", help="Cancel the running task")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # browser
    sp = sub.add_parser("browser", help="Control the in-app browser")
    sp.add_argument("browser_action", choices=["show", "hide", "click", "new-chat"],
                     help="Browser action")
    sp.add_argument("selector", nargs="?", help="CSS selector (for click)")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "status": cmd_status,
        "chats": cmd_chats,
        "show": cmd_show,
        "send": cmd_send,
        "cancel": cmd_cancel,
        "browser": cmd_browser,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
