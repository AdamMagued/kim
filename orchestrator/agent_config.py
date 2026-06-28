"""
Config-resolution helpers extracted from orchestrator/agent.py.

load_config reads config.yaml; _resolve_hitl_threshold derives the HITL risk
threshold from the config dict + an optional env var.
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Optional[str] = None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        logger.warning(f"config.yaml not found at {cfg_path}, using defaults")
        return {}
    with open(cfg_path) as f:
        return yaml.safe_load(f) or {}


def _resolve_hitl_threshold(config: dict, env_val: Optional[str] = None) -> Optional[str]:
    """Return the HITL risk threshold from config or env var.

    Accepts "high", "medium", or "low".  Config key wins over env var.
    Any other value (including None / empty string) disables the gate.
    """
    raw = config.get("hitl_risk_threshold") or env_val
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    return normalized if normalized in ("high", "medium", "low") else None
