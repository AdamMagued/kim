# Kim Codex Bridge & ChatGPT Web Proxy Setup Guide

This guide allows you to run **Codex CLI / Codex Desktop** routed through **ChatGPT Web** (`chatgpt.com`) using `gpt-5.6-sol` (High Effort reasoning) with **100% single-thread statefulness** (no chat fragmentation).

---

## Instructions for AI Agents / Automated Setup

If an AI agent is setting this up for you, feed it this instruction:
> *"Follow the steps in `CODEX_BRIDGE_SETUP.md` to set up the Python venv, load the Chrome Extension, start the proxy server, and configure Codex CLI/Desktop."*

---

## Step 1: Environment Setup

Run the following commands in the repo root:

```bash
# 1. Create virtual environment
python3 -m venv venv

# 2. Activate and install dependencies
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 2: Install Chrome Extension

1. Open Chrome and go to `chrome://extensions`.
2. Turn on **Developer mode** (toggle in top right).
3. Click **Load unpacked**.
4. Select the `chrome_extension/` directory inside this repository.
5. Make sure you are logged into [chatgpt.com](https://chatgpt.com) in Chrome and keep a tab open.

---

## Step 3: Start the Kim Proxy Server

Run:

```bash
./scripts/start_codex_proxy.sh
```

You should see log lines indicating:
- `Codex proxy started on port 10532`
- `Extension WebSocket server running on ws://127.0.0.1:10533`
- `Chrome Extension connected!`
- `Chrome Extension bridge ready!`

---

## Step 4: Configure Codex CLI / Desktop

Point Codex to the local Kim proxy:

### For Codex CLI / Environment Variables
Add to your shell profile (`~/.zshrc` or `~/.bashrc`):
```bash
export OPENAI_BASE_URL="http://127.0.0.1:10532/v1"
export OPENAI_API_KEY="dummy"
```

### For Codex Desktop (`~/.codex/config.toml`)
```toml
[model_providers.kim_proxy]
type = "openai"
base_url = "http://127.0.0.1:10532/v1"
api_key = "dummy"

[profiles.default]
model_provider = "kim_proxy"
model = "gpt-5.6-sol"
```

---

## What This Fixes & Guarantees

1. **100% Single-Thread Continuity**: Multi-turn conversation turns (even across 20+ execution relays) stay locked to **ONE SINGLE ChatGPT Web thread** on `chatgpt.com`.
2. **Stateless Background Titles**: Intercepts Codex GUI background sidebar title requests so they never pollute or reset your active web chat.
3. **Delta Goal Re-injection**: Prevents context loss when long command outputs are relayed back to ChatGPT Web.
