#!/bin/bash
# Kim launcher for Pop!_OS / Linux
# Fixes WebKit EGL/DMA-BUF crash on NVIDIA proprietary drivers

export WEBKIT_DISABLE_DMABUF_RENDERER=1
export WEBKIT_DISABLE_COMPOSITING_MODE=1

# Auto-detect DISPLAY if not set; honour a pre-existing value (#65).
if [ -z "$DISPLAY" ]; then
  export DISPLAY="${KIM_DISPLAY_FALLBACK:-:1}"
fi

# Source .env if present (for LLM API keys)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

exec "$SCRIPT_DIR/desktop/src-tauri/target/release/desktop" "$@"
