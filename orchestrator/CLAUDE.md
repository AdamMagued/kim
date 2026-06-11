# orchestrator/CLAUDE.md

## What lives here
The Python agent loop and all LLM provider adapters.

| File | Role |
|---|---|
| `agent.py` | Main async agent loop (`KimAgent`), system-prompt builders, tool dispatch |
| `providers/base.py` | Abstract `BaseProvider`; all providers must return `{"type": "tool_call"/"text", ...}` |
| `providers/{claude,openai_provider,gemini,deepseek,ollama,browser_provider}.py` | Provider impls |
| `session_store.py` | Reads/writes `kim_sessions/` JSONL trace files |
| `memory.py` | Conversation history + LLM-based compaction |
| `context_meter.py` | Token budget tracking (`ok` / `warn` / `critical`) |
| `interaction_policy.py` | Risk-gating for tool calls; `InteractionPolicy` |
| `tool_risk.py` | Risk tier classification per tool name |
| `ui_bridge.py` | Stdout event emitter (`UIBridge`) — see IPC protocol in `ARCHITECTURE.md` |
| `context_loader.py` | Loads per-directory CLAUDE.md and instruction files at startup |

## Local invariants
- **f-string prompts**: any `{example}` in a system prompt template inside an f-string must be `{{example}}` — a bare brace causes a `KeyError` mid-task. Tests in `tests/test_prompt_render.py` catch this.
- **`AgentTermination` enum** (`agent_states.py`): all run exits must go through it. Never `sys.exit()` from the loop.
- **Provider stdout**: providers print nothing to stdout — all user-visible output goes through `UIBridge`. stdout is the IPC channel to Rust.
- **`_call_with_retry`**: all LLM calls must use this wrapper (handles 429/529 backoff). Do not call provider `.complete()` directly in the loop.
- **Code tab provider constraint**: `find_code_backend()` must never resolve to OpenAI auth or gpt-5.5. There is a test for this in `tests/test_invariants.py`.

## How to add a provider
See `HOW_TO.md` → "Add a provider" (3 files to touch).

## How to test this layer
```bash
python -m pytest tests/test_agent*.py tests/test_providers*.py -v
python -m pytest tests/ -q  # full suite
```
