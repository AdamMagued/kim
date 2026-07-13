"""
Regression pack for the argv-level exec-capable-binary policy in
mcp_server/tools/shell.py (F-C-1, F-C-2 — the Critical allowlist-escape family).

The shell allowlist trusts program NAMES, but `git`, `awk`, `tar`, `sed`, `make`
are general-purpose command runners: they exec arbitrary shell (or read any
absolute path — e.g. a secret — from inside their own argument grammar, which
the token-shape path scan cannot see). `_check_blocked` must deny the escape
vectors argv-side while leaving every binary's benign use working.

Every string in the BLOCK matrices is a real, evidence-backed escape from
docs/ops/findings/team-c.md (F-C-1 / F-C-2). Every string in the ALLOW matrices
is a legitimate use that must keep dispatching.
"""

from __future__ import annotations

import pytest

from mcp_server.tools.shell import _check_blocked


def _assert_blocked(cmd: str) -> None:
    result = _check_blocked(cmd)
    assert result is not None and "BLOCKED" in result, (
        f"Expected {cmd!r} to be BLOCKED but got: {result!r}"
    )


def _assert_allowed(cmd: str) -> None:
    result = _check_blocked(cmd)
    assert result is None, (
        f"Expected benign {cmd!r} to be allowed but it was blocked: {result!r}"
    )


# ── F-C-1: git config-injection RCE + absolute-path secret read ───────────────


@pytest.mark.parametrize("cmd", [
    # `!`-prefixed alias body = arbitrary shell, runs even OUTSIDE a repo
    "git -c 'alias.pwn=!echo PWNED_$(id -un)' pwn",
    "git -c alias.x=!id x",
    # absolute-path secret read hidden inside the alias value (bypasses S3)
    "git -c 'alias.exfil=!cat /Users/x/.ssh/id_rsa' exfil",
    # config keys git executes as a program
    "git -c core.fsmonitor=/tmp/evil status",
    "git -c core.pager=/tmp/evil log",
    "git -c core.editor=/tmp/evil commit",
    "git -c core.sshCommand=/tmp/evil fetch",
    "git -c sequence.editor=/tmp/evil rebase -i",
    "git -c diff.external=/tmp/evil diff",
    "git -c gpg.program=/tmp/evil tag -s x",
    "git -c credential.helper=/tmp/evil push",
    "git -c uploadpack.packObjectsHook=/tmp/evil fetch",
    # per-driver command hooks (suffix-matched)
    "git -c filter.lfs.clean=/tmp/evil add .",
    "git -c diff.foo.command=/tmp/evil diff",
    # --config-env variants (glued + separate)
    "git --config-env=core.pager=EVIL log",
    "git --config-env core.sshCommand=EVIL fetch",
    # glued short form
    "git -ccore.pager=/tmp/evil log",
    # case-insensitivity of the key
    "git -c CORE.PAGER=/tmp/evil log",
    # behind a wrapper
    "sudo git -c alias.x=!id x",
    "env git -c core.pager=/tmp/evil log",
])
def test_git_config_injection_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "git status",
    "git status --short",
    "git diff",
    "git diff --cached",
    "git add .",
    "git commit -m 'a message'",
    "git log --oneline",
    "git log -20 --format=%h",
    "git checkout main",
    "git fetch origin",
    "git push origin HEAD",
    # benign -c overrides that do NOT name a program/command hook
    "git -c user.email=x@y.z commit -m msg",
    "git -c user.name=Kim commit -m msg",
    "git -c core.autocrlf=false status",
    "git -c color.ui=always log",
])
def test_git_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-2: awk / gawk / mawk exec + arbitrary file IO ─────────────────────────


