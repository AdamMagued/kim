"""Regression tests for mcp_server/logger.py — structured JSON logger."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from mcp_server.logger import (
    JSONLineHandler,
    _redact,
    _redact_value,
    setup_structured_logging,
)


# ---------------------------------------------------------------------------
# 1. secrets_redacted_in_output
# ---------------------------------------------------------------------------

def test_secrets_redacted_sk_key():
    """OpenAI/Anthropic-style sk- keys are masked."""
    raw = "Authorization: sk-abcdefghij1234567890"
    result = _redact(raw)
    assert "sk-abcdefghij1234567890" not in result
    assert "REDACTED" in result


def test_secrets_redacted_github_pat():
    """GitHub PAT (ghp_ prefix) is masked."""
    raw = "token=ghp_abcdefghij1234567890"
    result = _redact(raw)
    assert "ghp_abcdefghij1234567890" not in result
    assert "REDACTED" in result


def test_secrets_redacted_github_fine_grained_pat():
    """GitHub fine-grained PAT (github_pat_ prefix) is masked."""
    raw = "github_pat_abcdefghij1234567890"
    result = _redact(raw)
    assert "github_pat_abcdefghij1234567890" not in result
    assert "REDACTED" in result


def test_secrets_redacted_aws_key():
    """AWS access key IDs (AKIA...) are masked."""
    raw = "aws key: AKIAIOSFODNN7EXAMPLE"
    result = _redact(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "REDACTED" in result


def test_secrets_redacted_bearer_token():
    """Bearer tokens in Authorization headers are masked."""
    raw = "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9"
    result = _redact(raw)
    assert "eyJhbGciOiJSUzI1NiJ9" not in result
    assert "REDACTED" in result


def test_secrets_redacted_slack_token():
    """Slack tokens (xox*) are masked."""
    raw = "slack token: xoxb-abcdef1234-abcdefghij"
    result = _redact(raw)
    assert "xoxb-abcdef1234-abcdefghij" not in result
    assert "REDACTED" in result


def test_secrets_redacted_pem_block():
    """PEM private key blocks are collapsed to a single redaction marker."""
    raw = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = _redact(raw)
    assert "MIIEpAIBAAKCAQEA" not in result
    assert "REDACTED" in result


def test_secrets_redacted_in_jsonline_emit(tmp_path):
    """JSONLineHandler masks secrets in the emitted JSONL message field."""
    handler = JSONLineHandler(log_dir=str(tmp_path), level=logging.DEBUG)
    logger = logging.getLogger("kim.test.redact")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("key=sk-supersecretkey12345678")
    finally:
        handler.close()
        logger.removeHandler(handler)

    log_files = list(tmp_path.glob("kim_*.jsonl"))
    assert log_files, "No log file was written"
    entries = [json.loads(line) for line in log_files[0].read_text().splitlines() if line.strip()]
    messages = [e["message"] for e in entries]
    assert any("REDACTED" in m for m in messages), f"Expected redaction in: {messages}"
    assert not any("sk-supersecretkey12345678" in m for m in messages)


def test_redact_value_recurses_into_dict():
    """_redact_value masks secrets nested inside dicts."""
    data = {"api_key": "sk-abcdefghij1234567890", "count": 3}
    result = _redact_value(data)
    assert "sk-abcdefghij1234567890" not in result["api_key"]
    assert "REDACTED" in result["api_key"]
    assert result["count"] == 3


def test_redact_value_recurses_into_list():
    """_redact_value masks secrets inside lists."""
    data = ["sk-abcdefghij1234567890", "safe"]
    result = _redact_value(data)
    assert "REDACTED" in result[0]
    assert result[1] == "safe"


# ---------------------------------------------------------------------------
# 2. normal_message_unchanged
# ---------------------------------------------------------------------------

def test_normal_message_unchanged():
    """A benign message passes through _redact unmodified."""
    msg = "Task completed successfully in 1.23s"
    assert _redact(msg) == msg


def test_normal_message_unchanged_in_jsonline_emit(tmp_path):
    """JSONLineHandler preserves benign messages verbatim in the JSONL output."""
    handler = JSONLineHandler(log_dir=str(tmp_path), level=logging.DEBUG)
    logger = logging.getLogger("kim.test.benign")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    benign = "File written: /home/user/report.txt (1024 bytes)"
    try:
        logger.info(benign)
    finally:
        handler.close()
        logger.removeHandler(handler)

    log_files = list(tmp_path.glob("kim_*.jsonl"))
    assert log_files, "No log file was written"
    entries = [json.loads(line) for line in log_files[0].read_text().splitlines() if line.strip()]
    messages = [e["message"] for e in entries]
    assert any(benign in m for m in messages), f"Expected benign message preserved in: {messages}"


def test_redact_value_non_string_scalar_unchanged():
    """Non-string scalars (int, bool, None) are returned as-is by _redact_value."""
    assert _redact_value(42) == 42
    assert _redact_value(True) is True
    assert _redact_value(None) is None


# ---------------------------------------------------------------------------
# 3. log_dir_resolution
# ---------------------------------------------------------------------------

def test_log_dir_resolution_uses_configured_dir(tmp_path):
    """JSONLineHandler writes log files into the explicitly configured directory."""
    log_dir = tmp_path / "custom_logs"
    handler = JSONLineHandler(log_dir=str(log_dir), level=logging.DEBUG)
    logger = logging.getLogger("kim.test.dir")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("directory resolution test")
    finally:
        handler.close()
        logger.removeHandler(handler)

    log_files = list(log_dir.glob("kim_*.jsonl"))
    assert log_files, f"Expected log files in {log_dir}, found none"
    # Confirm file is inside the configured dir, not a hardcoded absolute path
    for lf in log_files:
        assert lf.parent.resolve() == log_dir.resolve()


def test_log_dir_resolution_via_setup(tmp_path):
    """setup_structured_logging writes to the log_dir argument, not a hardcoded path."""
    log_dir = tmp_path / "structured_logs"
    handler = setup_structured_logging(
        log_dir=str(log_dir),
        level=logging.DEBUG,
        also_stderr=False,
    )
    root = logging.getLogger()
    try:
        logging.getLogger("kim.test.setup").info("setup resolution test")
    finally:
        handler.close()
        root.removeHandler(handler)

    log_files = list(log_dir.glob("kim_*.jsonl"))
    assert log_files, f"Expected log files in {log_dir}, found none"
    for lf in log_files:
        assert lf.parent.resolve() == log_dir.resolve()


def test_log_dir_created_automatically(tmp_path):
    """JSONLineHandler auto-creates the log directory if it does not exist."""
    log_dir = tmp_path / "nested" / "auto_created"
    assert not log_dir.exists()
    handler = JSONLineHandler(log_dir=str(log_dir), level=logging.DEBUG)
    handler.close()
    assert log_dir.exists(), "Handler should have created the log directory"


def test_log_filename_contains_date(tmp_path):
    """Log filenames follow the kim_YYYY-MM-DD.jsonl convention."""
    from datetime import datetime, timezone

    handler = JSONLineHandler(log_dir=str(tmp_path), level=logging.DEBUG)
    logger = logging.getLogger("kim.test.filename")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("filename convention test")
    finally:
        handler.close()
        logger.removeHandler(handler)

    log_files = list(tmp_path.glob("kim_*.jsonl"))
    assert log_files, "No log file found"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert any(f"kim_{today}.jsonl" == lf.name for lf in log_files)
