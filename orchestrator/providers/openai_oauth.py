"""
OpenAI-OAuth provider.

Talks to a locally-running `openai-oauth` dev proxy
(https://github.com/EvanZhouDev/openai-oauth), which exposes an
OpenAI-compatible endpoint backed by the Codex CLI's OAuth session in
`~/.codex/auth.json` instead of an API key.

Because the proxy is OpenAI-compatible this subclasses OpenAIProvider and only
changes the base URL, the model config key, and the "are you signed in?"
preflight. The proxy takes no API key; OpenAIProvider already treats a
token-less *loopback* base URL as a legitimate no-key case, so no placeholder
warning fires.

config.yaml:
    openai_oauth_base_url: "http://127.0.0.1:10531/v1"   # optional
    model:
      openai_oauth: "gpt-5.6-terra"

Start the proxy with `npx openai-oauth@latest` (and `npx openai-oauth login`
once, if the Codex CLI is not already signed in).
"""

from __future__ import annotations

import logging
from pathlib import Path

from orchestrator.providers.openai_provider import OpenAIProvider, _default_token_param

logger = logging.getLogger(__name__)

#: Default endpoint the `openai-oauth` proxy binds to (its `--host`/`--port` defaults).
DEFAULT_OPENAI_OAUTH_BASE_URL = "http://127.0.0.1:10531/v1"

#: Codex-supported model. The proxy only serves models Codex supports, and it
#: auto-discovers them — override via `model.openai_oauth` in config.yaml.
DEFAULT_OPENAI_OAUTH_MODEL = "gpt-5.6-terra"

#: Where the Codex CLI (and `openai-oauth login`) stores the OAuth session.
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


class OpenAIOAuthProvider(OpenAIProvider):
    """OpenAI-compatible provider backed by the local `openai-oauth` proxy."""

    def __init__(self, config: dict):
        if not CODEX_AUTH_PATH.exists():
            raise EnvironmentError(
                f"No Codex OAuth session at {CODEX_AUTH_PATH}. Sign in once with "
                "`npx openai-oauth login`, then start the proxy with "
                "`npx openai-oauth@latest`."
            )

        # Instance attribute, not a class constant: the proxy's host/port are
        # configurable (`--host`/`--port`), so the base URL must stay
        # overridable. OpenAIProvider.__init__ reads `self._BASE_URL`, and a
        # non-None value there also suppresses the OPENAI_API_KEY requirement.
        self._BASE_URL = (
            config.get("openai_oauth_base_url") or DEFAULT_OPENAI_OAUTH_BASE_URL
        )

        super().__init__(config)

        # Parent resolved `model.openai` — this provider keys off its own entry.
        models = config.get("model", {})
        self._model = models.get("openai_oauth", DEFAULT_OPENAI_OAUTH_MODEL)
        self._token_param = _default_token_param(self._model)
        logger.info(
            f"OpenAIOAuthProvider: model={self._model} base_url={self._BASE_URL}"
        )
