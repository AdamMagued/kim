from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools import web


@pytest.mark.asyncio
async def test_text_with_selector_punctuation_uses_text_lookup():
    page = MagicMock()
    text_match = MagicMock()
    text_match.first.wait_for = AsyncMock()
    page.get_by_text.return_value = text_match

    with patch.object(web, "_page", AsyncMock(return_value=page)):
        result = await web.handle_web_wait_for(
            {"text": "Upload complete.", "timeout_ms": 50}
        )

    page.get_by_text.assert_called_once_with("Upload complete.", exact=False)
    page.locator.assert_not_called()
    assert result == "Text appeared: 'Upload complete.'"


@pytest.mark.asyncio
async def test_explicit_selector_uses_locator():
    page = MagicMock()
    selector_match = MagicMock()
    selector_match.first.wait_for = AsyncMock()
    page.locator.return_value = selector_match

    with patch.object(web, "_page", AsyncMock(return_value=page)):
        result = await web.handle_web_wait_for(
            {"selector": ".finished", "timeout_ms": 50}
        )

    page.locator.assert_called_once_with(".finished")
    page.get_by_text.assert_not_called()
    assert result == "Selector matched: .finished"
