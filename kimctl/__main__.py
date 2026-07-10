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
    python -m kimctl compare --task "<task>" --providers p1,p2 [--timeout <sec>]
                             [--save-dir PATH | --no-save] [--config PATH] [--json]
    python -m kimctl trace [<session_id>] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
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


def _get_fallback_token() -> str:
    """Return fallback token from KIM_API_KEY or mcp_server config."""
    token = os.environ.get("KIM_API_KEY", "").strip()
    if not token:
        try:
            from mcp_server.config import get_config
            token = get_config().get("api_key", "")
        except ImportError:
            pass
    return token


def _read_bridge_url_file(root: Path) -> str:
    """Read URL from kim_sessions/.bridge_url."""
    token_file = root / "kim_sessions" / ".bridge_url"
    if token_file.exists():
        try:
            url_text = token_file.read_text(encoding="utf-8").strip()
            if url_text:
                return url_text
        except Exception:
            pass
    return ""


def _read_legacy_token_file(root: Path) -> tuple[str, str]:
    """Read (url, token) from kim_sessions/.bridge_token."""
    legacy_token_file = root / "kim_sessions" / ".bridge_token"
    if legacy_token_file.exists():
        try:
            lines = legacy_token_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) >= 2:
                return lines[0], lines[1]
        except Exception:
            pass
    return "", ""


def _read_config_yaml_bridge(root: Path) -> tuple[str, str]:
    """Read (url, token) from config.yaml."""
    config_file = root / "config.yaml"
    if config_file.exists():
        try:
            import yaml  # type: ignore
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            bp = cfg.get("browser_provider", {})
            return bp.get("bridge_url", ""), bp.get("bridge_token", "")
        except Exception:
            pass
    return "", ""


def _resolve_bridge() -> tuple[str, str]:
    """Return (base_url, token) for the bridge HTTP server."""
    url = os.environ.get("KIM_WEBVIEW_BRIDGE_URL", "").strip()
    token = os.environ.get("KIM_WEBVIEW_BRIDGE_TOKEN", "").strip()

    if url and token:
        return url, token

    if not token:
        token = _get_fallback_token()

    root = _kim_root()

    if not url:
        url = _read_bridge_url_file(root)

    if not url:
        legacy_url, legacy_token = _read_legacy_token_file(root)
        if legacy_url:
            url = legacy_url
            if not token:
                token = legacy_token

    if url and token:
        return url, token

    config_url, config_token = _read_config_yaml_bridge(root)
    if not url:
        url = config_url
    if not token:
        token = config_token

    if not url:
        url = "http://127.0.0.1:18991"

    return url, token


def _bridge_request(method: str, endpoint: str, **kwargs) -> httpx.Response:
    """Send an HTTP request to the bridge, closing the connection immediately."""
    base_url, token = _resolve_bridge()
    headers = kwargs.pop("headers", {})
    if token:
        headers["X-Kim-Token"] = token
    kwargs["headers"] = headers
    kwargs.setdefault("timeout", 10.0)
    return httpx.request(method, f"{base_url}{endpoint}", **kwargs)


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
        resp = _bridge_request("GET", "/v1/status")
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
        print("Kim Status:")
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
    payload: dict = {"task": args.task}
    if args.session:
        payload["session_id"] = args.session
    if args.provider:
        payload["provider"] = args.provider

    try:
        resp = _bridge_request("POST", "/v1/task", json=payload)
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
        print("   Check with: python -m kimctl status")
        print(f"   View logs:  python -m kimctl show {session_id}")
    sys.exit(EXIT_TIMEOUT)


def cmd_cancel(args):
    try:
        resp = _bridge_request("POST", "/v1/cancel", json={})
        data = resp.json()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    if args.json:
        _print_json(data)
    else:
        msg = data.get("message", "Done")
        print(f"{'✅' if data.get('ok') else '❌'} {msg}")


# ---------------------------------------------------------------------------
# Schedule helpers (local file ops -- no bridge required)
# ---------------------------------------------------------------------------

def _schedule_store_file() -> "Optional[Path]":
    """Return an override path for the schedule store, or None to use CronStore's default.

    Set KIM_SCHEDULES_FILE to point at a different file (useful for testing).
    When None is returned, CronStore falls back to its built-in default path
    (kim_schedules.json at the project root), keeping path logic in one place.
    """
    env = os.environ.get("KIM_SCHEDULES_FILE", "").strip()
    return Path(env) if env else None


