"""Cross-platform helpers shared by the policy and subprocess layers."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
_TRUSTED_BIN_PREFIXES = (
    "/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/",
    "/usr/local/bin/", "/opt/homebrew/bin/", "/opt/local/bin/",
)
_SAFE_DEVICE_PATHS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin",
    "/dev/tty", "/dev/urandom", "/dev/zero",
})
_WINDOWS_RUNTIME_ENV_KEYS = (
    "SYSTEMROOT", "COMSPEC", "USERPROFILE", "TEMP", "TMP", "PATHEXT",
)


def _portable_basename(value: str) -> str:
    """Return a basename for either slash convention on every host OS."""
    text = value.strip().strip('"').strip("'").rstrip("/\\")
    return re.split(r"[/\\]", text)[-1].lower() if text else ""


def _strip_executable_suffix(base: str) -> str:
    lowered = base.lower()
    for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[:-len(suffix)]
    return lowered


def _env_lookup(source: Mapping[str, str], *names: str) -> str:
    values = {key.upper(): value for key, value in source.items()}
    return next((values[name.upper()] for name in names if values.get(name.upper())), "")


def _is_trusted_binary_path(real_path: str) -> bool:
    normalized = os.path.normcase(os.path.abspath(real_path))
    if os.name == "nt":
        system_root = _env_lookup(os.environ, "SYSTEMROOT", "SystemRoot")
        if not system_root:
            return False
        prefixes = (system_root, os.path.join(system_root, "System32"))
    else:
        prefixes = _TRUSTED_BIN_PREFIXES
    normalized_prefixes = (
        os.path.normcase(os.path.abspath(prefix)).rstrip("/\\") + os.sep
        for prefix in prefixes
    )
    return any(normalized.startswith(prefix) for prefix in normalized_prefixes)


def _split_shell_tokens(command: str) -> list[str]:
    """Tokenize without treating Windows path backslashes as escapes."""
    tokens = shlex.split(command, posix=os.name != "nt")
    if os.name != "nt":
        return tokens
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
        else token
        for token in tokens
    ]


def _is_safe_device_path(value: str) -> bool:
    """Return True only for exact POSIX null/device paths or Windows NUL."""
    normalized = value.strip().strip('"').strip("'")
    return normalized in _SAFE_DEVICE_PATHS or (os.name == "nt" and normalized.upper() == "NUL")


def _with_windows_env_aliases(
    env: Mapping[str, str],
    source: Mapping[str, str] | None = None,
    *,
    include_runtime: bool = False,
) -> dict[str, str]:
    """Add canonical Windows aliases without widening the environment allowlist."""
    result = dict(env)
    if os.name != "nt":
        return result
    source_env = os.environ if source is None else source
    path_value = _env_lookup(source_env, "PATH", "Path")
    home_value = _env_lookup(source_env, "HOME", "USERPROFILE")
    result.pop("Path", None)
    if path_value:
        result["PATH"] = path_value
    if home_value:
        result["HOME"] = home_value
    if include_runtime:
        for key in _WINDOWS_RUNTIME_ENV_KEYS:
            value = _env_lookup(source_env, key)
            if value:
                result[key] = value
    return result
