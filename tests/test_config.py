import pytest
from pathlib import Path
from mcp_server import config
from mcp_server.config import validate_path

@pytest.fixture
def mock_env(tmp_path, monkeypatch):
    """
    Setup a controlled environment for testing path validation using monkeypatch.
    This prevents tests from depending on the actual project or home directories.
    """
    home = tmp_path / "home"
    project_root = home / "project"
    project_root.mkdir(parents=True)

    other_allowed = home / "other"
    other_allowed.mkdir()

    ssh_dir = home / ".ssh"
    ssh_dir.mkdir()

    monkeypatch.setattr(config, "_HOME", home)
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    # Simulate an environment where we have multiple allowed paths
    monkeypatch.setattr(config, "ALLOWED_PATHS", [project_root, other_allowed])
    monkeypatch.setattr(config, "_SENSITIVE_PATHS", [ssh_dir])

    # Ensure relative path resolutions don't depend on where the test is run from
    monkeypatch.chdir(project_root)

    return {
        "home": home,
        "project_root": project_root,
        "other_allowed": other_allowed,
        "ssh_dir": ssh_dir,
        "tmp_path": tmp_path
    }

def test_validate_path_allowed_relative(mock_env):
    """Relative paths should resolve within PROJECT_ROOT."""
    p = validate_path("some_file.txt")
    assert p == mock_env["project_root"] / "some_file.txt"

def test_validate_path_allowed_absolute(mock_env):
    """Absolute paths within PROJECT_ROOT should be allowed."""
    abs_path = mock_env["project_root"] / "dir" / "file.txt"
    p = validate_path(str(abs_path))
    assert p == abs_path

def test_validate_path_other_allowed(mock_env):
    """Absolute paths within other ALLOWED_PATHS should be allowed."""
    abs_path = mock_env["other_allowed"] / "file.txt"
    p = validate_path(str(abs_path))
    assert p == abs_path

def test_validate_path_outside_allowed(mock_env):
    """Paths completely outside allowed directories should raise PermissionError."""
    outside_path = mock_env["tmp_path"] / "outside.txt"
    with pytest.raises(PermissionError, match="outside allowed directories"):
        validate_path(str(outside_path))

def test_validate_path_relative_outside(mock_env):
    """Relative paths escaping PROJECT_ROOT and ALLOWED_PATHS should fail."""
    with pytest.raises(PermissionError, match="outside allowed directories"):
        validate_path("../outside.txt")

def test_validate_path_sensitive(mock_env):
    """Paths inside sensitive directories should be denied, even if allowed generally."""
    sensitive_path = mock_env["ssh_dir"] / "id_rsa"

    # First, make sure the sensitive directory is actually within ALLOWED_PATHS
    # Otherwise it fails the "outside allowed" check before the "sensitive" check
    config.ALLOWED_PATHS.append(mock_env["ssh_dir"])

    with pytest.raises(PermissionError, match="inside sensitive directory"):
        validate_path(str(sensitive_path))

def test_validate_path_home_dotenv(mock_env):
    """Dotenv files directly in the home directory should be denied."""
    dotenv_path = mock_env["home"] / ".env"

    # Needs to be within allowed paths to test the dotenv check
    config.ALLOWED_PATHS.append(mock_env["home"])

    with pytest.raises(PermissionError, match="dotenv file in the home directory"):
        validate_path(str(dotenv_path))

    dotenv_local = mock_env["home"] / ".env.local"
    with pytest.raises(PermissionError, match="dotenv file in the home directory"):
        validate_path(str(dotenv_local))

    # Verify non-dotenv file in home is fine
    regular_file = mock_env["home"] / "regular.txt"
    p = validate_path(str(regular_file))
    assert p == regular_file
