path = "orchestrator/providers/openai_provider.py"
with open(path, "r") as f:
    lines = f.readlines()

lines[72] = "            kwargs: dict = {\n"
lines[73] = '                "model": self._model,\n'
lines[74] = '                "messages": oai_messages,\n'
lines[75] = '                "max_completion_tokens": 8000,\n'
lines[76] = '            }\n'

with open(path, "w") as f:
    f.writelines(lines)
