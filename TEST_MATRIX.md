# Test Matrix

Every test file, what it covers, how to run it, and its baseline status.

---

## Quick reference

```bash
# Run all individual tests (recommended on Windows)
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_browser_protocol.py -v

# Run main suite (crashes on Windows — see known issues)
python -m tests.kim_test_suite
```

## Test inventory

| File | Tests | Covers | Baseline | Notes |
|------|-------|--------|----------|-------|
| `tests/test_browser_protocol.py` | 2 | Stdout protocol parsing (`[STATUS]`, `[TOOL]`, etc.) | PASS | Critical contract test |
| `tests/test_browser_provider_parse.py` | 1 | Browser provider response parsing | SKIP | Requires playwright |
| `tests/test_context_meter.py` | 2 | Context window token counting | PASS | |
| `tests/test_interaction_policy.py` | 2 | Tool execution safety policy | PASS | |
| `tests/test_ollama_provider.py` | 1 | Ollama provider response normalization | PASS | |
| `tests/test_web_resolver.py` | 2 | Web tool URL resolution | PASS | |
| `tests/test_web_wait_for_url.py` | 1 | Web tool URL wait condition | PASS | |
| `tests/test_gemini_oauth_provider.py` | ~3 | Gemini OAuth token handling | PASS (expected) | |
| `tests/test_gemini_user_project_mode.py` | ~5 | Gemini user/project config | PASS (expected) | |
| `tests/test_github_create_repo.py` | ~4 | GitHub repo creation | PASS (expected) | |
| `tests/kim_test_suite.py` | ~20 | Full integration suite | CRASH | Windows cp1252 encoding bug |
| `tests/claw_test_suite.py` | ~20 | CLI test suite | Not run | Separate toolchain |

## Known issues (pre-existing)

1. **`kim_test_suite.py` crashes on Windows** — `print("─" * 100)` uses Unicode box-drawing characters that cp1252 can't encode. Not a code bug; it's a test output formatting issue.
2. **`yaml` module not installed** — `ModuleNotFoundError: No module named 'yaml'`. Install with `pip install pyyaml`.
3. **`playwright` not installed** — `test_browser_provider_parse.py` skips. Install with `pip install playwright && python -m playwright install`.

## When to run tests

- **Before any commit**: `python -m pytest tests/ -v`
- **After splitting a file**: run the specific tests that cover the split module
- **After changing stdout protocol**: `python -m pytest tests/test_browser_protocol.py -v`
- **After changing provider logic**: `python -m pytest tests/test_ollama_provider.py tests/test_browser_provider_parse.py -v`

## Adding new tests

- Place in `tests/` directory
- Name: `test_<module_name>.py`
- Must be runnable with `python -m pytest tests/test_<name>.py -v`
- Must not require external services (mock or skip if needed)
