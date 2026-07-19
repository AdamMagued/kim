# KimCLI

KimCLI is the terminal version of Kim: a standalone chat and coding interface
that can talk to local/API models and, when Kim desktop is running, browser
providers such as Claude, ChatGPT, and Gemini.

## Install

### Option A — pre-built binary (recommended for most users)

Download a binary from the [latest GitHub Release](https://github.com/AdamMagued/kim/releases/latest):

| Platform | File |
|---|---|
| macOS Apple Silicon | `kim-cli-<version>-macos-aarch64` |
| macOS Intel | `kim-cli-<version>-macos-x86_64` |
| Linux x86-64 | `kim-cli-<version>-linux-x86_64` |
| Windows x86-64 | `kim-cli-<version>-windows-x86_64.exe` |

After downloading:

```sh
# macOS / Linux
chmod +x kim-cli-*-<platform>
mv kim-cli-*-<platform> ~/.local/bin/kim

# macOS: clear quarantine on first run
xattr -dr com.apple.quarantine ~/.local/bin/kim
```

Binary artifacts are produced by the release workflow for every tagged release
(`v*`) and every manual workflow dispatch. Tags produce GitHub Release assets;
dispatches produce downloadable workflow artifacts (7-day retention) useful for
testing pre-release builds.

**Binary-only capabilities:**
- Direct API chat and code mode: Claude, OpenAI, Gemini, DeepSeek (API key required).
- Ollama chat and code mode: fully local, no key, no source checkout needed.

**Limitation — browser-backed code mode:**
The binary does not bundle the Kim Python orchestrator. Browser-backed code mode
(`/login browser:*` then `/code`) runs `python3 -m orchestrator.codex_bridge_service`,
which requires a full Kim source checkout. Without one, `kim doctor` will report
`Source root: not set`.

To enable browser-backed code mode with a binary install, choose one of:

- **Source installer (recommended):** run Option B once — it clones the repo, writes
  `~/.kim_root`, and does not remove the separately downloaded binary.
- **Manual checkout:** clone and point `~/.kim_root` at the clone:
  ```sh
  git clone --depth 1 https://github.com/AdamMagued/kim.git ~/.kim/source
  echo ~/.kim/source > ~/.kim_root
  ```
- **Per-session env var:** `export KIM_PROJECT_ROOT=/path/to/kim-checkout`

Chat mode with browser providers (Kim desktop bridge, no API key) works without a
source root.

### Option B — build from source (requires Rust)

From a local checkout:

```sh
./cli/install.sh
```

One-line remote install (clones the repo, builds, and installs):

```sh
curl -fsSL https://raw.githubusercontent.com/AdamMagued/kim/main/cli/install.sh | bash
```

The installer runs `cargo build --manifest-path cli/Cargo.toml --release`,
copies the binary to `~/.local/bin/kim`, and writes `~/.kim_root` so KimCLI
can find the Python orchestrator when launched outside the repo.

**Prerequisite:** `cargo` must be installed
([rustup.rs](https://rustup.rs)).

Environment overrides for remote installs:

```sh
KIM_REPO_URL=https://github.com/AdamMagued/kim.git \
KIM_INSTALL_BRANCH=my-branch \
bash <(curl -fsSL ...)
```

On subsequent runs the installer does a shallow fetch and hard-resets to the
branch tip (local changes in `~/.kim/source` will be overwritten).

## After installing

```sh
kim doctor
```

Checks the Kim source root, Python, Codex, Git, Cargo, Ollama, and the Kim
desktop bridge when you choose `desktop` or `browser:*` providers.

## Quick Start

```text
kim chat "explain this"  one-shot chat prompt
kim code "fix the bug"   one-shot coding-agent prompt
/doctor                check install/provider readiness inside Kim
kim doctor             same check from your normal shell
/chat                  switch to chat mode
/code                  switch to code mode
/login ollama          local models, no API key
/login browser:chatgpt  ChatGPT through Kim desktop, no API key
/login browser:gemini   Gemini through Kim desktop, no API key
/login browser:deepseek DeepSeek through Kim desktop, no API key
/provider              list providers
/status                show current mode/provider/config
```

Browser providers require the Kim desktop app to be running because KimCLI uses
the desktop browser bridge for signed-in web sessions. Chat mode routes through
the desktop `/v1/task` bridge. Code mode with a browser provider routes through
Kim's browser Codex bridge.

## Product Target

KimCLI should feel like a first-class terminal agent:

- Chat mode for normal assistant conversations.
- Code mode for repo-aware coding with Codex-style execution.
- Keyless browser providers when desktop Kim is running.
- Local Ollama support for fully local/free usage.
- One-command install with a normal `kim` executable on PATH.

## Session formats (C2)

KimCLI discovers sessions from two stores with **different formats**:

1. **CLI sessions** — `~/.kim/sessions/<id>.jsonl`, written by KimCLI itself.
   Each line is `{"type":"message","role","content","timestamp_ms"}`. These
   resume fully: the whole user/assistant transcript is restored.
2. **Orchestrator traces** — `kim_sessions/<date>/<id>.jsonl` (and in-repo dirs),
   written by the desktop/orchestrator agent. These are richer trace records
   (tool calls, screenshots, plan steps). When resumed in the CLI, only the
   displayable text is recovered — **tool context is not resumable**, so the
   CLI labels such a resume as a read-only transcript.

The session picker scans both stores plus the current directory's `.kim/`. If a
resumed session looks like an orchestrator trace, treat it as a read-only
reference, not a fully continuable conversation.
