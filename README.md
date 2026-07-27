# Kim — Local AI Agent Platform

Kim is a local AI agent platform that connects any cloud LLM (Claude, Gemini, GPT-4o, Ollama) to full OS control — screen vision, mouse/keyboard, file system, browser automation, and shell execution. It is the personal equivalent of Claude Code + Computer Use, running locally, controlled by you.

![Kim chat interface](docs/assets/screenshot.png)

> **Status:** Active development — macOS first-class, Windows/Linux beta. See [Architecture](ARCHITECTURE.md) for the full design.

---

## Features

- **Multi-provider:** Claude, Gemini (OAuth), OpenAI, DeepSeek, Ollama local, or drive any LLM via an open browser tab (no API key needed)
- **OS control:** Take screenshots, click, type, scroll, run shell commands, read/write files, manage windows
- **Browser automation:** Fill forms, navigate pages, extract structured data from the DOM
- **Code workspace:** Integrated code agent (Code tab) powered by Claw/Codex with browser-provider backend, running on `kimcli` — Kim's rebranded, pinned build of codex-cli 0.144.3 ([docs/kimcli.md](docs/kimcli.md))
- **MCP server:** 50+ OS-control tools exposed via the Model Context Protocol — usable from Claude Code or any MCP client
- **Session history:** Every run is saved as a JSONL trace in `kim_sessions/`

---

## Install (development)

### Prerequisites

- macOS 13+ (arm64 or x64) — **primary platform**
- Python 3.11+
- Node.js 20+ (LTS)
- Rust stable (`rustup install stable`)

### 1. Clone and set up Python

**Quick path** — the install script does all of this step for you (creates `venv/`, installs Python deps + Playwright Chromium, seeds `.env` from the template, and records the project root in `~/.kim_root` for the packaged app):

```bash
git clone https://github.com/AdamMagued/kim.git
cd kim/kim-pro
./install.sh          # macOS / Linux
# install.bat         # Windows
```

**Manual path** — the same, by hand:

```bash
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

---

## Quickstart: Codex CLI / Desktop Proxy (3 Easy Steps)

Run Codex CLI or Codex Desktop GUI routed through Kim Engine with live Chrome Extension streaming:

### 1. Install Dependencies
```bash
git clone https://github.com/AdamMagued/kim.git
cd kim/kim-pro
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

### 2. Start the Proxy Server
```bash
./scripts/start_codex_proxy.sh
```
*(Runs the proxy on `http://127.0.0.1:10532/v1` connected to the Chrome Extension on `ws://127.0.0.1:10533`)*

### 3. Run Codex CLI
In your project directory:
```bash
OPENAI_BASE_URL="http://127.0.0.1:10532/v1" codex --model gpt-5.6-sol
```
*(Or point Codex Desktop GUI's base URL to `http://127.0.0.1:10532/v1`)*
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
OPENAI_API_KEY=sk-...
```

Beyond the provider API keys, Kim's behavior knobs are `KIM_*` environment
variables (log level, tool tiers, HITL gates, browser automation, offline fake
mode, …) — see [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for the generated
reference of every variable.

### 3. Run the desktop app

```bash
cd desktop
npm install
npm run tauri dev
```

The Tauri window opens. Select a provider in Settings → AI, type a task, and press Enter.

### 4. Run tests

All four test suites (the `cli/` crate is separate — easy to forget):

```bash
# Python
python -m pytest tests/ -q

# Frontend (Vitest + type check)
cd desktop && npm run test && npx tsc --noEmit

# Rust (desktop)
cd desktop/src-tauri && cargo test

# Rust (kim CLI)
cd cli && cargo test
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
MCP server (50+ OS tools)
```

---

## How-to recipes

See [HOW_TO.md](HOW_TO.md) for minimal file sets to:
- Add an MCP tool (3 files)
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

## Troubleshooting

**Tasks fail instantly with `ModuleNotFoundError` (e.g. `No module named 'anthropic'`) — missing or broken venv.**
The desktop app resolves a Python interpreter in this order: bundled sidecar → `~/.kim/venv` → project `venv/`/`.venv/` → bare system `python3`. If the project venv is missing or broken, Kim silently falls back to your system Python, which does not have Kim's packages. Current builds run a dependency preflight on that fallback and show "Kim's Python dependencies are not installed…" instead of spawning; if you see either that message or a raw `ModuleNotFoundError` in the task stream, the fix is the same — create the venv:

```bash
./install.sh
# or manually:
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

**Where are the logs?** Settings → Feedback → "Reveal logs", or `logs/kim_YYYY-MM-DD.jsonl` (structured JSONL, 7-day retention).

---

## License

Licensed under the [MIT License](LICENSE) — see the `LICENSE` file at the repo root for the full text.

---

## Contributing

Contributions are welcome under the MIT License. A `CONTRIBUTING.md` with the
full workflow is still TODO; until then, follow the test-and-CI gate described
above (all four suites green + remote CI green before merge).
