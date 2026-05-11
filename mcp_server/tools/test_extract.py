import json

def _extract_first_bridge_json(text: str):
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start: i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict) and ("text" in parsed or "tool_calls" in parsed):
                        return parsed
                except json.JSONDecodeError:
                    pass
                start = -1
    return None

text = """I'll analyze the Pong game implementations in your directory and polish the best one.
<tool_call>
{"text": "Checking what's in the current directory", "tool_calls": [{"name": "bash", "input": {"command": "ls -la /Users/adammaged/Desktop/Personal/pongTEST", "timeout": 5000}}]}
</tool_call>"""

print(_extract_first_bridge_json(text))
