# Kim — Local AI Agent Platform

Kim is a local AI agent platform that connects any cloud LLM (Claude, Gemini, GPT-4o, Ollama) to full OS control — screen vision, mouse/keyboard, file system, browser automation, and shell execution. It is the personal equivalent of Claude Code + Computer Use, running locally, controlled by you.

![Kim chat interface](docs/assets/screenshot.png)

> **Status:** Active development — macOS first-class, Windows/Linux beta. See [Architecture](ARCHITECTURE.md) for the full design.

---

## Features

- **Multi-provider:** Claude, Gemini (OAuth), OpenAI, DeepSeek, Ollama local, or drive any LLM via an open browser tab (no API key needed)
- **OS control:** Take screenshots, click, type, scroll, run shell commands, read/write files, manage windows
- **Browser automation:** Fill forms, navigate pages, extract structured data from the DOM
- **Code workspace:** Integrated code agent (Code tab) powered by Claw/Codex with browser-provider backend
- **MCP server:** 31 OS-control tools exposed via the Model Context Protocol — usable from Claude Code or any MCP client
- **Session history:** Every run is saved as a JSONL trace in `kim_sessions/`

---

## Install (development)

### Prerequisites

- macOS 13+ (arm64 or x64) — **primary platform**
- Python 3.11+
- Node.js 20+ (LTS)
- Rust stable (`rustup install stable`)

### 1. Clone and set up Python

```bash
git clone https://github.com/AdamMagued/kim.git
cd kim/kim-pro
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure providers

```bash
cp config.yaml.example config.yaml
# Edit config.yaml: set provider, paste API keys, adjust paths
```

Or set environment variables in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
OPENAI_API_KEY=sk-...
```

### 3. Run the desktop app

```bash
cd desktop
npm install
npm run tauri dev
```

The Tauri window opens. Select a provider in Settings → AI, type a task, and press Enter.

### 4. Run tests

```bash
# Python (816+ tests)
python -m pytest tests/ -q

# Frontend (31 Vitest tests)
cd desktop && npm run test && npx tsc --noEmit

# Rust (50 tests)
cd desktop/src-tauri && cargo test
```

Or with `just` (if installed: `brew install just`):

```bash
just check   # parallel tsc + cargo check + pytest, <30s
just test    # full suite
```

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full layer diagram, IPC protocol spec, and design decisions.

**Quick summary:**

```
React UI (Tauri webview)
    ↕ Tauri commands / events
Rust backend (lib.rs)
    ↕ stdin/stdout IPC
Python orchestrator (agent.py)
    ↕ MCP stdio
MCP server (31 OS tools)
```

---

## How-to recipes

See [HOW_TO.md](HOW_TO.md) for minimal file sets to:
- Add an MCP tool (4 files)
- Add a provider (3 files)
- Add a settings pane (3 files)
- Run a targeted test pass

Quality campaign: [docs/OPERATION_GOOGLE_LEVEL.md](docs/OPERATION_GOOGLE_LEVEL.md) (plan) · [docs/ops/](docs/ops/) (findings, triage, baseline).

---

## Provider setup

| Provider | Setup |
|---|---|
| **Claude** | Set `ANTHROPIC_API_KEY` in `.env`; select "claude" in Settings → AI |
| **Gemini** | Click "Sign in with Google" in Settings → AI (PKCE OAuth, no key needed) |
| **Ollama** | Run `ollama serve`; select "ollama" and pick a model |
| **Browser** | Open Chrome with `--remote-debugging-port=9222`; select "browser:claude" etc. |
| **OpenAI** | Set `OPENAI_API_KEY`; select "openai" |

---

## Privacy

Kim runs entirely locally. Nothing leaves your machine except:
- LLM API calls (task text + screenshots sent to your chosen provider)
- Screenshots in `kim_sessions/` (screenshot payloads are stripped from sessions older than 2 days; whole sessions are deleted after 30 days; export or back up your data from Settings → Data)

No telemetry, no accounts, no cloud storage.

---

## License

Licensed under the [MIT License](LICENSE) — see the `LICENSE` file at the repo root for the full text.

---

## Contributing

Contributions are welcome under the MIT License. A `CONTRIBUTING.md` with the
full workflow is still TODO; until then, follow the test-and-CI gate described
above (all four suites green + remote CI green before merge).
