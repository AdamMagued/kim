import unittest
from unittest.mock import patch

from orchestrator.providers.ollama import (
    DEFAULT_OLLAMA_CLOUD_MODEL,
    OllamaProvider,
    _normalize_tool_arguments,
    _parse_num_ctx,
    _parse_ollama_ps_context,
)


class OllamaProviderTests(unittest.TestCase):
    def test_parse_num_ctx_from_parameters_block(self):
        raw = """
        temperature 0.2
        num_ctx 65536
        top_p 0.9
        """
        self.assertEqual(_parse_num_ctx(raw), 65536)

    def test_parse_num_ctx_from_modelfile_style_text(self):
        raw = "PARAMETER num_ctx 32768\nPARAMETER temperature 0.1"
        self.assertEqual(_parse_num_ctx(raw), 32768)

    def test_parse_ollama_ps_context_for_target_model(self):
        stdout = """NAME                    ID              SIZE      PROCESSOR    UNTIL              CONTEXT
gpt-oss:120b-cloud      abc123          73 GB     100% GPU     4 minutes from now 65536
llama3.2:latest         def456          2.0 GB    100% GPU     4 minutes from now 8192
"""
        self.assertEqual(_parse_ollama_ps_context(stdout, "gpt-oss:120b-cloud"), 65536)
        self.assertEqual(_parse_ollama_ps_context(stdout, "llama3.2:latest"), 8192)

    def test_normalize_tool_arguments_accepts_dict_and_json_string(self):
        self.assertEqual(_normalize_tool_arguments({"path": "a.txt"}), {"path": "a.txt"})
        self.assertEqual(_normalize_tool_arguments('{"path":"a.txt"}'), {"path": "a.txt"})
        self.assertEqual(_normalize_tool_arguments("not-json"), {})

    def test_provider_prefers_env_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "KIM_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "KIM_OLLAMA_MODE": "cloud",
                "KIM_OLLAMA_CLOUD_MODEL": DEFAULT_OLLAMA_CLOUD_MODEL,
            },
            clear=False,
        ):
            provider = OllamaProvider({"ollama": {"base_url": "http://localhost:11434", "mode": "local"}})
        self.assertEqual(provider._base_url, "http://127.0.0.1:11434")
        self.assertEqual(provider._mode, "cloud")
        self.assertEqual(provider._cloud_model, DEFAULT_OLLAMA_CLOUD_MODEL)


if __name__ == "__main__":
    unittest.main()
