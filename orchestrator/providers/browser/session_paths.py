"""
Stable-root resolution for BrowserProvider's session data (chrome_data,
the CDP-launch registry file).

Bug this fixes (login-reuse regression):
    ``BrowserProvider.__init__`` used to resolve its session directory as::

        project_root = Path(
            os.environ.get("PROJECT_ROOT")
            or config.get("project_root", str(Path.cwd()))
        ).resolve()

    ``config.yaml``'s shipped default is ``project_root: "."``, and the
    ``PROJECT_ROOT`` env var is *also* set by both the Tauri desktop app and
    the `kim` CLI to mean "the target project the user is currently working
    on" (see ``desktop/src-tauri/src/task_spec.rs``: "PROJECT_ROOT for MCP
    tools (GUI: target project; bridge: kim root)"; `kim tui` sets it to
    wherever the terminal's cwd was). Resolving a relative/"." value or an
    ambient env var against ``Path.cwd()`` (or against whatever "target
    project" happens to be active) means the auto-launched Chrome's
    persistent profile silently moves to a NEW, never-logged-in directory
    every time Kim is launched from a different terminal, or pointed at a
    different target project in the GUI.  Confirmed on disk: seven separate
    ``sessions/chrome_data`` directories were found scattered across old
    checkout/test directories, each requiring its own separate login.

    The browser-automation login session is a per-INSTALL resource, not a
    per-target-project one — a user should never have to re-sign-in to
    claude.ai just because they cd'd into a different repo.  This module
    anchors it to the one stable Kim install instead: ``~/.kim_root`` (the
    marker ``install.sh`` writes, already trusted by the Rust CLI/desktop
    app's own ``paths.rs::resolve_project_root`` — see that file's docstring
    for the parallel, already-correct Rust-side resolution this mirrors),
    falling back to this package's own on-disk location.

    The ``PROJECT_ROOT`` env var is deliberately NOT consulted here (unlike
    ``orchestrator/mcp_client.py``'s file-tool sandboxing, which legitimately
    wants the per-launch target project). Only an explicit, ABSOLUTE
    ``project_root`` passed directly in the config dict is honored as an
    override — this keeps existing test-isolation and power-user usage
    (``BrowserProvider({"project_root": str(tmp_path), ...})``) working
    exactly as before.
"""
from __future__ import annotations

from pathlib import Path


def resolve_session_root(config: dict) -> Path:
    """Root directory for chrome_data / the CDP registry file.

    An explicit ABSOLUTE ``config["project_root"]`` wins (test isolation,
    deliberate override). Anything else — unset, or a relative value such as
    the shipped ``"."`` default — anchors to the stable Kim install rather
    than the launching process's cwd or the ambient ``PROJECT_ROOT`` env var.
    """
    raw = config.get("project_root")
    if raw:
        candidate = Path(str(raw))
        if candidate.is_absolute():
            return candidate.resolve()
    return stable_install_root()


def stable_install_root() -> Path:
    """The one stable Kim install directory for this machine.

    Precedence: ``~/.kim_root`` (written once by ``install.sh``, and already
    the source of truth the Rust side trusts) if it names a real directory,
    else this file's own on-disk location (``orchestrator/providers/browser/
    session_paths.py`` -> kim-pro/).
    """
    marker = Path.home() / ".kim_root"
    try:
        marked = marker.read_text(encoding="utf-8").strip()
    except OSError:
        marked = ""
    if marked:
        candidate = Path(marked)
        if candidate.is_dir():
            return candidate.resolve()
    # orchestrator/providers/browser/session_paths.py -> parents[3] == kim-pro/
    return Path(__file__).resolve().parents[3]
