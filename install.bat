@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  Kim AI Agent — Windows Installer
REM  Creates a virtual environment, installs all dependencies, and sets up
REM  the .env configuration template.
REM
REM  Usage:  install.bat
REM ─────────────────────────────────────────────────────────────────────────

echo.
echo  ╔═══════════════════════════════════════════════════════╗
echo  ║           Kim AI Agent — Setup (Windows)              ║
echo  ╚═══════════════════════════════════════════════════════╝
echo.

REM ── Check Python ──────────────────────────────────────────────────────
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Download from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

python --version
echo.

REM ── Create virtual environment ────────────────────────────────────────
if not exist "venv" (
    echo [1/6] Creating virtual environment...
    python -m venv venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo       Done.
) else (
    echo [1/6] Virtual environment already exists.
)

REM ── Activate venv ─────────────────────────────────────────────────────
echo [2/6] Activating virtual environment...
call venv\Scripts\activate.bat
echo       Done.

REM ── Upgrade pip ───────────────────────────────────────────────────────
echo [3/6] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo       Done.

REM ── Install dependencies ──────────────────────────────────────────────
echo [4/6] Installing dependencies from requirements.txt...
pip install -r requirements.txt --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Dependency installation failed. Check requirements.txt.
    pause
    exit /b 1
)
echo       Done.

REM ── Install Playwright browsers ──────────────────────────────────────
echo [5/6] Installing Playwright browsers (Chromium)...
python -m playwright install chromium --quiet 2>nul
if %ERRORLEVEL% neq 0 (
    echo       [WARN] Playwright browser install failed. Browser provider may not work.
    echo              You can install later with: python -m playwright install chromium
)
echo       Done.

REM ── Set up .env ───────────────────────────────────────────────────────
echo [6/6] Setting up .env configuration...
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo       Created .env from .env.example
        echo       IMPORTANT: Edit .env with your API keys before running Kim.
    ) else (
        echo       [WARN] .env.example not found. Create .env manually.
    )
) else (
    echo       .env already exists — skipping.
)

REM ── Create required directories ───────────────────────────────────────
if not exist "logs" mkdir logs
if not exist "sessions\chrome_data" mkdir sessions\chrome_data

echo.
echo  ╔═══════════════════════════════════════════════════════╗
echo  ║           Setup complete!                             ║
echo  ╠═══════════════════════════════════════════════════════╣
echo  ║                                                       ║
echo  ║  Next steps:                                          ║
echo  ║                                                       ║
echo  ║  1. Edit .env with your API keys                      ║
echo  ║  2. Edit config.yaml to set your project_root         ║
echo  ║  3. Start the MCP server:                             ║
echo  ║       python -m mcp_server.server                     ║
echo  ║  4. Or run the agent:                                 ║
echo  ║       python -m orchestrator.agent --task "..."        ║
echo  ║  5. Register with Claude Code:                        ║
echo  ║       claude mcp add Kim -- python -m mcp_server.server║
echo  ║                                                       ║
echo  ╚═══════════════════════════════════════════════════════╝
echo.

pause
