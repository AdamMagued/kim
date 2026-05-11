import json
import re

text = """I'll search for Pong game files in the current directory and ensure the best version is working properly.

```json
{
  "text": "Searching for Pong game files in the current directory",
  "tool_calls": [
    {
      "name": "bash",
      "input": {
        "command": "ls -la /Users/adammaged/Desktop/Personal/pongTEST",
        "timeout": 30000
      }
    }
  ]
}
```
"""

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
                except Exception as e:
                    print("Failed parse:", candidate, str(e))
                start = -1
    return None

print(_extract_first_bridge_json(text))
