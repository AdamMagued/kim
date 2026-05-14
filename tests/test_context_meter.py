from orchestrator.context_meter import (
    ContextMeter,
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    context_phase,
    coerce_budget,
    estimate_request_tokens,
    estimate_text_tokens,
)


def test_budget_defaults_and_phase_thresholds():
    assert coerce_budget(None) == DEFAULT_CONTEXT_BUDGET_TOKENS
    assert coerce_budget(-1) == DEFAULT_CONTEXT_BUDGET_TOKENS
    assert context_phase(79, 100) == "ok"
    assert context_phase(80, 100) == "warn"
    assert context_phase(94, 100) == "warn"
    assert context_phase(95, 100) == "critical"
    assert context_phase(150, 100) == "critical"


def test_estimate_request_tokens_counts_system_tools_text_and_images():
    messages = [
        {"role": "user", "content": "hello world"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "data": "abc", "media_type": "image/png"},
            ],
        },
    ]
    estimated = estimate_request_tokens(messages, system="system prompt", tools=[{"name": "read_file"}])
    assert estimated > estimate_text_tokens("hello world")
    assert estimated >= 1500


def test_meter_prefers_exact_usage_and_emits_context_line():
    meter = ContextMeter(budget=100)
    snap = meter.observe_usage({"input": 25, "output": 5, "source": "api"})
    assert snap is not None
    assert snap.cumulative_input == 25
    assert snap.last_output == 5
    assert snap.phase == "ok"
    assert "[CONTEXT] cumulative_input=25 budget=100" in snap.to_log_line()
    assert "estimate=0" in snap.to_log_line()


def test_meter_uses_fallback_as_estimate_when_usage_missing():
    meter = ContextMeter(budget=100)
    snap = meter.observe_usage({}, fallback_input_tokens=82, source="browser")
    assert snap is not None
    assert snap.cumulative_input == 82
    assert snap.phase == "warn"
    assert snap.estimated is True
    assert "source=browser" in snap.to_log_line()
    assert "estimate=1" in snap.to_log_line()


def test_meter_loads_existing_metadata():
    meter = ContextMeter.from_metadata({"cumulative_input": 96, "last_compact_at": "x"}, budget=100)
    snap = meter.snapshot()
    assert snap.phase == "critical"
    assert snap.last_compact_at == "x"


def test_meter_coerces_invalid_metadata_input_to_zero():
    meter = ContextMeter.from_metadata({"cumulative_input": "not-an-int"}, budget=100)
    snap = meter.snapshot()
    assert snap.cumulative_input == 0
    assert snap.phase == "ok"
