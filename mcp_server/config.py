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
    """Load config.yaml into a dict.

    A malformed or non-mapping config must never crash the MCP server at
    import (finding 1.2 / 1.3): a YAML syntax error or a top-level that is not
    a mapping falls back to an empty config with a warning, mirroring the Rust
    loader (desktop/src-tauri/src/config.rs) which already degrades gracefully.
    """
    if not _CONFIG_PATH.exists():
        return {}
    try:
        with open(_CONFIG_PATH, "r") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as exc:
        logger.warning(
            "Failed to parse %s (%s); using built-in defaults.", _CONFIG_PATH, exc
        )
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "%s top-level is %s (expected a mapping); using built-in defaults.",
            _CONFIG_PATH, type(data).__name__,
        )
        return {}
    return data


# ── Type-coercion helpers ──────────────────────────────────────────────────
# A partial or hand-edited config.yaml commonly leaves a section header with no
# body (e.g. `shell:` with every option commented out) — YAML parses that as
# None. `None.get(...)` would raise AttributeError at import and stop the MCP
# server from ever starting (finding 1.2). These helpers make every section
# read and scalar coercion null-safe and type-safe instead.

def _section(cfg: dict, key: str) -> dict:
    """Return cfg[key] when it is a mapping, else an empty dict."""
    val = cfg.get(key)
    return val if isinstance(val, dict) else {}


