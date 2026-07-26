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

The **first line** of output is the handshake, and it contains the proxy's
per-run bearer token:

```json
{"event": "ready", "port": 10532, "token": "<TOKEN>"}
```

Copy that `token` — Step 4 needs it. It is regenerated on every start, so
re-copy it whenever you restart the proxy.

> **Why a token?** The proxy drives your authenticated ChatGPT Web session.
> Without the check, any other process on your machine could send prompts
> through it. The proxy rejects requests that do not carry the exact token
> with `401 Unauthorized` — a placeholder like `"dummy"` will not work.

---

## Step 4: Configure Codex CLI / Desktop

Point Codex to the local Kim proxy, using the `token` printed in Step 3:

### For Codex CLI / Environment Variables
```bash
export OPENAI_BASE_URL="http://127.0.0.1:10532/v1"
export OPENAI_API_KEY="<TOKEN from the ready line>"
```

(Do not hard-code this in `~/.zshrc` — the token changes on every proxy start.)

### For Codex Desktop (`~/.codex/config.toml`)
```toml
[model_providers.kim_proxy]
type = "openai"
base_url = "http://127.0.0.1:10532/v1"
env_key = "CODEX_API_KEY"

[profiles.default]
model_provider = "kim_proxy"
model = "gpt-5.6-sol"
```

Then launch Codex Desktop with `CODEX_API_KEY=<TOKEN>` in its environment.

---

## What This Fixes & Guarantees

1. **100% Single-Thread Continuity**: Multi-turn conversation turns (even across 20+ execution relays) stay locked to **ONE SINGLE ChatGPT Web thread** on `chatgpt.com`.
2. **Stateless Background Titles**: Intercepts Codex GUI background sidebar title requests so they never pollute or reset your active web chat.
3. **Delta Goal Re-injection**: Prevents context loss when long command outputs are relayed back to ChatGPT Web.