def _warn_if_forbidden_provider(provider: Optional[str]) -> None:
    """Print a warning if the provider violates the scheduled-executor allowlist.

    Uses the same is_allowed_provider check as the runner so add-time warnings
    are always consistent with runtime enforcement.
    """
    if not provider:
        return
    from orchestrator.scheduled_runner import is_allowed_provider
    if not is_allowed_provider(provider):
        print(
            f"Warning: provider {provider!r} violates the scheduled-executor constraint "
            "(allowed: ollama, ollama-cloud, browser, browser:<site>); "
            "this task will be skipped at runtime.",
            file=sys.stderr,
        )


def _print_task_table(tasks: list) -> None:
    if not tasks:
        print("No scheduled tasks.")
        return
    print(f"{'ID':<32}  {'Expr':<14}  {'Runs':>4}  En  Task")
    print("-" * 78)
    for t in tasks:
        en = "Y" if t.enabled else "n"
        preview = t.task[:33] + "..." if len(t.task) > 36 else t.task
        print(f"{t.id:<32}  {t.schedule_expr:<14}  {t.run_count:>4}  {en:<2}  {preview}")


def _sch_list(store, args, use_json: bool) -> None:
    tasks = store.list_tasks(enabled_only=getattr(args, "enabled_only", False))
    if use_json:
        _print_json([t.to_dict() for t in tasks])
    else:
        _print_task_table(tasks)


def _sch_add(store, args, use_json: bool) -> None:
    _warn_if_forbidden_provider(getattr(args, "provider", None))
    enabled = not getattr(args, "disabled", False)
    try:
        t = store.add(
            args.task,
            args.expr,
            provider=getattr(args, "provider", None) or None,
            enabled=enabled,
        )
    except ValueError as exc:
        if use_json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if use_json:
        _print_json({"ok": True, "task": t.to_dict()})
    else:
        print(f"Added {t.id}  {t.schedule_expr}  {t.task!r}")


