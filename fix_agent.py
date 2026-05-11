path = "orchestrator/agent.py"
with open(path, "r") as f:
    text = f.read()

text = text.replace("except (OSError, IOError):", "except OSError:")

with open(path, "w") as f:
    f.write(text)
