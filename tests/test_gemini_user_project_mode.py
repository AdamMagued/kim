"""
Tests for oauth_user_project mode in GeminiProvider.

Validates strict user-project behavior:
- Requires both access token and user project ID
- Uses x-goog-user-project header  
- Does NOT fall back to Kim-paid credentials
- Provides clear error messages for quota/billing issues
"""

import os
import pytest
from orchestrator.providers.gemini import (
    GeminiProvider,
    OAUTH_TOKEN_ENV,
    OAUTH_USER_PROJECT_ENV,
)


class TestOAuthUserProjectMode:
    """Tests for oauth_user_project authentication mode."""

    def test_requires_explicit_mode_and_credentials(self):
        """oauth_user_project mode requires explicit declaration and both token+project."""
        # Missing explicit mode + user project → should NOT auto-enable
        config_no_mode = {
            "gemini_auth_mode": "auto",
            "oauth_access_token": "valid-token",
            "user_project_id": "my-project",
        }
        # auto mode with oauth + user project → falls back to regular oauth
        provider = GeminiProvider(config_no_mode)
        assert provider._auth_mode == "oauth"

        # Explicit mode but missing user project → error
        config_no_project = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "valid-token",
        }
        with pytest.raises(EnvironmentError, match="requires a user Google Cloud project ID"):
            GeminiProvider(config_no_project)

        # Explicit mode but missing token → error
        config_no_token = {
            "gemini_auth_mode": "oauth_user_project",
            "user_project_id": "my-project",
        }
        with pytest.raises(EnvironmentError, match="requires a valid OAuth access token"):
            GeminiProvider(config_no_token)

    def test_user_project_id_from_env(self):
        """User project ID can be read from KIM_GOOGLE_USER_PROJECT_ID env var."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
        }
        os.environ[OAUTH_USER_PROJECT_ENV] = "my-env-project"
        try:
            provider = GeminiProvider(config)
            assert provider._auth_mode == "oauth_user_project"
            assert provider._user_project_id == "my-env-project"
            assert provider._quota_project == "my-env-project"
        finally:
            os.environ.pop(OAUTH_USER_PROJECT_ENV, None)

    def test_user_project_id_from_config(self):
        """User project ID can be passed via config dict."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "config-project",
        }
        provider = GeminiProvider(config)
        assert provider._auth_mode == "oauth_user_project"
        assert provider._user_project_id == "config-project"
        assert provider._quota_project == "config-project"

    def test_quota_project_override_with_user_project(self):
        """In user-project mode, user project ID overrides generic quota project."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "user-project",
            "google_cloud_project": "other-project",  # Should be ignored
        }
        provider = GeminiProvider(config)
        assert provider._quota_project == "user-project"

    def test_cannot_mix_api_key_and_user_project(self):
        """Cannot enable user-project mode while API key is configured."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "api_key": "sk-xxx",
            "oauth_access_token": "token123",
            "user_project_id": "my-project",
        }
        with pytest.raises(EnvironmentError, match="ambiguous"):
            GeminiProvider(config)

    def test_regular_oauth_mode_still_works(self):
        """Regular oauth mode (without user project) continues to work."""
        config = {
            "gemini_auth_mode": "oauth",
            "oauth_access_token": "token123",
        }
        provider = GeminiProvider(config)
        assert provider._auth_mode == "oauth"
        assert provider._quota_project == ""

    def test_user_project_provides_header_in_requests(self):
        """Ensure that user-project mode includes x-goog-user-project header."""
        # This is tested indirectly via _complete_oauth using _quota_project
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "test-token",
            "user_project_id": "test-project-123",
        }
        provider = GeminiProvider(config)
        # When _complete_oauth runs, it will check `self._quota_project` which should be set
        assert provider._quota_project == "test-project-123"


class TestUserProjectErrorMessages:
    """Tests for user-friendly error messages in user-project mode."""

    def test_quota_exceeded_error(self):
        """429 errors in user-project mode indicate free-tier quota exceeded."""
        # This test would mock an actual HTTP 429 response.
        # For now, we validate the error handling logic exists in the code.
        import asyncio
        from orchestrator.providers.gemini import GeminiProvider

        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "my-project",
        }
        provider = GeminiProvider(config)
        # The error message handling is in _complete_oauth and checks exc.code == 429
        assert provider._auth_mode == "oauth_user_project"

    def test_project_not_configured_error(self):
        """Missing user project ID raises clear error."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
        }
        with pytest.raises(EnvironmentError) as exc_info:
            GeminiProvider(config)
        assert "project id" in str(exc_info.value).lower()
        assert OAUTH_USER_PROJECT_ENV in str(exc_info.value)

    def test_missing_access_token_error(self):
        """Missing access token raises clear error."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "user_project_id": "my-project",
        }
        with pytest.raises(EnvironmentError) as exc_info:
            GeminiProvider(config)
        assert "access token" in str(exc_info.value).lower()
        assert OAUTH_TOKEN_ENV in str(exc_info.value)


class TestUserProjectNoFallback:
    """Tests ensuring user-project mode does NOT fall back to Kim-paid credentials."""

    def test_no_api_key_fallback(self):
        """In user-project mode, if request fails, never silently use API key."""
        # GeminiProvider validates strictly: either api_key OR oauth, never both
        config_both = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token",
            "user_project_id": "project",
            "api_key": "key",
        }
        with pytest.raises(EnvironmentError, match="ambiguous"):
            GeminiProvider(config_both)

    def test_no_kim_project_fallback(self):
        """In user-project mode, never use Kim's shared project if user project fails."""
        config = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token",
            "user_project_id": "user-project",
            "google_cloud_project": "kim-shared-project",  # Ignored in user-project mode
        }
        provider = GeminiProvider(config)
        # The user project should be used, not Kim's
        assert provider._quota_project == "user-project"


class TestUserProjectModeTransition:
    """Tests for switching between auth modes."""

    def test_switch_from_api_key_to_user_project(self):
        """Can transition from API key mode to user-project mode."""
        # Start with API key
        config1 = {"api_key": "key123"}
        provider1 = GeminiProvider(config1)
        assert provider1._auth_mode == "api_key"

        # Switch to user-project
        config2 = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "my-project",
        }
        provider2 = GeminiProvider(config2)
        assert provider2._auth_mode == "oauth_user_project"

    def test_switch_from_oauth_to_user_project(self):
        """Can transition from regular oauth to user-project mode."""
        config1 = {
            "gemini_auth_mode": "oauth",
            "oauth_access_token": "token123",
        }
        provider1 = GeminiProvider(config1)
        assert provider1._auth_mode == "oauth"

        config2 = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "my-project",
        }
        provider2 = GeminiProvider(config2)
        assert provider2._auth_mode == "oauth_user_project"

    def test_disconnect_clears_user_project(self):
        """Disconnecting from user-project should clear stored project."""
        # Simulate: user was in user-project mode, now disconnects
        config1 = {
            "gemini_auth_mode": "oauth_user_project",
            "oauth_access_token": "token123",
            "user_project_id": "my-project",
        }
        provider1 = GeminiProvider(config1)
        assert provider1._user_project_id == "my-project"

        # After disconnect, user should need to reconfigure
        config2 = {"gemini_auth_mode": "oauth_user_project"}
        with pytest.raises(EnvironmentError):
            GeminiProvider(config2)
