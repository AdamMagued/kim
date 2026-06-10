"""
Regression test: run_codex_subtask() must pass environment variables via
the subprocess `env=` dict parameter, NOT by mutating os.environ.

If someone refactors codex_bridge.py and accidentally uses os.environ[k] = v
instead of the local env dict, this test will break.
"""

import ast
import inspect
import textwrap
from pathlib import Path

# Locate the source file
_CODEX_BRIDGE = Path(__file__).resolve().parent.parent / "mcp_server" / "tools" / "codex_bridge.py"


def _get_function_source(source_text: str, func_name: str) -> str:
    """Extract the source of a top-level async function from source_text."""
    tree = ast.parse(source_text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = source_text.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise ValueError(f"Function {func_name!r} not found")


class TestCodexEnvScoping:
    """Verify env-var isolation in codex_bridge.run_codex_subtask."""

    def test_source_file_exists(self):
        assert _CODEX_BRIDGE.exists(), f"Expected codex_bridge.py at {_CODEX_BRIDGE}"

    def test_env_dict_is_passed_to_subprocess(self):
        """The subprocess must receive env= as a keyword argument."""
        source = _CODEX_BRIDGE.read_text(encoding="utf-8")
        func_src = _get_function_source(source, "run_codex_subtask")

        # The env dict must be constructed as a local variable
        assert "env = {" in func_src or "env={" in func_src, (
            "run_codex_subtask should build a local env dict via `env = {...}`"
        )

        # The env dict must be passed via env= keyword to create_subprocess_exec
        assert "env=env" in func_src, (
            "run_codex_subtask must pass `env=env` to create_subprocess_exec"
        )

    def test_no_os_environ_mutation(self):
        """run_codex_subtask must NOT mutate os.environ directly."""
        source = _CODEX_BRIDGE.read_text(encoding="utf-8")
        func_src = _get_function_source(source, "run_codex_subtask")

        # These patterns indicate direct os.environ mutation
        forbidden_patterns = [
            "os.environ[",        # os.environ["KEY"] = value
            "os.environ.update",  # os.environ.update({...})
            "os.putenv",          # os.putenv("KEY", value)
        ]
        for pattern in forbidden_patterns:
            assert pattern not in func_src, (
                f"run_codex_subtask must not use `{pattern}` — "
                "env vars should be scoped via the subprocess env= dict, "
                "not by mutating the global os.environ"
            )

    def test_env_dict_spreads_os_environ(self):
        """The local env dict should inherit from os.environ via spread."""
        source = _CODEX_BRIDGE.read_text(encoding="utf-8")
        func_src = _get_function_source(source, "run_codex_subtask")

        assert "**os.environ" in func_src, (
            "run_codex_subtask should spread os.environ into the local env dict "
            "so the subprocess inherits the parent environment"
        )

    def test_env_dict_sets_required_keys(self):
        """The env dict must include CODEX_HOME, CODEX_API_KEY, etc."""
        source = _CODEX_BRIDGE.read_text(encoding="utf-8")
        func_src = _get_function_source(source, "run_codex_subtask")

        required_keys = ["CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY"]
        for key in required_keys:
            assert f'"{key}"' in func_src or f"'{key}'" in func_src, (
                f"env dict must set {key}"
            )