def _as_bool(val: object, default: bool) -> bool:
    """Coerce a config value to bool without the str-is-always-truthy trap.

    `bool("false")` is True, so `bool(cfg.get(...))` silently inverts a quoted
    "false"/"no"/"off" (finding 1.4 — a privacy inversion for use_real_browser).
    Recognise the common textual spellings; warn and fall back on anything else.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off", ""}:
        return False
    logger.warning("config: expected a boolean, got %r; using %s", val, default)
    return default


def _as_int(val: object, default: int) -> int:
    """Coerce a config value to int, warning and falling back on bad input."""
    if val is None:
        return default
    if isinstance(val, bool):
        # bool is an int subclass; a boolean where a count/timeout is expected
        # is almost certainly a mistake, so reject rather than silently use 0/1.
        logger.warning("config: expected an integer, got bool %r; using %s", val, default)
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    logger.warning("config: expected an integer, got %r; using %s", val, default)
    return default


_cfg = _load_yaml()

# Resolve project_root relative to config.yaml's directory — NOT cwd.
# This makes project_root: "." work correctly regardless of launch dir.
_raw_root = os.environ.get("PROJECT_ROOT") or _cfg.get("project_root") or str(_PROJECT_DIR)
_root_path = Path(str(_raw_root))
if not _root_path.is_absolute():
    _root_path = _PROJECT_DIR / _root_path
PROJECT_ROOT = _root_path.resolve()

def _resolve_allowed_paths(raw: object, project_dir: Path, project_root: Path) -> list[Path]:
    """Coerce and resolve the allowed_paths config value to a list of roots.

    allowed_paths MUST be a list. A bare scalar string (e.g. `allowed_paths: ~`)
    is a common YAML mistake that, if iterated directly, is walked
    CHARACTER-BY-CHARACTER — turning "~/x" into allowed roots "/" and "$HOME",
    a full-filesystem sandbox escape (finding 1.1). Coerce a lone string to a
    one-element list, reject any other non-list shape, and skip non-string
    entries. project_root is always appended so the sandbox never excludes it.
    """
    if isinstance(raw, str):
        logger.warning(
            "allowed_paths should be a LIST, got a bare string %r; treating it as a "
            "single entry. Use YAML list syntax (e.g. `allowed_paths: [\"%s\"]`).",
            raw, raw,
        )
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        logger.warning(
            "allowed_paths must be a list, got %s; restricting to project root only.",
            type(raw).__name__,
        )
        raw = [str(project_root)]

    resolved: list[Path] = []
    for p in raw:
        if not isinstance(p, str):
            logger.warning("allowed_paths entry %r is not a string; skipping.", p)
            continue
        pp = Path(p).expanduser()
        if not pp.is_absolute():
            pp = project_dir / pp
        # #3: Warn when ~ grants access to entire home directory
        if p.strip() == "~" or p.strip() == "~/":
            logger.warning(
                "⚠ '~' in allowed_paths grants access to entire home directory; "
                "consider scoping to '~/Projects' or '.' (project root only)."
            )
        resolved.append(pp.resolve())
    if project_root not in resolved:
        resolved.append(project_root)
    return resolved


ALLOWED_PATHS = _resolve_allowed_paths(
    _cfg.get("allowed_paths", [str(PROJECT_ROOT)]), _PROJECT_DIR, PROJECT_ROOT
)

SHELL_TIMEOUT: int = _as_int(_section(_cfg, "shell").get("timeout"), 30)
_shell_sandbox_env = os.environ.get("KIM_SHELL_SANDBOX_MODE")
if _shell_sandbox_env is None:
    SHELL_SANDBOX_MODE: bool = _as_bool(_section(_cfg, "shell").get("sandbox_mode"), True)
else:
    SHELL_SANDBOX_MODE = _shell_sandbox_env.strip().lower() in {"1", "true", "yes", "on"}
CODE_TIMEOUT: int = _as_int(_cfg.get("code_timeout"), 30)
PREVIEW_MODE: bool = _as_bool(_cfg.get("preview_mode"), False)
LOG_LEVEL: str = str(_section(_cfg, "logging").get("level") or "INFO")
BROWSER_HEADLESS: bool = _as_bool(
    _section(_cfg, "browser_provider").get("browser_headless"), False
)
# Canonical fallback for `use_real_browser` when the key is absent from
# config.yaml. False keeps Kim on its dedicated managed Chromium instead of
# silently attaching to the user's real Chrome over CDP — a privacy change a
# missing key should never trigger. Mirrors the shipped `config.yaml`
# (`use_real_browser: false`); see tests/test_config_parity.py.
DEFAULT_USE_REAL_BROWSER: bool = False
USE_REAL_BROWSER: bool = _as_bool(
    _cfg.get("use_real_browser", DEFAULT_USE_REAL_BROWSER), DEFAULT_USE_REAL_BROWSER
)
VOICE_ENABLED: bool = _as_bool(_cfg.get("voice_enabled", False), False)

# ── Site connectors ───────────────────────────────────────────────────────
# `connectors.enabled` is a list of connector ids (e.g. ["my_site"]).
# The MCP server merges those connectors' tools into its
# dispatch map at startup, so the LLM only sees toolkits the user has
# explicitly opted into. Unknown ids are warned about and skipped.
_connectors_cfg = _section(_cfg, "connectors")
_enabled_ids_raw = _connectors_cfg.get("enabled", [])
ENABLED_CONNECTOR_IDS: list[str] = (
    list(_enabled_ids_raw) if isinstance(_enabled_ids_raw, (list, tuple)) else []
)

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
#
# "credentials*" is prefix-anchored: it only matches filenames STARTING WITH
# "credentials", missing the far more common naming convention of a prefixed
# credential file (google_credentials.json, aws_credentials.json,
# service_account_credentials.json). "*credentials*" (substring, anywhere in
# the filename) closes that gap. "credentials" is specific enough as a
# substring that it doesn't collide with ordinary source filenames the way a
# bare "*token*" would (tokenizer.py, auth_token_test.py, token_bucket.py are
# all legitimate, non-secret source files) -- so "token" secrets are matched
# by exact/narrow filenames instead of a substring glob: `token.json` and
# `authorized_user.json` are the literal filenames Google's OAuth client
# libraries write to disk for cached credentials, not generic identifiers.
_SENSITIVE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*credentials*",
    "*_credentials.json",
    "client_secret*",
    "*.credentials",
    "token.json",
    "authorized_user.json",
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
    """Return a shallow copy of the loaded config.

    A copy (rather than the module-global dict) keeps callers from silently
    mutating the shared configuration — e.g. policy.hitl_threshold() reads
    hitl_risk_threshold from this dict on every call (finding 1.5).
    """
    return dict(_cfg)
