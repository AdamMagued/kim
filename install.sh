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
pip install -r requirements.txt --quiet
if [ $? -ne 0 ]; then
    echo "[ERROR] Dependency installation failed."
    exit 1
fi
echo "      Done."

# ── Install Playwright browsers ─────────────────────────────────────────
echo "[5/6] Installing Playwright browsers (Chromium)..."
python -m playwright install chromium 2>/dev/null || {
    echo "      [WARN] Playwright browser install failed."
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
echo "$PWD" > "$HOME/.kim_root"
echo "  Saved project root to ~/.kim_root (used by Kim.app)"

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
