import pytest
from pathlib import Path

@pytest.fixture
def project_root():
    return Path(__file__).resolve().parent.parent

@pytest.fixture
def mock_config():
    return {
        "max_iterations": 25,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }
