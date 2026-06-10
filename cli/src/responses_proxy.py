#!/usr/bin/env python3
"""
Minimal OpenAI Responses API -> Chat Completions proxy.
Codex CLI uses the Responses API; this proxy lets it talk to ollama
(which only speaks Chat Completions).

Usage: python3 responses_proxy.py <ollama_base_url>
Prints the port it bound to on the first line of stdout, then serves.
"""
import json, sys, uuid, socket, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

TARGET = sys.argv[1].rstrip("/")  # e.g. http://127.0.0.1:11434/v1
_LOG = open("/tmp/kim_proxy.log", "a", buffering=1)

def _log(*args):
    import datetime
    print(datetime.datetime.now().isoformat(), *args, file=_LOG, flush=True)


def clean_image_data(text):
    if not isinstance(text, str):
        return text
    if "data:image/" in text:
        import re
        text = re.sub(r'data:image/[a-zA-Z0-9.-]+;base64,[A-Za-z0-9+/=]+', '[Image content stripped]', text)
    return text


def input_to_messages(input_items):
    msgs = []
    for item in (input_items or []):
        if not isinstance(item, dict):
            continue
        t = item.get("type", "")
        role = item.get("role", "user")
        if t == "function_call_output":
            msgs.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": clean_image_data(str(item.get("output", ""))),
            })
            continue
        if t == "function_call":
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id", item.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }],
            })
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                bt = block.get("type", "")
                if bt in ("input_text", "output_text", "text"):
                    parts.append(block.get("text", ""))
                elif bt == "tool_result":
                    parts.append(clean_image_data(str(block.get("output", ""))))
            content = "\n".join(parts)
        else:
            content = clean_image_data(str(content))
        msgs.append({"role": role, "content": content})
    return msgs


def tools_to_chat(resp_tools):
    out = []
    for t in (resp_tools or []):
        if t.get("type") == "function":
            if "function" in t:
                # Already in Chat Completions nested format
                out.append(t)
            else:
                # Responses API flat format → convert to Chat Completions nested format
                out.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
        elif "name" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        upgrade = self.headers.get("Upgrade", "").lower()
        _log(f"GET path={self.path!r} upgrade={upgrade!r} headers={dict(self.headers)}")
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            self.send_error(400, "Bad JSON")
            return

        model = req.get("model", "llama3.2")
        messages = input_to_messages(req.get("input", []))
        tools = tools_to_chat(req.get("tools", []))
        stream = req.get("stream", False)
        _log(f"POST model={model!r} stream={stream} path={self.path!r} msgs={len(messages)}")

        chat = {"model": model, "messages": messages, "stream": stream}
        if tools:
            chat["tools"] = tools
            chat["tool_choice"] = "auto"

        url = TARGET + "/chat/completions"
        try:
            http_req = Request(
                url,
                data=json.dumps(chat).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer ollama",
                },
            )
            with urlopen(http_req, timeout=300) as resp:
                _log(f"ollama HTTP status={resp.status}")
                if stream:
                    self._handle_stream(resp)
                else:
                    self._handle_once(resp)
        except HTTPError as e:
            err_body = e.read()
            _log(f"HTTPError {e.code}: {err_body[:200]}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            _log(f"Exception in do_POST: {e}\n{traceback.format_exc()}")
            self.send_error(502, str(e))

    def _handle_once(self, resp):
        data = json.loads(resp.read())
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        rid = "resp_" + uuid.uuid4().hex[:16]
        output = []
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                tc_id = tc.get("id", uuid.uuid4().hex[:8])
                output.append({
                    "type": "function_call",
                    "id": tc_id,
                    "call_id": tc_id,
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                    "status": "completed",
                })
        if text:
            output.append({
                "type": "message",
                "id": "msg_" + rid,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            })
        result = {
            "id": rid, "object": "response", "created_at": 0, "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            },
        }
        out = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_stream(self, resp):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        rid = "resp_" + uuid.uuid4().hex[:16]
        msg_id = "msg_" + rid
        seq = 0

        def emit(event_type, data):
            nonlocal seq
            seq += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            _log(f"EMIT {event_type}")
            try:
                self.wfile.write(payload.encode())
                self.wfile.flush()
            except Exception as e:
                _log(f"EMIT FAILED {event_type}: {e}")
                raise

        emit("response.created", {
            "type": "response.created", "sequence_number": seq,
            "response": {"id": rid, "object": "response", "status": "in_progress", "output": []},
        })

        full_text = ""
        tool_buf = {}   # index -> {id, name, arguments}
        in_message = False

        _log("starting stream loop")
        try:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                ds = line[5:].strip()
                if ds == "[DONE]":
                    _log("got [DONE]")
                    break
                try:
                    chunk = json.loads(ds)
                except Exception:
                    _log(f"bad chunk JSON: {ds[:100]}")
                    continue

                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                tcs = delta.get("tool_calls") or []

                if content:
                    if not in_message:
                        in_message = True
                        emit("response.output_item.added", {
                            "type": "response.output_item.added", "sequence_number": seq,
                            "output_index": 0,
                            "item": {"type": "message", "id": msg_id, "status": "in_progress",
                                     "role": "assistant", "content": []},
                        })
                        emit("response.content_part.added", {
                            "type": "response.content_part.added", "sequence_number": seq,
                            "item_id": msg_id, "output_index": 0, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        })
                    full_text += content
                    emit("response.output_text.delta", {
                        "type": "response.output_text.delta", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "delta": content,
                    })

                for tc in tcs:
                    idx = tc.get("index", 0)
                    out_idx = idx + (1 if in_message else 0)
                    fn = tc.get("function", {})
                    if idx not in tool_buf:
                        tc_id = tc.get("id") or ("call_" + uuid.uuid4().hex[:8])
                        fn_name = fn.get("name", "")
                        fn_args = fn.get("arguments", "")
                        tool_buf[idx] = {"id": tc_id, "name": fn_name, "arguments": fn_args}
                        emit("response.output_item.added", {
                            "type": "response.output_item.added", "sequence_number": seq,
                            "output_index": out_idx,
                            "item": {
                                "type": "function_call", "id": tc_id, "call_id": tc_id,
                                "name": fn_name, "arguments": fn_args, "status": "in_progress",
                            },
                        })
                        if fn_args:
                            emit("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "sequence_number": seq,
                                "item_id": tc_id, "output_index": out_idx,
                                "delta": fn_args,
                            })
                    else:
                        if fn.get("name"):
                            tool_buf[idx]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tool_buf[idx]["arguments"] += fn["arguments"]
                            emit("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "sequence_number": seq,
                                "item_id": tool_buf[idx]["id"], "output_index": out_idx,
                                "delta": fn["arguments"],
                            })
        except Exception as _e:
            _log(f"stream loop exception: {_e}\n{traceback.format_exc()}")
        _log(f"stream loop done: in_message={in_message} tool_buf_len={len(tool_buf)} text_len={len(full_text)}")

        # Finalize text output
        if in_message:
            emit("response.output_text.done", {
                "type": "response.output_text.done", "sequence_number": seq,
                "item_id": msg_id, "output_index": 0, "content_index": 0, "text": full_text,
            })
            emit("response.content_part.done", {
                "type": "response.content_part.done", "sequence_number": seq,
                "item_id": msg_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": full_text, "annotations": []},
            })
            emit("response.output_item.done", {
                "type": "response.output_item.done", "sequence_number": seq,
                "output_index": 0,
                "item": {"type": "message", "id": msg_id, "status": "completed",
                         "role": "assistant",
                         "content": [{"type": "output_text", "text": full_text,
                                      "annotations": []}]},
            })

        # Finalize tool calls
        for idx, tc in tool_buf.items():
            out_idx = idx + (1 if in_message else 0)
            emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "sequence_number": seq,
                "item_id": tc["id"], "output_index": out_idx, "arguments": tc["arguments"],
            })
            emit("response.output_item.done", {
                "type": "response.output_item.done", "sequence_number": seq,
                "output_index": out_idx,
                "item": {
                    "type": "function_call", "id": tc["id"], "call_id": tc["id"],
                    "name": tc["name"], "arguments": tc["arguments"], "status": "completed",
                },
            })

        # Build final output list
        final_output = []
        if in_message:
            final_output.append({
                "type": "message", "id": msg_id, "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": full_text, "annotations": []}],
            })
        for idx, tc in tool_buf.items():
            final_output.append({
                "type": "function_call", "id": tc["id"], "call_id": tc["id"],
                "name": tc["name"], "arguments": tc["arguments"], "status": "completed",
            })

        final_response = {
            "id": rid, "object": "response", "status": "completed",
            "output": final_output,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        emit("response.completed", {
            "type": "response.completed", "sequence_number": seq,
            "response": final_response,
        })
        emit("response.done", {
            "type": "response.done", "sequence_number": seq,
            "response": final_response,
        })


if __name__ == "__main__":
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    print(port, flush=True)
    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.serve_forever()
