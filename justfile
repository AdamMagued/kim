# Kim development task runner
# Install: brew install just (macOS) / cargo install just
# Usage: just check | just test | just test-web | just dev

# Default — show available recipes
default:
    @just --list

# First-time setup: venv, dependencies, app wiring (runs install.sh)
setup:
    ./install.sh

# Development check: type-check + Rust checks + the current full Python test set
check:
    #!/usr/bin/env bash
    set -e
    echo "=== TypeScript ==="
    (cd desktop && npx tsc --noEmit) &
    TSC_PID=$!

    echo "=== Rust ==="
    (cargo check -p desktop 2>&1) &
    CARGO_PID=$!

    echo "=== Rust (CLI) ==="
    (cargo check -p kim-cli 2>&1) &
    CLI_PID=$!

    echo "=== Python ==="
    source venv/bin/activate && python -m pytest tests/ -q --tb=short &
    PYTEST_PID=$!

    echo "=== pyright ==="
    pyright --outputjson | python3 -c "
import sys, json
d = json.load(sys.stdin)
errs = [e for e in d.get('generalDiagnostics', []) if e['severity'] == 'error']
if errs:
    for e in errs:
        print(e['file'] + ':' + str(e['range']['start']['line']+1) + ': ' + e['message'])
    sys.exit(1)
print('pyright: 0 errors')
" &
    PYRIGHT_PID=$!

    wait $TSC_PID && wait $CARGO_PID && wait $CLI_PID && wait $PYTEST_PID && wait $PYRIGHT_PID
    echo "=== check: all green ==="

# pyright type check only
typecheck:
    #!/usr/bin/env bash
    set -e
    pyright

# Full test suite: all three languages
test:
    #!/usr/bin/env bash
    set -e
    source venv/bin/activate
    echo "=== Python ==="
    python -m pytest tests/ -q --tb=short
    echo "=== TypeScript ==="
    cd desktop && npm run test && npx tsc --noEmit
    cd ..
    echo "=== Rust ==="
    cargo test -p desktop
    echo "=== CLI Rust ==="
    # --test-threads=1 is required (#56 F-K-12): kim-cli tests name temp dirs
    # by SystemTime nanos alone (unique_config_path, cli/src/config.rs:213;
    # same pattern in sessions.rs/provider.rs/file_refs.rs). SystemTime has
    # ~1us resolution on macOS, so parallel tests can share a dir and one
    # test's remove_dir_all deletes another's dir mid-save. Measured
    # 2026-07-13: parallel failed 1/3 full runs + 2/10 config stress runs;
    # serial passed 3/3. Full details in .github/workflows/ci.yml (linux-cli).
    cargo test -p kim-cli -- --test-threads=1

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
