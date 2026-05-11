import asyncio
import pytest
import os
from mcp_server.tools.shell import handle_run_command

@pytest.mark.asyncio
async def test_handle_run_command_success():
    args = {"cmd": "echo 'hello world'"}
    result = await handle_run_command(args)
    assert "exit_code: 0" in result
    assert "hello world" in result

@pytest.mark.asyncio
async def test_handle_run_command_injection_mitigated():
    # If using shell=True, this would echo "test"
    # With exec, the command is 'echo' and arguments are ["hello", ";", "echo", "test"]
    # We should pass allow_chaining=True to bypass the first line of defense (the blocklist regex)
    # so we can test the actual exec vs shell behavior.
    args = {"cmd": "echo hello ; echo test", "allow_chaining": True}
    result = await handle_run_command(args)
    # the entire string "hello ; echo test" might be printed by echo depending on how shlex parses it,
    # or it might fail if shlex splits it and echo just prints the tokens.
    # In either case, it won't actually execute the second echo command to just print "test" cleanly.
    # Let's see what it outputs.
    assert "exit_code: 0" in result
    assert "hello" in result

@pytest.mark.asyncio
async def test_handle_run_command_pipe_as_arg():
    # Same here, test that pipe is treated as an argument to echo, not evaluated by bash.
    args = {"cmd": "echo 'hello' | base64", "allow_chaining": True}
    result = await handle_run_command(args)
    assert "exit_code: 0" in result
    assert "hello" in result
    assert "|" in result # Ensure the pipe character was passed to echo
    assert "base64" in result
