# Kim development task runner
# Install: brew install just (macOS) / cargo install just
# Usage: just check | just test | just test-web | just dev

# Default — show available recipes
default:
    @just --list

# Fast feedback loop: type-check + rust check + pytest (no slow tests)
# Target: <30 seconds total
check:
    #!/usr/bin/env bash
    set -e
    echo "=== TypeScript ==="
    (cd desktop && npx tsc --noEmit) &
    TSC_PID=$!

    echo "=== Rust ==="
    (cd desktop/src-tauri && cargo check 2>&1) &
    CARGO_PID=$!

    echo "=== Python (fast) ==="
    source venv/bin/activate && python -m pytest tests/ -q -m "not slow" --tb=short &
    PYTEST_PID=$!

    wait $TSC_PID && wait $CARGO_PID && wait $PYTEST_PID
    echo "=== check: all green ==="

# Full test suite: all three languages
test:
    #!/usr/bin/env bash
    set -e
    source venv/bin/activate
    echo "=== Python ==="
    python -m pytest tests/ -q --tb=short
    echo "=== TypeScript ==="
    cd desktop && npm run test && npx tsc --noEmit
    echo "=== Rust ==="
    cd src-tauri && cargo test
    echo "=== CLI Rust ==="
    cd ../../cli && cargo test -- --test-threads=1

# Web automation evals only
test-web:
    #!/usr/bin/env bash
    set -e
    source venv/bin/activate
    python -m pytest tests/evals/ -v --tb=short

# Python tests only
test-py:
    #!/usr/bin/env bash
    set -e
    source venv/bin/activate
    python -m pytest tests/ -q --tb=short

# Offline end-to-end fake run (no API keys, no browser, no spend)
fake task="open Notepad and say hello":
    #!/usr/bin/env bash
    set -e
    source venv/bin/activate
    KIM_FAKE=1 python -m orchestrator.agent --task "{{task}}"

# Launch dev environment (Tauri hot-reload + Python venv reminder)
dev:
    #!/usr/bin/env bash
    echo "Note: after any Rust change, restart this process (Rust is not hot-reloaded)"
    source venv/bin/activate
    cd desktop && npm run tauri dev
