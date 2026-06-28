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


# ── Budget overflow edge cases ───────────────────────────────────────────────


def test_budget_overflow_single_large_input():
    """A single input exceeding the entire budget goes straight to critical."""
    meter = ContextMeter(budget=100)
    snap = meter.add_input(150, source="test", estimated=False)
    assert snap.phase == "critical"
    assert snap.cumulative_input == 150
    assert snap.ratio == 1.5


def test_budget_overflow_cumulative():
    """Each add_input reflects the most-recent request window size (not a running sum).
    The meter uses the last request size as the current-window estimate, so the final
    call with 20 tokens reports 20 (ok), not an accumulated 100."""
    meter = ContextMeter(budget=100)
    meter.add_input(40, source="test", estimated=False)
    meter.add_input(40, source="test", estimated=False)
    snap = meter.add_input(20, source="test", estimated=False)
    assert snap.cumulative_input == 20
    assert snap.phase == "ok"  # 20/100 = 0.20 < 0.80


def test_budget_warn_to_critical_transition():
    """Each call sets the current-window size independently; phase is determined
    by the most-recent request size relative to budget."""
    meter = ContextMeter(budget=1000)
    snap1 = meter.add_input(500, source="test", estimated=False)
    assert snap1.phase == "ok"   # 500/1000 = 50%
    assert snap1.cumulative_input == 500

    snap2 = meter.add_input(810, source="test", estimated=False)
    assert snap2.phase == "warn"  # 810/1000 = 81%
    assert snap2.cumulative_input == 810

    snap3 = meter.add_input(1010, source="test", estimated=False)
    assert snap3.phase == "critical"  # 1010/1000 = 101%
    assert snap3.cumulative_input == 1010


def test_reset_after_compact_resets_cumulative():
    """reset_after_compact zeroes cumulative_input and records compact timestamp."""
    meter = ContextMeter(budget=100)
    meter.add_input(95, source="test", estimated=False)
    assert meter.cumulative_input == 95

    snap = meter.reset_after_compact(compacted_at="2025-01-01T00:00:00Z")
    assert snap.cumulative_input == 0
    assert snap.phase == "ok"
    assert snap.last_compact_at == "2025-01-01T00:00:00Z"


def test_negative_input_clamped_to_zero():
    """Negative input values should be clamped to zero."""
    meter = ContextMeter(budget=100)
    snap = meter.add_input(-50, source="test", estimated=False)
    assert snap.cumulative_input == 0
    assert snap.phase == "ok"


def test_zero_budget_does_not_divide_by_zero():
    """Constructing with budget=0 should coerce to default and not crash."""
    meter = ContextMeter(budget=0)
    assert meter.budget == DEFAULT_CONTEXT_BUDGET_TOKENS
    snap = meter.snapshot()
    assert snap.ratio == 0.0


def test_to_metadata_round_trip():
    """to_metadata produces a dict that from_metadata can reload."""
    meter = ContextMeter(budget=500)
    meter.add_input(250, source="test", estimated=False)
    meta = meter.to_metadata()
    meter2 = ContextMeter.from_metadata(meta, budget=500)
    assert meter2.cumulative_input == 250


def test_estimate_text_tokens_empty_string():
    """Empty string should return 0 tokens."""
    assert estimate_text_tokens("") == 0


def test_estimate_text_tokens_short_text():
    """Short text should return at least 1 token."""
    assert estimate_text_tokens("hi") >= 1
