import pytest
import builtins
from unittest.mock import patch, MagicMock

from tray.voice import (
    HttpVoiceProvider,
    HumeVoiceProvider,
    MayaVoiceProvider,
    KokoroVoiceProvider,
    VoiceEngine,
    VoiceStatus,
)


@patch("urllib.request.urlopen")
def test_http_voice_provider_error_handling(mock_urlopen):
    original_import = builtins.__import__
    def mock_import(name, *args, **kwargs):
        if name in ('sounddevice', 'numpy'):
            return MagicMock()
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', side_effect=mock_import):
        mock_urlopen.side_effect = Exception("HTTP connection failed")

        provider = HttpVoiceProvider(url="http://localhost:8880", model="test", voice="test")
        result = provider.speak_sync("Hello World")

        assert result is False
        mock_urlopen.assert_called_once()


@patch("urllib.request.urlopen")
def test_hume_voice_provider_error_handling(mock_urlopen):
    original_import = builtins.__import__
    def mock_import(name, *args, **kwargs):
        if name in ('sounddevice', 'numpy'):
            return MagicMock()
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', side_effect=mock_import):
        mock_urlopen.side_effect = Exception("Hume API error")

        provider = HumeVoiceProvider(voice_name="Ava", description="Test", config_dict={})
        with patch.object(provider, "initialize", return_value=True):
            result = provider.speak_sync("Hello World")

        assert result is False
        mock_urlopen.assert_called_once()


def test_maya_voice_provider_import_error_handling():
    original_import = builtins.__import__
    def side_effect(name, *args, **kwargs):
        if name in ('torch', 'sounddevice', 'numpy', 'snac'):
            raise Exception(f"Mocked exception for {name}")
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', side_effect=side_effect):
        provider = MayaVoiceProvider()
        with patch.object(provider, "initialize", return_value=True):
            result = provider.speak_sync("Hello World")

    assert result is False


def test_kokoro_voice_provider_error_handling():
    original_import = builtins.__import__
    def side_effect(name, *args, **kwargs):
        if name == 'sounddevice':
            raise Exception("Mocked sounddevice exception")
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', side_effect=side_effect):
        provider = KokoroVoiceProvider()
        with patch.object(provider, "initialize", return_value=True):
            result = provider.speak_sync("Hello World")

    assert result is False


def test_voice_engine_fallback_error_handling():
    config = {
        "voice": {
            "enabled": True,
            "engine": "test_mock",
        }
    }

    engine = VoiceEngine(config)

    class FaultyProvider:
        name = "Faulty"
        def initialize(self): return True
        def speak_sync(self, text): raise Exception("Faulty provider crashed!")
        def shutdown(self): pass

    class WorkingProvider:
        name = "Working"
        def initialize(self): return True
        def speak_sync(self, text):
            self.called = True
            return True
        def shutdown(self): pass

    working_provider = WorkingProvider()
    working_provider.called = False

    engine._provider = FaultyProvider()
    engine._fallback_chain = [engine._provider, working_provider]

    engine._speak_sync("Fallback test")

    assert working_provider.called is True
