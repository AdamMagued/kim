import pytest
from unittest.mock import patch, MagicMock
import sys

from tray.voice import MayaVoiceProvider, HttpVoiceProvider, KokoroVoiceProvider

def test_maya_voice_provider_import_error():
    provider = MayaVoiceProvider()

    with patch.object(provider, 'initialize', return_value=True):
        # We need to ensure torch is NOT imported so we can mock it raising an ImportError
        # If torch is already in sys.modules, we temporarily remove it to simulate the absence of the module
        with patch.dict(sys.modules, {'torch': None}):
            assert provider.speak_sync("Hello") is False

def test_maya_voice_provider_general_error():
    provider = MayaVoiceProvider()

    with patch.object(provider, 'initialize', return_value=True):
        # We patch an internal method that's called inside the try block to raise Exception
        with patch.object(provider, '_prepare_prompt', side_effect=Exception("Mocked error")):
            assert provider.speak_sync("Hello") is False

def test_http_voice_provider_import_error():
    provider = HttpVoiceProvider()

    with patch.dict(sys.modules, {'urllib.request': None}):
        assert provider.speak_sync("Hello") is False

def test_http_voice_provider_general_error():
    provider = HttpVoiceProvider()

    # We patch the built-in urllib.request.urlopen that gets imported locally in speak_sync
    with patch('urllib.request.urlopen', side_effect=Exception("Mocked HTTP error")):
        assert provider.speak_sync("Hello") is False

def test_kokoro_voice_provider_import_error():
    provider = KokoroVoiceProvider()

    with patch.object(provider, 'initialize', return_value=True):
        with patch.dict(sys.modules, {'sounddevice': None}):
            assert provider.speak_sync("Hello") is False

def test_kokoro_voice_provider_general_error():
    provider = KokoroVoiceProvider()

    with patch.object(provider, 'initialize', return_value=True):
        # Setting pipeline to something that raises an exception when called
        provider._pipeline = MagicMock(side_effect=Exception("Mocked Kokoro pipeline error"))
        assert provider.speak_sync("Hello") is False
