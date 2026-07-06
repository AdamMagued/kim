"""Fake `codex app-server` for AppServerClient unit tests.

Speaks newline-delimited JSON-RPC 2.0 on stdio. Scenario behaviors are keyed
on the request method so each test drives exactly the shape it needs:

  initialize            → normal result
  thread/start          → {"thread": {"id": "th_fake_1"}}
  echo/hold             → response deferred until echo/release arrives
  echo/release          → responds to itself FIRST, then the held request
                          (out-of-order correlation)
  approval/trigger      → server sends its own item/commandExecution/
                          requestApproval request; the client's decision is
                          echoed back as a test/approvalResult notification
                          (and written to argv[1] if given, then the fake
                          exits — deterministic shutdown-auto-decline proof)
  garbage/then-ok       → emits a non-JSON line, then a normal response
  notify/unknown        → emits a totally/unknown notification, then responds
  error/trigger         → responds with a JSON-RPC error object
  die                   → writes stderr breadcrumbs and exits, no response
  never/respond         → never responds (client-timeout tests)

Run: python fake_app_server.py [decision_out_file]
"""

from __future__ import annotations

import json
import sys


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    decision_out = sys.argv[1] if len(sys.argv) > 1 else None
    held_id = None
    server_req_id = "srv-1"
    awaiting_decision = False

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)

        # A response to OUR server-initiated request?
        if awaiting_decision and msg.get("id") == server_req_id and "method" not in msg:
            decision = (msg.get("result") or {}).get("decision")
            send({"jsonrpc": "2.0", "method": "test/approvalResult",
                  "params": {"decision": decision}})
            if decision_out:
                with open(decision_out, "w", encoding="utf-8") as fh:
                    json.dump({"decision": decision}, fh)
                return  # deterministic exit for the shutdown-auto-decline test
            awaiting_decision = False
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        if method is None:
            continue

        if method == "initialized":
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id,
                  "result": {"userAgent": "fake-app-server/0.0.1"}})
        elif method == "thread/start":
            send({"jsonrpc": "2.0", "id": req_id,
                  "result": {"thread": {"id": "th_fake_1"}}})
        elif method == "echo/hold":
            held_id = req_id
        elif method == "echo/release":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"which": "release"}})
            if held_id is not None:
                send({"jsonrpc": "2.0", "id": held_id, "result": {"which": "hold"}})
                held_id = None
        elif method == "approval/trigger":
            awaiting_decision = True
            send({"jsonrpc": "2.0", "id": server_req_id,
                  "method": "item/commandExecution/requestApproval",
                  "params": {"itemId": "item_1", "command": "touch x",
                             "cwd": "/tmp", "threadId": "th_fake_1",
                             "turnId": "turn_1"}})
            send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
        elif method == "garbage/then-ok":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
        elif method == "notify/unknown":
            send({"jsonrpc": "2.0", "method": "totally/unknown",
                  "params": {"n": 1}})
            send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
        elif method == "error/trigger":
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32000, "message": "fake failure"}})
        elif method == "die":
            print("fake server dying now", file=sys.stderr)
            sys.stderr.flush()
            return
        elif method == "never/respond":
            pass
        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})


if __name__ == "__main__":
    main()
