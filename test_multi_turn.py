import urllib.request
import json
import time

URL = "http://127.0.0.1:10532/v1/responses"
HEADERS = {"Content-Type": "application/json"}

def send_request(input_items, turn_name):
    payload = {
        "model": "kim-proxy-model",
        "stream": True,
        "input": input_items
    }
    
    print(f"=== STARTING {turn_name} ===")
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"), headers=HEADERS)
    
    reasoning_deltas = []
    final_reasoning = ""
    output_deltas = []
    final_output = ""
    completed_response = None

    with urllib.request.urlopen(req) as resp:
        for line_bytes in resp:
            line = line_bytes.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            
            try:
                evt = json.loads(data_str)
            except Exception:
                continue
            
            etype = evt.get("type")
            if etype == "response.reasoning.text.delta":
                delta = evt.get("delta", "")
                reasoning_deltas.append(delta)
            elif etype == "response.output_text.delta":
                delta = evt.get("delta", "")
                output_deltas.append(delta)
            elif etype == "response.completed":
                completed_response = evt.get("response", {})

    if completed_response:
        for item in completed_response.get("output", []):
            if item.get("type") == "reasoning":
                final_reasoning = item.get("reasoning_text", "")
            elif item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        final_output += c.get("text", "")

    result = {
        "turn": turn_name,
        "reasoning_deltas": reasoning_deltas,
        "final_reasoning": final_reasoning,
        "output_deltas": output_deltas,
        "final_output": final_output,
        "completed_response": completed_response
    }
    return result

# Turn 1
input_turn1 = [
    {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
]
res1 = send_request(input_turn1, "Turn 1")

# Turn 2 (including history)
input_turn2 = [
    {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {
        "role": "assistant",
        "content": [{"type": "output_text", "text": res1["final_output"]}]
    },
    {"role": "user", "content": [{"type": "input_text", "text": "how is the weather today?"}]}
]
res2 = send_request(input_turn2, "Turn 2")

summary = {
    "turn1": res1,
    "turn2": res2
}

with open("test_results.json", "w") as f:
    json.dump(summary, f, indent=2)

print("\n=== TEST COMPLETED SUCCESSFULLY ===")
