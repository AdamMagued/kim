import json

def test():
    text = """I'll search for Pong game files in the current directory and ensure the best version is working properly.

json
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
}"""
    print("Test passed.")

test()
