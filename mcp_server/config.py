import os
import sys
import logging
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _load_yaml() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg = _load_yaml()

PROJECT_ROOT = Path(
    os.environ.get("PROJECT_ROOT") or _cfg.get("project_root", str(Path.home()))
).resolve()

_raw_allowed = _cfg.get("allowed_paths", [str(PROJECT_ROOT)])
ALLOWED_PATHS = [Path(p).resolve() for p in _raw_allowed]
if PROJECT_ROOT not in ALLOWED_PATHS:
    ALLOWED_PATHS.append(PROJECT_ROOT)

BLOCKED_COMMANDS: list[str] = _cfg.get("shell", {}).get("blocked_commands", [
    "rm -rf /", "format c:", "del /S /Q C:\\", "rd /S /Q C:\\"
])

SHELL_TIMEOUT: int = int(_cfg.get("shell", {}).get("timeout", 30))
PREVIEW_MODE: bool = bool(_cfg.get("preview_mode", False))
LOG_LEVEL: str = _cfg.get("logging", {}).get("level", "INFO")
BROWSER_HEADLESS: bool = bool(
    _cfg.get("browser_provider", {}).get("browser_headless", False)
)


def validate_path(path_str: str) -> Path:
    """
    Resolve path_str relative to PROJECT_ROOT and verify it stays within
    an allowed root. Raises PermissionError if outside allowed paths.
    """
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    for allowed in ALLOWED_PATHS:
        try:
            p.relative_to(allowed)
            return p
        except ValueError:
            continue
    raise PermissionError(
        f"Path '{p}' is outside allowed directories: {[str(a) for a in ALLOWED_PATHS]}"
    )


def get_config() -> dict:
    return _cfg