@pytest.mark.parametrize("cmd", [
    "awk 'BEGIN{system(\"id\")}'",
    "gawk 'BEGIN{system(\"id\")}'",
    "mawk 'BEGIN{system(\"id\")}'",
    "awk 'BEGIN{ system (\"cat /etc/passwd\") }'",
    "awk '{print $1 > \"/etc/passwd\"}'",
    "awk '{printf \"%s\", $0 >> \"/tmp/out\"}'",
    "awk 'BEGIN{while((getline line < \"/Users/x/.ssh/id_rsa\")>0) print line}'",
    "env awk 'BEGIN{system(\"id\")}'",
])
def test_awk_exec_and_io_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "awk '{print $1}'",
    "awk '{print $1, $3}'",
    "awk -F: '{print $1}' file.txt",
    "awk 'NR==1{print}' file.txt",
    "awk '{sum += $2} END{print sum}' data.txt",
])
def test_awk_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-2: tar external-program options ───────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "tar -cf /dev/null --checkpoint=1 --checkpoint-action=exec=id .",
    "tar --checkpoint-action=exec=sh\\ -c\\ id -cf out.tar .",
    "tar --to-command=id -xf a.tar",
    "tar --use-compress-program=/tmp/evil -xf a.tar",
    "tar -I /tmp/evil -xf a.tar",
    "tar --rmt-command=/tmp/evil -xf a.tar",
    "gtar --checkpoint-action=exec=id -cf out.tar .",
    "bsdtar --use-compress-program=/tmp/evil -xf a.tar",
])
def test_tar_exec_options_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "tar -xzf archive.tgz",
    "tar -czf archive.tgz dir",
    "tar -tvf archive.tar",
    "tar -xf archive.tar -C dest",
    "tar --exclude=node_modules -czf out.tgz src",
])
def test_tar_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-2: sed exec / file-IO / in-place ──────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "sed 's/a/b/e' file",          # s///e — runs replacement as a shell command
    "sed 'e id' file",             # e — execute
    "sed '1e id' file",            # address-prefixed e
    "sed 'w /etc/passwd' file",    # write command
    "sed '5,10w /tmp/out' file",   # addressed write
    "sed 'r /etc/shadow' file",    # read command
    "sed -i 's/a/b/' file",        # in-place edit
    "sed --in-place 's/a/b/' file",
])
def test_sed_exec_and_io_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "sed 's/a/b/'",
    "sed 's/a/b/g' file",
    "sed -n '1,5p' file",
    "sed 's/were/was/' file",      # 'w' inside content must NOT false-positive
    "sed 's/9e/x/' file",          # 'e' inside content must NOT false-positive
    "sed 'y/abc/xyz/' file",
    "sed -e 's/a/b/' -e 's/c/d/' file",
])
def test_sed_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-2: make arbitrary makefile / recipe ───────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "make -f /tmp/EvilMakefile",
    "make --file=/tmp/EvilMakefile",
    "make --makefile=/tmp/EvilMakefile all",
    "gmake -f /tmp/EvilMakefile",
])
def test_make_exec_options_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "make",
    "make all",
    "make build",
    "make -j4",
    "make install PREFIX=/usr/local",
])
def test_make_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-2 residual: sed `/regex/`-ADDRESSED exec / file-IO (Wave-2 C') ─────────


@pytest.mark.parametrize("cmd", [
    "sed '/foo/e' file",             # regex-addressed execute
    "sed '/foo/e id' file",          # regex-addressed execute with command
    "sed '/foo/w /etc/passwd' file", # regex-addressed write
    "sed '/foo/W /tmp/out' file",    # regex-addressed write-first-line
    "sed '/foo/r /etc/shadow' file", # regex-addressed read
    "sed '/foo/R /etc/shadow' file", # regex-addressed read-one-line
    "sed '1,/foo/w /tmp/out' file",  # line..regex range write
    "sed '/a/,/b/W /tmp/out' file",  # regex..regex range write
    "sed '/foo/!e id' file",         # negated regex-addressed execute
    "sed 's/x/y/; /foo/w /tmp/out' file",  # compound: safe subst then addressed write
])
def test_sed_regex_addressed_exec_io_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "sed '/foo/d' file",             # regex-addressed delete — safe
    "sed '/foo/p' file",             # regex-addressed print — safe
    "sed -n '/foo/p' file",
    "sed '/start/,/end/d' file",     # range delete — safe
    "sed '/foo/{s/a/b/}' file",      # addressed substitution block — safe
    "sed 's/write/read/' file",      # 'w'/'r' inside content must NOT false-positive
    "sed 's/reader/x/g' file",
]
)
def test_sed_regex_addressed_benign_allowed(cmd):
    _assert_allowed(cmd)


# ── F-C-3: gh credential-exfiltration subcommands ─────────────────────────────


@pytest.mark.parametrize("cmd", [
    "gh auth token",
    "gh auth status --show-token",
    "gh auth status -t",
    "gh auth login",
    "gh auth login --with-token",
    "env gh auth token",
    "sudo gh auth token",
])
def test_gh_credential_exfil_blocked(cmd):
    _assert_blocked(cmd)


@pytest.mark.parametrize("cmd", [
    "gh auth status",                # status without --show-token is fine
    "gh pr list",
    "gh pr create --title x --body y",
    "gh issue list",
    "gh repo view",
    "gh api /user",
    "gh run list --limit 1",
])
def test_gh_benign_usage_allowed(cmd):
    _assert_allowed(cmd)


# ── End-to-end: the handler denies before dispatch (no execution) ─────────────


@pytest.mark.asyncio
async def test_run_command_handler_blocks_git_config_rce():
    from mcp_server.tools.shell import handle_run_command
    result = await handle_run_command(
        {"cmd": "git -c 'alias.pwn=!echo PWNED' pwn", "timeout": 5}
    )
    assert "BLOCKED" in result
    assert "PWNED" not in result  # never executed


@pytest.mark.asyncio
async def test_run_command_handler_blocks_awk_system():
    from mcp_server.tools.shell import handle_run_command
    result = await handle_run_command(
        {"cmd": "awk 'BEGIN{system(\"echo PWNED\")}'", "timeout": 5}
    )
    assert "BLOCKED" in result
    assert "PWNED" not in result
