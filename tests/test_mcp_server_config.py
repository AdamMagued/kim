import pytest
from pathlib import Path
from unittest.mock import patch

from mcp_server.config import validate_path

@pytest.fixture
def mock_env(monkeypatch):
    mock_root = Path("/mock/project_root").resolve()
    mock_home = Path("/mock/home").resolve()

    # Allowed paths
    monkeypatch.setattr("mcp_server.config.PROJECT_ROOT", mock_root)
    monkeypatch.setattr("mcp_server.config.ALLOWED_PATHS", [mock_root, Path("/mock/allowed").resolve()])

    # Sensitive paths
    monkeypatch.setattr("mcp_server.config._HOME", mock_home)
    monkeypatch.setattr("mcp_server.config._SENSITIVE_PATHS", [mock_home / ".ssh", mock_home / ".aws"])

    return mock_root, mock_home

def test_validate_path_valid_relative(mock_env):
    """Test that relative paths are resolved relative to PROJECT_ROOT."""
    mock_root, _ = mock_env
    result = validate_path("src/main.py")
    assert result == mock_root / "src/main.py"

def test_validate_path_valid_absolute(mock_env):
    """Test that valid absolute paths are returned correctly."""
    mock_root, _ = mock_env
    result = validate_path("/mock/project_root/src/main.py")
    assert result == mock_root / "src/main.py"

def test_validate_path_valid_other_allowed(mock_env):
    """Test that paths in other ALLOWED_PATHS are valid."""
    result = validate_path("/mock/allowed/data.txt")
    assert result == Path("/mock/allowed/data.txt").resolve()

def test_validate_path_outside_allowed(mock_env):
    """Test that paths outside ALLOWED_PATHS raise PermissionError."""
    with pytest.raises(PermissionError, match="outside allowed directories"):
        validate_path("/outside/path.txt")

def test_validate_path_relative_traversal_outside(mock_env):
    """Test that directory traversal outside ALLOWED_PATHS raises PermissionError."""
    with pytest.raises(PermissionError, match="outside allowed directories"):
        validate_path("../../../etc/passwd")

def test_validate_path_sensitive_directory(mock_env, monkeypatch):
    """Test that accessing sensitive directories raises PermissionError."""
    mock_root, mock_home = mock_env
    monkeypatch.setattr("mcp_server.config.ALLOWED_PATHS", [mock_root, mock_home])

    with pytest.raises(PermissionError, match="inside sensitive directory.*access denied"):
        validate_path("/mock/home/.ssh/id_rsa")

def test_validate_path_dotenv_in_home(mock_env, monkeypatch):
    """Test that accessing .env files in home directory raises PermissionError."""
    mock_root, mock_home = mock_env
    monkeypatch.setattr("mcp_server.config.ALLOWED_PATHS", [mock_root, mock_home])

    with pytest.raises(PermissionError, match="dotenv file in the home directory.*access denied"):
        validate_path("/mock/home/.env.local")

def test_validate_path_expanduser(mock_env, monkeypatch):
    """Test that validate_path expands the user directory (~) properly."""
    mock_root, mock_home = mock_env
    monkeypatch.setattr("mcp_server.config.ALLOWED_PATHS", [mock_root, mock_home])

    # Python's expanduser reads the HOME env variable on POSIX systems
    monkeypatch.setenv("HOME", str(mock_home))
    monkeypatch.setenv("USERPROFILE", str(mock_home))  # For Windows just in case

    result = validate_path("~/workspace/file.txt")
    assert result == mock_home / "workspace/file.txt"
