"""
Kim — Structured JSON Lines Logger (Phase 7)

Provides a JSON Lines logging handler that writes structured log entries to
logs/kim_{date}.jsonl for easy auditing, debugging, and log aggregation.

Each log entry is a single JSON line containing:
  - timestamp (ISO 8601)
  - level (DEBUG/INFO/WARNING/ERROR/CRITICAL)
  - logger name
  - message
  - module, function, line number
  - exception info (if any)
  - extra fields (tool name, task id, etc.)

Usage:
    from mcp_server.logger import setup_structured_logging

    # Call once at startup:
    setup_structured_logging()

    # Or with custom settings:
    setup_structured_logging(log_dir="my_logs", level=logging.DEBUG)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path


class JSONLineHandler(logging.Handler):
    """
    Logging handler that writes one JSON object per line to a .jsonl file.

    Log files are named kim_{YYYY-MM-DD}.jsonl and rotate daily by filename.
    The handler auto-creates the log directory if needed.
    """

    def __init__(self, log_dir: str = "logs", level: int = logging.DEBUG):
        super().__init__(level=level)
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: str = ""
        self._file = None
        self._lock = threading.Lock()

    def _get_file(self):
        """Get (or open) the log file for today's date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date or self._file is None:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._current_date = today
            filepath = self._log_dir / f"kim_{today}.jsonl"
            self._file = open(filepath, "a", encoding="utf-8")
        return self._file

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                entry = {
                    "timestamp": datetime.fromtimestamp(
                        record.created, tz=timezone.utc
                    ).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "module": record.module,
                    "function": record.funcName,
                    "line": record.lineno,
                }

                # Include exception info if present
                if record.exc_info and record.exc_info[0] is not None:
                    entry["exception"] = {
                        "type": record.exc_info[0].__name__,
                        "message": str(record.exc_info[1]),
                        "traceback": traceback.format_exception(*record.exc_info),
                    }

                # Include any extra fields set via logger.info("msg", extra={...})
                standard_attrs = {
                    "name", "msg", "args", "created", "relativeCreated",
                    "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                    "filename", "module", "pathname", "thread", "threadName",
                    "process", "processName", "levelname", "levelno", "message",
                    "msecs", "taskName",
                }
                extras = {
                    k: v for k, v in record.__dict__.items()
                    if k not in standard_attrs and not k.startswith("_")
                }
                if extras:
                    entry["extra"] = extras

                f = self._get_file()
                f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
                f.flush()
            except Exception:
                self.handleError(record)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
        super().close()


def setup_structured_logging(
    log_dir: str = "logs",
    level: int = logging.INFO,
    also_stderr: bool = True,
) -> JSONLineHandler:
    """
    Set up structured JSON logging for the entire Kim application.

    Call this once at application startup. Attaches a JSONLineHandler to
    the root logger so all modules benefit from structured logging.

    Args:
        log_dir:     Directory to write .jsonl files to (default: "logs/")
        level:       Minimum log level for the JSON handler
        also_stderr: If True, also configure a human-readable stderr handler

    Returns:
        The JSONLineHandler instance (useful for cleanup).
    """
    root = logging.getLogger()

    # Avoid duplicate handlers on repeat calls
    for h in root.handlers[:]:
        if isinstance(h, JSONLineHandler):
            root.removeHandler(h)
            h.close()

    # JSON Lines handler
    json_handler = JSONLineHandler(log_dir=log_dir, level=level)
    root.addHandler(json_handler)

    # Human-readable stderr handler (for terminal/tray)
    if also_stderr:
        has_stderr = any(
            isinstance(h, logging.StreamHandler) and h.stream == sys.stderr
            for h in root.handlers
        )
        if not has_stderr:
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setLevel(level)
            stderr_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            root.addHandler(stderr_handler)

    root.setLevel(min(level, root.level) if root.level != logging.WARNING else level)

    logging.getLogger("kim.logger").info(
        f"Structured logging initialized: {log_dir}/kim_*.jsonl"
    )

    return json_handler
