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

# ── Canonical shared defaults ──────────────────────────────────────────────
# These are the canonical fallbacks for shared runtime config; `config.yaml`
# (when present) overrides them. On a fresh install `config.yaml` is usually
# absent (it is gitignored — `config.yaml.example` is the tracked template), so
# these constants are what actually governs. They exist so the orchestrator's
# call sites no longer re-type an inline default that can drift.
# `DEFAULT_PROVIDER` is `ollama` because that is what a fresh desktop
# install actually runs (the frontend sends `ollama` and the Rust subprocess
# falls back to `ollama`), it needs no API key, and it matches Settings.
# `tests/test_config_parity.py` asserts this stays in sync across every loader.
DEFAULT_PROVIDER = "ollama"


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
