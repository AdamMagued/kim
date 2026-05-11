import sys
from tray.voice import MayaVoiceProvider

def test_maya_provider():
    # We just want to make sure we don't have syntax errors or issues
    # loading the module and that the parameters are accepted correctly.
    # We won't actually try to initialize it as it requires specific hardware/deps.
    provider = MayaVoiceProvider()
    print("Provider created:", provider.name)
    assert provider.name == "Maya-1"
    assert provider._model_id == "maya-research/maya1"

if __name__ == "__main__":
    test_maya_provider()
    print("Test passed.")
