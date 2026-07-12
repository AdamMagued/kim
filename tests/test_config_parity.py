"""
Config parity invariants — single canonical config source of truth (#18 / R-5).

`config.yaml` is the canonical data file for shared runtime config. Every runtime
keeps exactly one loader that reads it, and the config-absent fallback defaults
must all resolve to the SAME values, sourced from one shared constant per runtime
rather than a literal re-typed inline at each call site.

Two keys had drifted before this landed:
  - `provider` resolved to four different values across loaders
    (`browser` / `claude` / `ollama`).
  - `use_real_browser` had opposite polarity between the Python in-code default
    (`True`) and the shipped `config.yaml` (`false`).

These tests lock the alignment so a future inline default cannot silently
reintroduce the drift. They read source text for the Rust/TS layers — the same
technique `test_invariants.py` uses for the Code-tab provider constraint —
because no static, in-process check can observe which default a *missing* key
resolves to across process boundaries; that only surfaces at runtime on a fresh
config, which is exactly where the drift used to hide.

Canonical values (chosen to match what the shipping desktop app runs on today):
  - provider          = "ollama"
  - use_real_browser  = False
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orchestrator.agent_config import DEFAULT_PROVIDER
from mcp_server.config import DEFAULT_USE_REAL_BROWSER

_REPO = Path(__file__).resolve().parent.parent

# The tracked canonical data file is `config.yaml.example` — the user's
# `config.yaml` is gitignored (a per-install instance derived from this template),
# so the example is what is version-controlled and what seeds a fresh install.
# Read it directly so these assertions are deterministic on a clean checkout
# (where no local config.yaml exists).
_CANONICAL_YAML = _REPO / "config.yaml.example"

# The single source of truth every layer must agree with.
CANONICAL_PROVIDER = "ollama"
CANONICAL_USE_REAL_BROWSER = False


def _load_canonical_yaml() -> dict:
    return yaml.safe_load(_CANONICAL_YAML.read_text()) or {}


class TestSharedConstants:
    """The in-code shared constants ARE the canonical values."""

    def test_default_provider_is_canonical(self):
        assert DEFAULT_PROVIDER == CANONICAL_PROVIDER

    def test_default_use_real_browser_is_canonical(self):
        assert DEFAULT_USE_REAL_BROWSER is CANONICAL_USE_REAL_BROWSER


class TestShippedConfigMatchesDefaults:
    """The shipped config.yaml.example agrees with the in-code fallback defaults,
    so a fresh install and a missing-key fallback resolve to the same thing."""

    def test_yaml_provider_matches_default(self):
        cfg = _load_canonical_yaml()
        assert cfg.get("provider") == DEFAULT_PROVIDER, (
            f"config.yaml.example provider={cfg.get('provider')!r} but "
            f"DEFAULT_PROVIDER={DEFAULT_PROVIDER!r} — the data file and the "
            "config-absent fallback have drifted."
        )

    def test_yaml_use_real_browser_matches_default(self):
        cfg = _load_canonical_yaml()
        assert cfg.get("use_real_browser") is False, (
            f"config.yaml.example use_real_browser={cfg.get('use_real_browser')!r} — "
            "must ship false (canonical) so a missing key never flips Kim onto "
            "the user's real Chrome over CDP."
        )
        assert DEFAULT_USE_REAL_BROWSER is False


class TestOrchestratorCallSitesUseSharedConstant:
    """agent.py / cli.py must source the provider default from the shared
    constant, not a re-typed inline literal (the old drift was `"claude"`)."""

    _PROVIDER_FROM_CONSTANT = re.compile(
        r"""config\.get\(\s*['"]provider['"]\s*,\s*DEFAULT_PROVIDER\s*\)"""
    )
    # Any string literal as the provider default = drift creeping back.
    _PROVIDER_FROM_LITERAL = re.compile(
        r"""config\.get\(\s*['"]provider['"]\s*,\s*['"][\w:.-]+['"]\s*\)"""
    )

    def test_agent_uses_shared_default(self):
        src = (_REPO / "orchestrator" / "agent.py").read_text()
        assert self._PROVIDER_FROM_CONSTANT.search(src), (
            "orchestrator/agent.py must resolve the provider default via "
            "DEFAULT_PROVIDER, not an inline literal."
        )
        assert not self._PROVIDER_FROM_LITERAL.search(src), (
            "orchestrator/agent.py has an inline string provider default — "
            "use DEFAULT_PROVIDER instead (drift guard)."
        )

    def test_cli_uses_shared_default(self):
        src = (_REPO / "orchestrator" / "cli.py").read_text()
        assert self._PROVIDER_FROM_CONSTANT.search(src), (
            "orchestrator/cli.py must resolve the provider default via "
            "DEFAULT_PROVIDER, not an inline literal."
        )
        assert not self._PROVIDER_FROM_LITERAL.search(src), (
            "orchestrator/cli.py has an inline string provider default — "
            "use DEFAULT_PROVIDER instead (drift guard)."
        )


class TestMcpServerInCodeDefault:
    """mcp_server/config.py's use_real_browser fallback is the shared constant,
    flipped from the old True to the canonical False."""

    def test_uses_constant_not_inline_true(self):
        src = (_REPO / "mcp_server" / "config.py").read_text()
        assert re.search(
            r"""_cfg\.get\(\s*['"]use_real_browser['"]\s*,\s*DEFAULT_USE_REAL_BROWSER\s*\)""",
            src,
        ), "mcp_server/config.py must use DEFAULT_USE_REAL_BROWSER as the fallback."
        assert not re.search(
            r"""_cfg\.get\(\s*['"]use_real_browser['"]\s*,\s*True\s*\)""",
            src,
        ), (
            "mcp_server/config.py still defaults use_real_browser to True — that "
            "is the polarity that flipped Kim onto the real browser on a missing key."
        )


class TestCrossLanguageProviderParity:
    """Every other runtime's config-absent provider default also resolves to the
    canonical value. Source-text reads, matching test_invariants.py — a fresh
    config's winner is only observable at runtime, so these guard it statically."""

    def test_rust_subprocess_fallback_is_canonical(self):
        # K2: the GUI provider default is the literal passed to
        # task_spec::promote_provider(provider, "<default>", is_codex).
        src = (_REPO / "desktop" / "src-tauri" / "src" / "subprocess.rs").read_text()
        m = re.search(
            r"""promote_provider\(provider,\s*["'](\w+)["']""", src
        )
        assert m is not None, "could not find the provider fallback in subprocess.rs"
        assert m.group(1) == CANONICAL_PROVIDER, (
            f"subprocess.rs provider fallback={m.group(1)!r} != {CANONICAL_PROVIDER!r}"
        )

    def test_cli_config_default_is_canonical(self):
        src = (_REPO / "cli" / "src" / "config.rs").read_text()
        m = re.search(r"""provider:\s*["']([\w:.-]+)["']\.to_string\(\)""", src)
        assert m is not None, "could not find provider default in cli/src/config.rs"
        assert m.group(1) == CANONICAL_PROVIDER, (
            f"cli/src/config.rs provider default={m.group(1)!r} != {CANONICAL_PROVIDER!r}"
        )

    def test_frontend_default_settings_provider_is_canonical(self):
        src = (_REPO / "desktop" / "src" / "types" / "index.ts").read_text()
        # DEFAULT_SETTINGS.provider — the explicit value the desktop frontend
        # sends to the subprocess on every run.
        m = re.search(r"""\n\s*provider:\s*'([\w:.-]+)'""", src)
        assert m is not None, "could not find DEFAULT_SETTINGS.provider in index.ts"
        assert m.group(1) == CANONICAL_PROVIDER, (
            f"index.ts DEFAULT_SETTINGS.provider={m.group(1)!r} != {CANONICAL_PROVIDER!r}"
        )


class TestModelMapParity:
    """config.rs default_model_map() carries a `claude` entry that matches
    the canonical config's model block (issue #18 step 4)."""

    def test_rust_default_model_map_has_claude_matching_yaml(self):
        cfg = _load_canonical_yaml()
        yaml_claude = cfg.get("model", {}).get("claude")
        assert yaml_claude, "config.yaml.example model.claude is missing"
        src = (_REPO / "desktop" / "src-tauri" / "src" / "config.rs").read_text()
        m = re.search(
            r"""map\.insert\(\s*"claude"\.to_string\(\)\s*,\s*"([^"]+)"\.to_string\(\)\)""",
            src,
        )
        assert m is not None, (
            "default_model_map() in config.rs has no claude entry (issue #18 step 4)"
        )
        assert m.group(1) == yaml_claude, (
            f"config.rs claude default={m.group(1)!r} != config.yaml model.claude={yaml_claude!r}"
        )


class TestNoDeadSecurityConfigKeys:
    """F-G-4: `shell.blocked_commands` was shipped in the canonical config but
    read by NOTHING — an operator who added a command to it reasonably believed
    it was now blocked, and it was silently ignored (false security). The key
    was deleted, and the deny-list is documented as CODE-OWNED in
    mcp_server/tools/shell.py (_DENY_COMMANDS / _DENY_PATTERNS). Per the
    standing invariant, config must never be able to weaken (or pretend to
    extend) a code-owned safety gate, so the key must not come back — neither
    as a dead key nor as a live one.
    """

    def test_canonical_config_has_no_blocked_commands_key(self):
        cfg = _load_canonical_yaml()
        shell_cfg = cfg.get("shell") or {}
        assert "blocked_commands" not in shell_cfg, (
            "config.yaml.example reintroduces shell.blocked_commands — the "
            "deny-list is code-owned (mcp_server/tools/shell.py); a config "
            "key here is either dead (false security, F-G-4) or a gate "
            "weakener. Remove it."
        )
        # Belt-and-braces: not anywhere else in the file either.
        assert "blocked_commands" not in _CANONICAL_YAML.read_text()

    def test_no_python_runtime_reads_blocked_commands(self):
        # If someone wires a reader, they wired config into a code-owned gate.
        for pkg in ("mcp_server", "orchestrator"):
            for path in sorted((_REPO / pkg).rglob("*.py")):
                src = path.read_text(errors="replace")
                assert "blocked_commands" not in src, (
                    f"{path.relative_to(_REPO)} references blocked_commands — "
                    "the shell deny-list must stay code-owned (F-G-4)"
                )

    def test_deny_list_is_code_owned_in_shell_py(self):
        src = (_REPO / "mcp_server" / "tools" / "shell.py").read_text()
        assert "_DENY_COMMANDS" in src and "_DENY_PATTERNS" in src
        assert "CODE-OWNED" in src, (
            "shell.py must document the deny sets as CODE-OWNED (F-G-4)"
        )
