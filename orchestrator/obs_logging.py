"""Observability logging helpers for the Kim agent.

Provides init_logging() which attaches the current run_id and session_id to
every LogRecord via a custom factory.  Call once at agent startup so that all
subsequent log lines carry KIM_RUN_ID / KIM_SESSION_ID context without any
changes to existing log call sites.

The log level is controlled by the KIM_LOG_LEVEL environment variable
(DEBUG / INFO / WARNING / ERROR).  When unset the level is unchanged.
"""

from __future__ import annotations

import logging
import os

_INITIALIZED = False


def init_logging(
    run_id: str = "",
    session_id: str = "",
    *,
    level: int | None = None,
) -> None:
    """Attach run_id / session_id to every LogRecord and honour KIM_LOG_LEVEL.

    Safe to call multiple times — subsequent calls update run_id / session_id
    on the existing factory without re-installing the chain.

    Args:
        run_id: Opaque identifier for the current agent run (e.g. a UUID or
                sequential counter).  Defaults to the KIM_RUN_ID env var.
        session_id: Persistent session identifier.  Defaults to
                    KIM_SESSION_ID env var.
        level: Explicit log level to apply to the root logger.  When None,
               KIM_LOG_LEVEL is consulted instead.
    """
    global _INITIALIZED

    # Resolve run/session IDs from env when not supplied by caller.
    run_id = run_id or os.environ.get("KIM_RUN_ID", "")
    session_id = session_id or os.environ.get("KIM_SESSION_ID", "")

    if not _INITIALIZED:
        _old_factory = logging.getLogRecordFactory()

        def _record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = _old_factory(*args, **kwargs)
            # Always read from the module-level closure vars so that a second
            # init_logging() call (which updates _current_ids) is reflected in
            # subsequent log records without re-installing the factory.
            _ids = _current_ids
            record.run_id = _ids[0]      # type: ignore[attr-defined]
            record.session_id = _ids[1]  # type: ignore[attr-defined]
            return record

        logging.setLogRecordFactory(_record_factory)
        _INITIALIZED = True

    # Mutate the shared state so the already-installed factory picks up new values.
    _current_ids[0] = run_id
    _current_ids[1] = session_id

    # Apply log level from argument or environment variable.
    effective_level = level
    if effective_level is None:
        env_level = os.environ.get("KIM_LOG_LEVEL", "").upper()
        if env_level:
            effective_level = getattr(logging, env_level, None)
    if effective_level is not None:
        logging.getLogger().setLevel(effective_level)


# Mutable list so the factory closure can see updates made by later calls.
_current_ids: list[str] = ["", ""]
