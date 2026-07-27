#!/bin/bash
# Kim Engine → Codex CLI Proxy Launcher
# Launches Kim's Python Standalone Codex Engine connected to Chrome Extension bridge
#
# Usage: ./scripts/start_codex_proxy.sh
# Run from the repo root, or it auto-detects the repo root.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KIM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$KIM_DIR/venv/bin/python"

if [ ! -f "$PYTHON" ]; then
  echo "[error] Python venv not found at $PYTHON"
  echo "        Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          Kim Engine → Codex CLI Proxy Launcher          ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Starting Kim Standalone Codex Engine (gpt-5.6-sol high) ║"
echo "║  Connecting to Chrome Extension on ws://127.0.0.1:10533   ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

export KIM_PREFERRED_SITE="chatgpt:gpt-5.6-sol"
export KIM_EFFORT="high"
# Opt this deployment into the Chrome Extension WebSocket bridge. Without it
# BrowserProvider uses its normal CDP/playwright path (see _use_webview_bridge).
export KIM_EXTENSION_BRIDGE="1"
export KIM_ALLOW_DUMMY_AUTH="1"

cd "$KIM_DIR"
exec "$PYTHON" -m codex_engine.standalone_proxy --provider browser:chatgpt:gpt-5.6-sol
