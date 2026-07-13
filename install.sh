#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  Kim AI Agent — macOS / Linux Installer
#  Creates a virtual environment, installs all dependencies, and sets up
#  the .env configuration template.
#
#  Usage:  chmod +x install.sh && ./install.sh
# ─────────────────────────────────────────────────────────────────────────

set -e

echo ""
echo "  ╔═══════════════════════════════════════════════════════╗"
echo "  ║          Kim AI Agent — Setup (macOS / Linux)         ║"
echo "  ╚═══════════════════════════════════════════════════════╝"
echo ""

# ── Detect OS ───────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin) OS_NAME="macOS" ;;
    Linux)  OS_NAME="Linux" ;;
    *)      OS_NAME="$OS" ;;
esac
echo "  Detected OS: $OS_NAME ($(uname -m))"
echo ""

# ── Check Python ────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3 is not installed."
    if [ "$OS_NAME" = "macOS" ]; then
        echo "        Install with: brew install python3"
    else
        echo "        Install with: sudo apt install python3 python3-venv python3-pip"
    fi
    exit 1
fi

echo "  Python: $($PYTHON --version 2>&1)"
echo ""

# ── Check Python version (3.11+) ────────────────────────────────────────
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "[ERROR] Kim requires Python 3.11 or newer (found $($PYTHON --version 2>&1))."
    if [ "$OS_NAME" = "macOS" ]; then
        echo "        Upgrade with: brew install python@3.11 (or newer)"
    else
        echo "        Upgrade with: sudo apt install python3.11 python3.11-venv (or newer)"
    fi
    exit 1
fi

# ── Create virtual environment ──────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "[1/6] Creating virtual environment..."
    $PYTHON -m venv venv
    echo "      Done."
else
    echo "[1/6] Virtual environment already exists."
fi

# ── Activate venv ───────────────────────────────────────────────────────
echo "[2/6] Activating virtual environment..."
source venv/bin/activate
echo "      Done. ($(python --version))"

# ── Upgrade pip ─────────────────────────────────────────────────────────
echo "[3/6] Upgrading pip..."
pip install --upgrade pip --quiet
echo "      Done."

# ── Install dependencies ────────────────────────────────────────────────
echo "[4/6] Installing dependencies from requirements.txt..."
# set -e (line 10) aborts on failure, so no manual $? check is needed here
# (the old one was dead code — the script had already exited).
pip install -r requirements.txt --quiet
echo "      Done."

# ── Install Playwright browsers ─────────────────────────────────────────
echo "[5/6] Installing Playwright browsers (Chromium)..."
# Don't hide stderr: if this fails the user needs the real reason.
python -m playwright install chromium || {
    echo "      [WARN] Playwright browser install failed (see error above)."
    echo "             Install later with: python -m playwright install chromium"
}
echo "      Done."

# ── Set up .env ─────────────────────────────────────────────────────────
echo "[6/6] Setting up .env configuration..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "      Created .env from .env.example"
        echo "      IMPORTANT: Edit .env with your API keys before running Kim."
    else
        echo "      [WARN] .env.example not found. Create .env manually."
    fi
else
    echo "      .env already exists — skipping."
fi

# ── Create required directories ─────────────────────────────────────────
mkdir -p logs
mkdir -p sessions/chrome_data

# ── Write project root for .app bundle discovery ─────────────────────────
# Guard: only record a directory that actually looks like the Kim repo so a
# stray run from an unrelated directory can't poison Kim.app discovery.
if [ -f "orchestrator/agent.py" ]; then
    echo "$PWD" > "$HOME/.kim_root"
    echo "  Saved project root to ~/.kim_root (used by Kim.app)"
else
    echo "  [WARN] $PWD does not look like the Kim repo (no orchestrator/agent.py);"
    echo "         not writing ~/.kim_root."
fi

echo ""
echo "  ╔═══════════════════════════════════════════════════════╗"
echo "  ║          Setup complete!                              ║"
echo "  ╠═══════════════════════════════════════════════════════╣"
echo "  ║                                                       ║"
echo "  ║  Next steps:                                          ║"
echo "  ║                                                       ║"
echo "  ║  1. Edit .env with your API keys                      ║"
echo "  ║  2. Edit config.yaml to set your project_root         ║"
echo "  ║  3. Activate the venv:                                ║"
echo "  ║       source venv/bin/activate                        ║"
echo "  ║  4. Start the MCP server:                             ║"
echo "  ║       python -m mcp_server.server                     ║"
echo "  ║  5. Or run the agent:                                 ║"
echo "  ║       python -m orchestrator.agent --task \"...\"        ║"
echo "  ║  6. Register with Claude Code:                        ║"
echo "  ║       claude mcp add Kim -- python -m mcp_server.server║"
echo "  ║                                                       ║"
echo "  ╚═══════════════════════════════════════════════════════╝"
echo ""
