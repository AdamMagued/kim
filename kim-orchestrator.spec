# kim-orchestrator.spec — PyInstaller build specification
#
# Bundles orchestrator/ + mcp_server/ into a single executable sidecar
# for the Tauri desktop app.
#
# Usage (from repo root):
#   pip install pyinstaller
#   pyinstaller kim-orchestrator.spec
#
# Output: dist/kim-orchestrator (macOS/Linux) or dist/kim-orchestrator.exe (Windows)
#
# After building, register the sidecar in desktop/src-tauri/tauri.conf.json
# by adding "externalBin": ["../dist/kim-orchestrator"] to the "bundle" object.
# Tauri auto-appends the platform triple at bundle time so you only list the
# bare name here.
#
# TODO(human): Code-signing required for notarization before Mac App Store / Gatekeeper
# distribution. Sign with: codesign --deep --force --sign "<cert>" dist/kim-orchestrator
#
# The Tauri sidecar mechanism auto-appends the platform target triple at
# build time (e.g. kim-orchestrator-aarch64-apple-darwin). Rename accordingly:
#   mv dist/kim-orchestrator dist/kim-orchestrator-aarch64-apple-darwin   # macOS ARM
#   mv dist/kim-orchestrator dist/kim-orchestrator-x86_64-apple-darwin    # macOS Intel
#   mv dist/kim-orchestrator dist/kim-orchestrator-x86_64-unknown-linux-gnu # Linux
#   mv dist/kim-orchestrator.exe dist/kim-orchestrator-x86_64-pc-windows-msvc.exe # Windows

import sys
from pathlib import Path

block_cipher = None

# ── Repo layout ───────────────────────────────────────────────────────────────
# Spec file lives at the repo root. Everything is relative to here.
REPO_ROOT = Path(SPECPATH)

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    # Entry point: orchestrator/agent.py (same module invoked with -m orchestrator.agent)
    scripts=[str(REPO_ROOT / 'orchestrator' / 'agent.py')],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[
        # Preserve mcp_server tool registry (scanned at runtime for tool definitions)
        (str(REPO_ROOT / 'mcp_server'), 'mcp_server'),
        # Include orchestrator package so relative imports work inside the frozen app
        (str(REPO_ROOT / 'orchestrator'), 'orchestrator'),
        # Default config file so the agent can find its defaults when no config.yaml
        # exists in the installation directory.
        (str(REPO_ROOT / 'config.yaml.example'), '.'),
    ],
    hiddenimports=[
        # Providers loaded dynamically via create_provider() string dispatch
        'orchestrator.providers.claude',
        'orchestrator.providers.openai_provider',
        'orchestrator.providers.gemini',
        'orchestrator.providers.deepseek',
        'orchestrator.providers.ollama',
        'orchestrator.providers.browser_provider',
        'orchestrator.providers.fake',
        # MCP server modules imported at runtime
        'mcp_server.tool_registry',
        'mcp_server.logger',
        # Standard library modules sometimes missed by PyInstaller's static analysis
        'asyncio',
        'json',
        'logging',
        'pathlib',
        'ssl',
        'urllib',
        'urllib.request',
        'urllib.parse',
        'http',
        'http.client',
        # Optional heavy deps — gracefully absent in slim builds.
        # These are guarded by try/except in the orchestrator, so omitting them
        # only disables those features (voice, screenshot, etc.) in the bundle.
        # Uncomment and install the packages if you want them included:
        # 'mss', 'pyautogui', 'PIL', 'pynput', 'sounddevice', 'kokoro',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude large deps that are not needed at runtime
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'pytest',
        'unittest',
        'doctest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='kim-orchestrator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX compression — disable for faster startup
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # Must be True: the orchestrator communicates via stdout/stderr
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,    # None = current arch; set 'universal2' for Mac fat binary
    codesign_identity=None,   # TODO(human): set to your Developer ID for notarization
    entitlements_file=None,   # TODO(human): add entitlements for hardened runtime
    onefile=True,        # Single self-contained binary — required for Tauri sidecar
)
