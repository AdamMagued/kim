"""
DeepSeek provider.

DeepSeek exposes an OpenAI-compatible API, so this subclasses OpenAIProvider
and just changes the base URL, API key env var, and default model.
"""

import logging
import os

from orchestrator.providers.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(OpenAIProvider):
    _BASE_URL = _DEEPSEEK_BASE_URL

    def __init__(self, config: dict):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise EnvironmentError("DEEPSEEK_API_KEY is not set")

        # Initialize the client directly without polluting OPENAI_API_KEY (#18)
        import openai
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=_DEEPSEEK_BASE_URL,
        )

        models = config.get("model", {})
        self._model = models.get("deepseek", "deepseek-chat")
        self._max_tokens = int(config.get("max_tokens", 4096))
        logger.info(f"DeepSeekProvider: model={self._model}")
