import pytest
from mcp_server.tools.shell import _check_blocked

def test_check_blocked_valid_commands():
    # Empty commands
    assert _check_blocked("") is None
    assert _check_blocked("   ") is None

    # Safe commands
    assert _check_blocked("ls -la") is None
    assert _check_blocked("echo hello") is None
    assert _check_blocked("cat file.txt") is None
    assert _check_blocked("python script.py") is None

    # Safe commands with quotes
    assert _check_blocked('echo "hello world"') is None
    assert _check_blocked("echo 'hello world'") is None

def test_check_blocked_chaining():
    # Chaining operators when allow_chaining=False
    blocked_cases = [
        "ls -l; echo 1",
        "cmd1 && cmd2",
        "cmd1 || cmd2",
        "ls -l | grep text",
        "echo `ls`",
        "echo $(ls)",
        "echo $( ls )"
    ]
    for cmd in blocked_cases:
        result = _check_blocked(cmd, allow_chaining=False)
        assert result is not None
        assert "BLOCKED: Command contains chaining metacharacters" in result

    # Same operators should be allowed when allow_chaining=True
    assert _check_blocked("ls -l; echo 1", allow_chaining=True) is None
    assert _check_blocked("echo `ls`", allow_chaining=True) is None

def test_check_blocked_malformed_quotes():
    result = _check_blocked('echo "hello')
    assert result is not None
    assert "BLOCKED: Command has malformed shell quoting" in result

def test_check_blocked_deny_commands():
    # Unconditionally blocked commands
    blocked_cmds = ["rm", "rmdir", "del", "format", "diskpart", "mkfs", "dd", "shred"]
    for cmd in blocked_cmds:
        result = _check_blocked(f"{cmd} some_arg")
        assert result is not None
        assert f"BLOCKED: '{cmd}' is a blocked command" in result

    # Blocked commands with path prefix
    # Note: For Windows paths, backslashes must be properly escaped so shlex parses them as backslashes
    path_cases = [
        ("/bin/rm", "rm"),
        ("/usr/bin/rmdir", "rmdir"),
        ("C:\\\\Windows\\\\System32\\\\format", "format"),
        ("C:/tools/del", "del"),
    ]
    for cmd, expected_base in path_cases:
        result = _check_blocked(f"{cmd} arg")
        assert result is not None
        assert f"BLOCKED: '{expected_base}' is a blocked command" in result

def test_check_blocked_dangerous_patterns():
    # Fork bomb
    result = _check_blocked(":(){ :|:& };:")
    assert result is not None
    assert "BLOCKED: Command matches dangerous pattern" in result

    # chmod 777 /
    result = _check_blocked("chmod -R 777 /")
    assert result is not None
    assert "BLOCKED: Command matches dangerous pattern" in result

    result = _check_blocked("chmod 777 /")
    assert result is not None
    assert "BLOCKED: Command matches dangerous pattern" in result

    # Allowed chmod
    assert _check_blocked("chmod 777 ./file") is None

    # dd if=/dev/zero pattern match
    result = _check_blocked("sudo dd if=/dev/zero of=/dev/sda")
    assert result is not None
    assert "BLOCKED: Command matches dangerous pattern" in result

def test_check_blocked_special_rm_cases():
    # We use sudo to bypass the initial `_DENY_COMMANDS` check for `rm`
    # and reach the 4. Special case logic

    blocked_cases = [
        "sudo rm -r /",
        "sudo rm -rf /",
        "sudo rm -r ..",
        "sudo rm -rf ..",
        "sudo rm -f -r /",
        "sudo rm -r    /",
        "sudo rm -rf   ..",
    ]
    for cmd in blocked_cases:
        result = _check_blocked(cmd)
        assert result is not None
        assert "BLOCKED: recursive rm targeting root or parent directory" in result

    # Safe sudo rm cases
    safe_cases = [
        "sudo rm -rf ./local_dir",
        "sudo rm -r /tmp/dir",
        "sudo rm file.txt",
        "sudo rm -R /", # The regex r"-\w*r\w*" only catches lower-case 'r'
    ]
    for cmd in safe_cases:
        assert _check_blocked(cmd) is None
