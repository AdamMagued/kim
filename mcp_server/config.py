import os
import sys
import fnmatch
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

SHELL_TIMEOUT: int = int(_cfg.get("shell", {}).get("timeout", 30))
_shell_sandbox_env = os.environ.get("KIM_SHELL_SANDBOX_MODE")
if _shell_sandbox_env is None:
    SHELL_SANDBOX_MODE: bool = bool(_cfg.get("shell", {}).get("sandbox_mode", True))
else:
    SHELL_SANDBOX_MODE = _shell_sandbox_env.strip().lower() in {"1", "true", "yes", "on"}
CODE_TIMEOUT: int = int(_cfg.get("code_timeout", 30))
PREVIEW_MODE: bool = bool(_cfg.get("preview_mode", False))
LOG_LEVEL: str = _cfg.get("logging", {}).get("level", "INFO")
BROWSER_HEADLESS: bool = bool(
    _cfg.get("browser_provider", {}).get("browser_headless", False)
)
# Canonical fallback for `use_real_browser` when the key is absent from
# config.yaml. False keeps Kim on its dedicated managed Chromium instead of
# silently attaching to the user's real Chrome over CDP — a privacy change a
# missing key should never trigger. Mirrors the shipped `config.yaml`
# (`use_real_browser: false`); see tests/test_config_parity.py.
DEFAULT_USE_REAL_BROWSER: bool = False
USE_REAL_BROWSER: bool = bool(
    _cfg.get("use_real_browser", DEFAULT_USE_REAL_BROWSER)
)
VOICE_ENABLED: bool = bool(_cfg.get("voice_enabled", False))

# ── Site connectors ───────────────────────────────────────────────────────
# `connectors.enabled` is a list of connector ids (e.g. ["guc_cms",
# "guc_mail"]). The MCP server merges those connectors' tools into its
# dispatch map at startup, so the LLM only sees toolkits the user has
# explicitly opted into. Unknown ids are warned about and skipped.
_connectors_cfg = _cfg.get("connectors", {}) or {}
ENABLED_CONNECTOR_IDS: list[str] = list(_connectors_cfg.get("enabled", []))

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
    # Cloud SDKs / secret stores (G2)
    _HOME / ".config" / "gcloud",
    _HOME / ".password-store",
    # Browser profiles (cookies + saved sessions = account takeover) (G2)
    _HOME / ".mozilla",
    # macOS-specific. These are harmless no-op prefixes on other platforms
    # (a path that never matches a real file), so they are listed
    # unconditionally — matching the existing unconditional Keychains entry —
    # rather than platform-guarded, which keeps them testable everywhere.
    _HOME / "Library" / "Keychains",
    _HOME / "Library" / "Application Support" / "Google" / "Chrome",
    _HOME / "Library" / "Application Support" / "Firefox",
    _HOME / "Library" / "Application Support" / "Code",
    # Linux browser/session stores (cookies + saved sessions = account takeover).
    # Listed unconditionally for the same reason as the macOS paths above —
    # a non-existent path is a harmless no-op on platforms that don't have it.
    _HOME / ".config" / "google-chrome",
    _HOME / ".config" / "chromium",
    _HOME / ".config" / "Code",
    # Windows AppData paths. On POSIX these expand to non-existent paths and
    # never match, so no platform guard is needed.
    _HOME / "AppData" / "Roaming" / "Google" / "Chrome" / "User Data",
    _HOME / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles",
    _HOME / "AppData" / "Roaming" / "Code",
    _HOME / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
]
# Secret-file name patterns denied at ANY depth inside an allowed root (G1).
# Matched case-by-case against `p.name` via fnmatch in validate_path().
_SENSITIVE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "credentials",
    ".npmrc",
    ".pypirc",
]


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

    # Check against sensitive path deny list — CASE-INSENSITIVELY. macOS/Windows
    # filesystems are case-insensitive, so ~/.AWS/CREDENTIALS resolves to the same
    # on-disk file as ~/.aws/credentials; a case-sensitive comparison (the old
    # relative_to / fnmatch) let case-varied paths slip past the deny-list. Path.resolve
    # canonicalizes symlinks and .. but NOT case, so we must lower-case explicitly.
    p_low = str(p).lower()
    for sensitive in _SENSITIVE_PATHS:
        s_low = str(sensitive).lower()
        if p_low == s_low or p_low.startswith(s_low + os.sep) or p_low.startswith(s_low + "/"):
            raise PermissionError(
                f"Path '{p}' is inside sensitive directory '{sensitive}' — access denied"
            )

    # Check for secret files (.env, keys, credentials) at ANY depth (G1), case-insensitively.
    name_low = p.name.lower()
    for pattern in _SENSITIVE_GLOBS:
        if fnmatch.fnmatch(name_low, pattern.lower()):
            raise PermissionError(
                f"Path '{p}' matches sensitive file pattern '{pattern}' "
                f"({p.name}) — access denied"
            )

    return p


def get_config() -> dict:
    return _cfg