def _sch_update(store, args, use_json: bool) -> None:
    enable = getattr(args, "enable", False)
    disable = getattr(args, "disable", False)
    if enable and disable:
        msg = "--enable and --disable are mutually exclusive."
        if use_json:
            _print_json({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    kwargs: dict = {}
    task_text = getattr(args, "task_text", None)
    if task_text is not None:
        kwargs["task"] = task_text
    expr = getattr(args, "expr", None)
    if expr is not None:
        kwargs["schedule_expr"] = expr
    provider = getattr(args, "provider", None)
    if provider is not None:
        kwargs["provider"] = provider
    if enable:
        kwargs["enabled"] = True
    elif disable:
        kwargs["enabled"] = False

    if not kwargs:
        msg = "Nothing to update. Use --task, --expr, --provider, --enable, or --disable."
        if use_json:
            _print_json({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    try:
        t = store.update(args.id, **kwargs)
    except ValueError as exc:
        if use_json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if t is None:
        msg = f"Task {args.id!r} not found."
        if use_json:
            _print_json({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if use_json:
        _print_json({"ok": True, "task": t.to_dict()})
    else:
        print(f"Updated {t.id}")


def _sch_delete(store, args, use_json: bool) -> None:
    deleted = store.delete(args.id)
    if not deleted:
        msg = f"Task {args.id!r} not found."
        if use_json:
            _print_json({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)
    if use_json:
        _print_json({"ok": True, "id": args.id})
    else:
        print(f"Deleted {args.id}")


def _sch_due(store, args, use_json: bool) -> None:
    from datetime import datetime, timezone as _tz
    as_of = None
    as_of_str = getattr(args, "as_of", None)
    if as_of_str:
        try:
            dt = datetime.fromisoformat(as_of_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            as_of = dt
        except ValueError as exc:
            if use_json:
                _print_json({"ok": False, "error": f"Invalid --as-of: {exc}"})
            else:
                print(f"Error: invalid --as-of value: {exc}", file=sys.stderr)
            sys.exit(1)
    tasks = store.due_tasks(as_of=as_of)
    if use_json:
        _print_json([t.to_dict() for t in tasks])
    else:
        if not tasks:
            print("No tasks due.")
        else:
            _print_task_table(tasks)


def _sch_record_run(store, args, use_json: bool) -> None:
    from datetime import datetime, timezone as _tz
    ran_at = None
    at_str = getattr(args, "at", None)
    if at_str:
        try:
            dt = datetime.fromisoformat(at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            ran_at = dt
        except ValueError as exc:
            if use_json:
                _print_json({"ok": False, "error": f"Invalid --at: {exc}"})
            else:
                print(f"Error: invalid --at value: {exc}", file=sys.stderr)
            sys.exit(1)
    t = store.record_run(args.id, ran_at=ran_at)
    if t is None:
        msg = f"Task {args.id!r} not found or corrupt."
        if use_json:
            _print_json({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)
    if use_json:
        _print_json({"ok": True, "task": t.to_dict()})
    else:
        print(
            f"Recorded run for {t.id}  "
            f"run_count={t.run_count}  next_run_at={t.next_run_at}"
        )


def _sch_run_due(store, args, use_json: bool) -> None:
    from orchestrator.scheduled_runner import run_next_due_task
    dry_run = getattr(args, "dry_run", False)
    result = run_next_due_task(store_file=_schedule_store_file(), dry_run=dry_run)
    if result is None:
        if use_json:
            print(json.dumps({"ok": True, "launched": False, "reason": "no tasks due"}))
        else:
            print("No tasks due.")
        return
    if result.error:
        if use_json:
            print(json.dumps({"ok": False, **result.to_dict()}))
        else:
            print(f"Error launching task {result.task_id}: {result.error}", file=sys.stderr)
        sys.exit(1)
    if use_json:
        print(json.dumps({"ok": True, **result.to_dict()}))
    else:
        if result.skipped:
            print(f"Skipped: {result.task_text!r} — {result.skip_reason}")
        elif result.launched:
            print(f"Launched: {result.task_text!r} (id={result.task_id})")
            if result.recorded:
                print("  run recorded — next_run_at advanced")
            if result.log_file:
                print(f"  agent log: {result.log_file}")


_SCH_HANDLERS: dict = {
    "list": _sch_list,
    "add": _sch_add,
    "update": _sch_update,
    "delete": _sch_delete,
    "due": _sch_due,
    "record-run": _sch_record_run,
    "run-due": _sch_run_due,
}


def cmd_schedule(args):
    """Manage scheduled agent tasks (local file ops, no bridge required).

    Provider constraint: scheduled tasks in Code-tab context may only use
    ollama or browser providers, never openai/gpt variants. The CLI warns on
    add; enforcement is the executor's responsibility (a later slice).
    """
    from orchestrator.cron_store import CronStore
    store = CronStore(store_file=_schedule_store_file())

    action = getattr(args, "schedule_action", None)
    use_json = getattr(args, "json", False)

    if not action or action not in _SCH_HANDLERS:
        print(
            "Usage: kimctl schedule {list,add,update,delete,due,record-run,run-due}",
            file=sys.stderr,
        )
        sys.exit(1)

    _SCH_HANDLERS[action](store, args, use_json)


def cmd_compare(args):
    """Run the same task through multiple providers and print a structured comparison."""
    # Parse provider list
    raw_providers = getattr(args, "providers", "") or ""
    providers = [p.strip() for p in raw_providers.split(",")]
    if not raw_providers.strip():
        if args.json:
            _print_json({"ok": False, "error": "--providers is required (comma-separated list)"})
        else:
            print("Error: --providers is required (comma-separated list)", file=sys.stderr)
        sys.exit(1)

    # Resolve save directory
    no_save = getattr(args, "no_save", False)
    save_dir_arg = getattr(args, "save_dir", None)
    if no_save:
        save_dir = Path(os.devnull)
    elif save_dir_arg:
        save_dir = Path(save_dir_arg)
    else:
        save_dir = None  # compare_providers will use its default (kim_comparisons/)

    timeout = float(getattr(args, "timeout", 120) or 120)

    try:
        from orchestrator.compare import compare_providers
        _validate_compare_request(args.task, providers, timeout)
    except ValueError as exc:
        if args.json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Load kim config for provider credentials / MCP server path. Keep this
    # independent from orchestrator.agent so lightweight CLI/test paths do not
    # require importing the MCP client package until a real comparison starts.
    try:
        config = _load_compare_config(getattr(args, "config", None))
    except Exception as exc:
        if args.json:
            _print_json({"ok": False, "error": f"could not load config: {exc}"})
        else:
            print(f"Error: could not load config: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        results, saved_path = asyncio.run(compare_providers(
            task=args.task,
            providers=providers,
            config=config,
            timeout_seconds=timeout,
            save_dir=save_dir,
            _session_factory=getattr(args, "_session_factory", None),
        ))
    except ValueError as exc:
        if args.json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        if args.json:
            _print_json({"ok": False, "error": f"comparison failed: {exc}"})
        else:
            print(f"Error: comparison failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _print_json({
            "ok": True,
            "results": results,
            "saved": str(saved_path) if saved_path else None,
        })
    else:
        _print_comparison_table(results)
        if saved_path:
            print(f"Saved: {saved_path}")


def _validate_compare_request(task: str, providers: list, timeout: float) -> None:
    if not task or not task.strip():
        raise ValueError("task must not be empty")
    if not providers:
        raise ValueError("providers list must not be empty")
    providers = [str(p).strip() for p in providers]
    if any(not p for p in providers):
        raise ValueError("provider names must not be empty")
    seen = set()
    duplicates = []
    for provider in providers:
        key = provider.lower()
        if key in seen:
            duplicates.append(provider)
        seen.add(key)
    if duplicates:
        raise ValueError(f"duplicate providers are not allowed: {', '.join(duplicates)}")
    if len(providers) > 8:
        raise ValueError(f"compare_providers: at most 8 providers allowed, got {len(providers)}")
    if timeout < 1:
        raise ValueError(f"timeout_seconds must be >= 1, got {timeout}")


def _load_compare_config(config_path: Optional[str]) -> dict:
    path = Path(config_path) if config_path else _kim_root() / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read config.yaml") from exc
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _print_comparison_table(results: list) -> None:
    if not results:
        print("No results.")
        return
    print(f"{'Provider':<20}  {'OK':>3}  {'Duration':>9}  Summary / Error")
    print("-" * 72)
    for r in results:
        ok_flag = "yes" if r.get("success") else "no"
        dur = f"{r.get('duration_seconds', 0.0):.1f}s"
        detail = r.get("summary") or r.get("error") or r.get("termination") or "--"
        if len(detail) > 38:
            detail = detail[:35] + "..."
        print(f"{r.get('provider', '?'):<20}  {ok_flag:>3}  {dur:>9}  {detail}")


def cmd_trace(args):
    """Print a trace summary for one session or all sessions (read-only, no bridge needed)."""
    from orchestrator.session_store import SessionStore

    session_id = getattr(args, "session_id", None) or None
    base = _sessions_dir()

    if session_id and not SessionStore.session_exists(session_id, base_dir=base):
        if args.json:
            _print_json({"ok": False, "error": f"Session '{session_id}' not found."})
        else:
            print(f"Session '{session_id}' not found.", file=sys.stderr)
        sys.exit(EXIT_TRANSPORT)

    summary = SessionStore.summarize_trace_events(session_id=session_id, base_dir=base)

    if args.json:
        _print_json(summary)
        return

    # Human-readable output
    sid_label = summary["session_id"] or "(all sessions)"
    print(f"Trace summary  (session: {sid_label})")
    print("─" * 50)
    print(f"Events: {summary['event_count']}")

    by_type = summary["by_type"]
    if by_type:
        type_parts = "  ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
        print(f"  by type:    {type_parts}")

    tc = summary["tool_calls"]
    print(
        f"\nTool calls:   started={tc['started']}  "
        f"completed={tc['completed']}  errored={tc['errored']}"
    )
    if tc["by_tool"]:
        by_tool = "  ".join(f"{k}×{v}" for k, v in sorted(tc["by_tool"].items()))
        print(f"  by tool:    {by_tool}")
    if tc["error_codes"]:
        ec = "  ".join(f"{k}×{v}" for k, v in sorted(tc["error_codes"].items()))
        print(f"  error_codes: {ec}")
    if tc["risk_levels"]:
        rl = "  ".join(f"{k}×{v}" for k, v in sorted(tc["risk_levels"].items()))
        print(f"  risk_levels: {rl}")

    lt = summary["llm_turns"]
    print(
        f"\nLLM turns:    started={lt['started']}  "
        f"completed={lt['completed']}  errored={lt['errored']}"
    )
    if lt["by_provider"]:
        by_prov = "  ".join(f"{k}×{v}" for k, v in sorted(lt["by_provider"].items()))
        print(f"  by provider: {by_prov}")
    if lt["error_codes"]:
        ec = "  ".join(f"{k}×{v}" for k, v in sorted(lt["error_codes"].items()))
        print(f"  error_codes: {ec}")

    run = summary["run"]
    print(f"\nRun:          started={run['started']}  result={run['result']}")
    if run["result"] > 0:
        print(f"  success:     {run['success']}")
        print(f"  termination: {run['termination']}")


def cmd_browser(args):
    if args.browser_action == "show":
        resp = _bridge_request("POST", "/v1/browser/show", json={})
    elif args.browser_action == "hide":
        resp = _bridge_request("POST", "/v1/browser/hide", json={})
    elif args.browser_action == "new-chat":
        resp = _bridge_request("POST", "/v1/browser/new-chat", json={})
    elif args.browser_action == "click":
        if not args.selector:
            print("Error: --selector is required for 'click'", file=sys.stderr)
            sys.exit(1)
        resp = _bridge_request("POST", "/v1/browser/click", json={"selector": args.selector})
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

    # schedule
    sp = sub.add_parser("schedule", help="Manage scheduled agent tasks (local, no bridge)")
    ssp = sp.add_subparsers(dest="schedule_action", help="Schedule subcommand")

    s = ssp.add_parser("list", help="List scheduled tasks")
    s.add_argument("--enabled-only", dest="enabled_only", action="store_true",
                   help="Show only enabled tasks")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser(
        "add",
        help="Add a scheduled task (Code-tab: provider must be ollama or browser, not openai/gpt)",
    )
    s.add_argument("task", help="Task text to run on schedule")
    s.add_argument("expr", help="Schedule expr: @hourly, @daily, @weekly, @every <N>m/h/d")
    s.add_argument("--provider", metavar="NAME", help="Provider hint (ollama, browser, ...)")
    s.add_argument("--disabled", action="store_true", help="Create as disabled")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser("update", help="Update fields on a scheduled task")
    s.add_argument("id", help="Task ID to update")
    s.add_argument("--task", dest="task_text", metavar="TEXT", help="New task text")
    s.add_argument("--expr", metavar="EXPR", help="New schedule expression")
    s.add_argument("--provider", metavar="NAME", help="New provider hint")
    s.add_argument("--enable", action="store_true", help="Enable the task")
    s.add_argument("--disable", action="store_true", help="Disable the task")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser("delete", help="Delete a scheduled task")
    s.add_argument("id", help="Task ID to delete")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser("due", help="List tasks due now (or at --as-of)")
    s.add_argument("--as-of", dest="as_of", metavar="ISO_DATETIME",
                   help="Check due tasks at this UTC datetime instead of now")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser("record-run", help="Record that a scheduled task ran")
    s.add_argument("id", help="Task ID that ran")
    s.add_argument("--at", dest="at", metavar="ISO_DATETIME",
                   help="When the task ran (default: now UTC)")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    s = ssp.add_parser(
        "run-due",
        help=(
            "Launch the next due task via orchestrator.agent. "
            "Only ollama/ollama-cloud/browser providers are allowed."
        ),
    )
    s.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Show what would run without spawning")
    s.add_argument("--json", action="store_true", help="Machine-readable output")

    # compare
    sp = sub.add_parser(
        "compare",
        help="Run a task through multiple providers and compare results",
    )
    sp.add_argument("--task", "-t", required=True, metavar="TASK",
                    help="Task text to run through each provider")
    sp.add_argument("--providers", required=True, metavar="P1,P2,...",
                    help="Comma-separated provider names (e.g. ollama,claude)")
    sp.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS",
                    help="Per-provider timeout in seconds (default: 120)")
    sp.add_argument("--save-dir", dest="save_dir", metavar="PATH",
                    help="Directory to save comparison JSON (default: kim_comparisons/)")
    sp.add_argument("--no-save", dest="no_save", action="store_true",
                    help="Do not save comparison results to disk")
    sp.add_argument("--config", metavar="PATH", help="Path to config.yaml")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # trace
    sp = sub.add_parser(
        "trace",
        help="Print trace summary for one session or all sessions (read-only, no bridge needed)",
    )
    sp.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Session ID to inspect (omit to summarize all sessions)",
    )
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    # browser
    sp = sub.add_parser("browser", help="Control the in-app browser")
    sp.add_argument(
        "browser_action",
        choices=["show", "hide", "click", "new-chat"],
        help="Browser action",
    )
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
        "schedule": cmd_schedule,
        "compare": cmd_compare,
        "trace": cmd_trace,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
