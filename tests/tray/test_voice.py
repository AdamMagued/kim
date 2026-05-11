import pytest
from unittest.mock import patch, MagicMock
from tray.voice import (
    MayaVoiceProvider,
    KokoroVoiceProvider,
    HttpVoiceProvider,
    HumeVoiceProvider,
    VoiceEngine,
    VoiceStatus
)

class TestVoiceProviderErrors:
    # --- MayaVoiceProvider Tests ---
    @patch('tray.voice.MayaVoiceProvider._prepare_prompt')
    def test_maya_voice_provider_exception(self, mock_prepare_prompt):
        provider = MayaVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Mock inside try block to raise an Exception
        mock_prepare_prompt.side_effect = Exception("Test Maya Exception")
        result = provider.speak_sync("Hello")
        assert result is False

    def test_maya_voice_provider_import_error(self):
        provider = MayaVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Raise ImportError during `import torch` etc.
        with patch('builtins.__import__', side_effect=ImportError("mocked import error")):
            result = provider.speak_sync("Hello")
            assert result is False

    def test_maya_voice_provider_shutdown_exception(self):
        provider = MayaVoiceProvider()
        # Create dummy mocked objects to simulate actual run
        provider._model = MagicMock()
        with patch('builtins.__import__', side_effect=Exception("Test Shutdown Maya Exception")):
            # this hits the except block around line 494
            provider.shutdown()
            assert provider._available is None

    # --- KokoroVoiceProvider Tests ---
    def test_kokoro_voice_provider_import_error(self):
        provider = KokoroVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Raise ImportError during `import sounddevice`
        with patch('builtins.__import__', side_effect=ImportError("mocked import error")):
            result = provider.speak_sync("Hello")
            assert result is False

    def test_kokoro_voice_provider_exception(self):
        provider = KokoroVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Raise generic Exception during speak_sync
        with patch('builtins.__import__'):  # let imports pass but fail inside
            provider._pipeline = MagicMock(side_effect=Exception("Test Kokoro Exception"))
            result = provider.speak_sync("Hello")
            assert result is False

    # --- HttpVoiceProvider Tests ---
    def test_http_voice_provider_import_error(self):
        provider = HttpVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        with patch('builtins.__import__', side_effect=ImportError("mocked import error")):
            result = provider.speak_sync("Hello")
            assert result is False

    def test_http_voice_provider_exception(self):
        provider = HttpVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Raise Exception inside try block
        with patch('json.dumps', side_effect=Exception("Test Http Exception")):
            result = provider.speak_sync("Hello")
            assert result is False

    # --- HumeVoiceProvider Tests ---
    def test_hume_voice_provider_import_error(self):
        provider = HumeVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        with patch('builtins.__import__', side_effect=ImportError("mocked import error")):
            result = provider.speak_sync("Hello")
            assert result is False

    def test_hume_voice_provider_exception(self):
        provider = HumeVoiceProvider()
        provider.initialize = MagicMock(return_value=True)
        # Raise Exception inside try block
        with patch('json.dumps', side_effect=Exception("Test Hume Exception")):
            result = provider.speak_sync("Hello")
            assert result is False


class TestVoiceEngineErrors:
    def test_voice_engine_warm_up_exception(self):
        engine = VoiceEngine({"voice_enabled": True})
        engine._enabled = True
        engine._provider = MagicMock()
        engine._provider.name = "MockProvider"
        engine._provider.initialize.side_effect = Exception("Warm-up Exception")

        # We need a mock callback to capture the status message
        captured_messages = []
        def mock_callback(status, message):
            captured_messages.append((status, message))

        engine.set_status_callback(mock_callback)

        # Test warm_up_sync to directly run it
        engine._warm_up_sync()

        assert engine._status == VoiceStatus.FAILED
        assert any("Warm-up Exception" in msg for status, msg in captured_messages)

    def test_voice_engine_speak_sync_provider_exception(self):
        engine = VoiceEngine({"voice_enabled": True})
        engine._enabled = True

        mock_provider1 = MagicMock()
        mock_provider1.name = "Provider1"
        mock_provider1.speak_sync.side_effect = Exception("Speak Exception 1")

        mock_provider2 = MagicMock()
        mock_provider2.name = "Provider2"
        mock_provider2.speak_sync.return_value = True

        # Override the fallback chain
        engine._fallback_chain = [mock_provider1, mock_provider2]
        engine._provider = mock_provider1

        engine._speak_sync("Hello")

        # We expect provider1 to fail and throw an exception,
        # which VoiceEngine catches and proceeds to provider2
        mock_provider1.speak_sync.assert_called_once_with("Hello")
        mock_provider2.speak_sync.assert_called_once_with("Hello")

    def test_voice_engine_shutdown_exception(self):
        engine = VoiceEngine({"voice_enabled": True})

        mock_provider = MagicMock()
        mock_provider.name = "MockProvider"
        mock_provider.shutdown.side_effect = Exception("Shutdown Exception")

        engine._fallback_chain = [mock_provider]

        # Shutdown shouldn't crash if provider throws an exception
        try:
            engine.shutdown()
        except Exception as e:
            pytest.fail(f"shutdown() raised {e} unexpectedly!")

        mock_provider.shutdown.assert_called_once()

    def test_voice_engine_switch_engine_exception(self):
        engine = VoiceEngine({"voice_enabled": True})
        mock_provider = MagicMock()
        mock_provider.shutdown.side_effect = Exception("Shutdown Exception during switch")
        engine._fallback_chain = [mock_provider]

        # This shouldn't throw an error either
        try:
            # We don't need to patch _rebuild_providers because we want to test switch_engine exception handling
            engine.switch_engine("test", {"voice": {"engine": "test"}})
        except Exception as e:
            pytest.fail(f"switch_engine() raised {e} unexpectedly!")

        mock_provider.shutdown.assert_called_once()

    def test_voice_engine_set_status_exception(self):
        engine = VoiceEngine({"voice_enabled": True})

        def mock_callback(status, message):
            raise Exception("Callback exception")

        engine.set_status_callback(mock_callback)

        # Should catch the exception inside _set_status
        try:
            engine._set_status(VoiceStatus.READY, "Test Message")
        except Exception as e:
            pytest.fail(f"_set_status() raised {e} unexpectedly!")

    def test_voice_engine_speak_exception(self):
        engine = VoiceEngine({"voice_enabled": True})
        engine._enabled = True

        with patch.object(engine._executor, 'submit', side_effect=Exception("Submit exception")):
            try:
                # Need to use async loop to run async method
                import asyncio
                asyncio.run(engine.speak("Test async speak"))
            except Exception as e:
                pytest.fail(f"speak() raised {e} unexpectedly!")
