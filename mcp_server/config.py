import os
import sys
import logging
from pathlib import Path
import yaml
from dotenv import load_dotenv

# Ensure the project root is on sys.path so `mcp_server` is importable
# regardless of how the process was launched.
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

load_dotenv()

logger = logging.getLogger(__name__)

_CONFIG_PATH = _PROJECT_DIR / "config.yaml"


def _load_yaml() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg = _load_yaml()

# Resolve project_root relative to config.yaml's directory — NOT cwd.
# This makes project_root: "." work correctly regardless of launch dir.
_raw_root = os.environ.get("PROJECT_ROOT") or _cfg.get("project_root", str(_PROJECT_DIR))
_root_path = Path(_raw_root)
if not _root_path.is_absolute():
    _root_path = _PROJECT_DIR / _root_path
PROJECT_ROOT = _root_path.resolve()

# Resolve allowed_paths the same way (with ~ expansion)
_raw_allowed = _cfg.get("allowed_paths", [str(PROJECT_ROOT)])
ALLOWED_PATHS = []
for p in _raw_allowed:
    pp = Path(p).expanduser()
    if not pp.is_absolute():
        pp = _PROJECT_DIR / pp
    resolved_pp = pp.resolve()
    # #3: Warn when ~ grants access to entire home directory
    if p.strip() == "~" or p.strip() == "~/":
        logger.warning(
            "⚠ '~' in allowed_paths grants access to entire home directory; "
            "consider scoping to '~/Projects' or '.' (project root only)."
        )
    ALLOWED_PATHS.append(resolved_pp)
if PROJECT_ROOT not in ALLOWED_PATHS:
    ALLOWED_PATHS.append(PROJECT_ROOT)

BLOCKED_COMMANDS: list[str] = _cfg.get("shell", {}).get("blocked_commands", [
    "rm -rf /", "format c:", "del /S /Q C:\\", "rd /S /Q C:\\"
])

SHELL_TIMEOUT: int = int(_cfg.get("shell", {}).get("timeout", 30))
CODE_TIMEOUT: int = int(_cfg.get("code_timeout", 30))
PREVIEW_MODE: bool = bool(_cfg.get("preview_mode", False))
LOG_LEVEL: str = _cfg.get("logging", {}).get("level", "INFO")
BROWSER_HEADLESS: bool = bool(
    _cfg.get("browser_provider", {}).get("browser_headless", False)
)
VOICE_ENABLED: bool = bool(_cfg.get("voice_enabled", False))

# ── Sensitive path deny list (#3) ─────────────────────────────────────────────
# Even when a path falls within ALLOWED_PATHS, these directories are always
# off-limits to prevent accidental credential/key exposure.

_HOME = Path.home()
_SENSITIVE_PATHS: list[Path] = [
    _HOME / ".ssh",
    _HOME / ".gnupg",
    _HOME / ".aws",
    _HOME / ".kube",
    _HOME / ".docker",
    _HOME / ".netrc",
    _HOME / ".config" / "gh",
    # macOS-specific
    _HOME / "Library" / "Keychains",
]
# Also deny any dotenv files in home
_SENSITIVE_GLOBS = [".env", ".env.*"]


def validate_path(path_str: str) -> Path:
    """
    Resolve path_str relative to PROJECT_ROOT and verify it stays within
    an allowed root. Raises PermissionError if outside allowed paths or
    inside a sensitive directory.
    """
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()

    # Check against allowed paths
    allowed = False
    for ap in ALLOWED_PATHS:
        try:
            p.relative_to(ap)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise PermissionError(
            f"Path '{p}' is outside allowed directories: {[str(a) for a in ALLOWED_PATHS]}"
        )

    # Check against sensitive path deny list
    for sensitive in _SENSITIVE_PATHS:
        try:
            p.relative_to(sensitive)
            raise PermissionError(
                f"Path '{p}' is inside sensitive directory '{sensitive}' — access denied"
            )
        except ValueError:
            continue

    # Check for dotenv files in home directory
    if p.parent == _HOME and p.name.startswith(".env"):
        raise PermissionError(
            f"Path '{p}' is a dotenv file in the home directory — access denied"
        )

    return p


def get_config() -> dict:
    return _cfg

