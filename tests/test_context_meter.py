from orchestrator.context_meter import (
    ContextMeter,
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    context_phase,
    coerce_budget,
    estimate_request_tokens,
    estimate_text_tokens,
    _tools_token_count,
    _tools_token_cache,
    _tools_cache_key,
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


# ── Regression guards for the four specified behaviors ───────────────────────


def test_record_run_uses_current_window_not_cumulative():
    """cumulative_input is assigned (not summed), so a second call with 2000
    tokens yields 2000, not 1000+2000=3000.  This prevents spurious WARN/CRITICAL
    phases caused by quadratic growth when the full conversation is resent."""
    meter = ContextMeter(budget=DEFAULT_CONTEXT_BUDGET_TOKENS)
    meter.add_input(1000, source="test", estimated=False)
    snap = meter.add_input(2000, source="test", estimated=False)
    assert snap.cumulative_input == 2000, (
        "cumulative_input must reflect the most-recent request window (assignment), "
        f"got {snap.cumulative_input}"
    )
    assert meter.cumulative_input == 2000


def test_tools_token_count_cached_by_content():
    """_tools_token_count returns the same value on repeated calls for the same
    list object without re-serializing: the cache entry keyed on a
    content-derived signature (not object identity — see finding 4) should be
    populated after the first call and reused on the second."""
    _tools_token_cache.clear()
    tools = [{"name": "read_file", "description": "reads a file"}]
    first = _tools_token_count(tools)
    # Cache must now hold exactly one entry for this list
    assert len(_tools_token_cache) == 1
    key = _tools_cache_key(tools)
    assert key in _tools_token_cache
    second = _tools_token_count(tools)
    assert first == second, "Second call must return same count as first (cache hit)"
    # Cache should still hold exactly one entry (no duplicate keys created)
    assert len(_tools_token_cache) == 1


def test_tools_token_cache_invalidated_on_length_change():
    """Appending an element to the tools list changes its content signature,
    which shifts the cache key and triggers a recompute.  After recompute,
    the cache holds at most one entry (old entry is evicted via clear())."""
    _tools_token_cache.clear()
    tools = [{"name": "read_file"}]
    count_before = _tools_token_count(tools)
    old_key = _tools_cache_key(tools)

    tools.append({"name": "write_file", "description": "writes a file with more tokens here"})
    count_after = _tools_token_count(tools)

    # Count must have changed because the schema is larger
    assert count_after > count_before, (
        "Appending a tool should increase the serialised token count"
    )
    # Old key must no longer be in cache (clear() was called on recompute)
    assert old_key not in _tools_token_cache, "Stale cache entry must be evicted after length change"
    # Cache holds exactly one entry (the new key)
    new_key = _tools_cache_key(tools)
    assert new_key in _tools_token_cache
    assert len(_tools_token_cache) == 1


def test_tools_token_cache_not_aliased_by_id_reuse():
    """Two DIFFERENT same-length tool lists must not collide in the cache even
    if one happens to be allocated at the same memory address a previous,
    now-freed list used (id() reuse across GC cycles) — finding 4. Regression
    guard for the old `(id(tools), len(tools))` key, which could silently
    return a stale token count for a completely different tool list.
    """
    _tools_token_cache.clear()
    tools_a = [{"name": "read_file", "description": "reads a file from disk"}]
    tools_b = [{"name": "run_command", "description": "executes a shell command with a much longer description"}]
    assert len(tools_a) == len(tools_b)

    count_a = _tools_token_count(tools_a)
    key_a = _tools_cache_key(tools_a)
    assert key_a in _tools_token_cache

    # Simulate id() reuse: force tools_b to reuse tools_a's old identity by
    # freeing tools_a first, then confirm the cache key itself (not python's
    # incidental memory reuse) is what keeps the two calls from colliding —
    # i.e. the key is content-derived, so it differs for different content
    # regardless of what id() any particular allocation gets.
    key_b = _tools_cache_key(tools_b)
    assert key_a != key_b, "Different tool lists must produce different cache keys"

    del tools_a
    count_b = _tools_token_count(tools_b)
    assert count_b != count_a, (
        "A different, longer-description tool list must not reuse the first "
        "list's cached token count"
    )
    assert key_b in _tools_token_cache
    assert _tools_token_cache[key_b] == count_b


def test_estimate_request_tokens_includes_tools():
    """estimate_request_tokens must add the tools token count on top of the
    system+messages estimate.  Calling with vs. without tools on the same
    system+messages should produce a strictly larger result."""
    messages = [{"role": "user", "content": "hello"}]
    system = "You are a helpful assistant."
    tools = [{"name": "read_file", "description": "reads a file from disk"}]

    without_tools = estimate_request_tokens(messages, system=system, tools=None)
    with_tools = estimate_request_tokens(messages, system=system, tools=tools)

    assert with_tools > without_tools, (
        f"estimate_request_tokens with tools ({with_tools}) must exceed "
        f"estimate without tools ({without_tools})"
    )
    # The difference must equal the cached tools count
    tools_count = _tools_token_count(tools)
    assert with_tools - without_tools == tools_count, (
        "The gap between with-tools and without-tools estimates must equal "
        f"_tools_token_count(tools)={tools_count}, got gap={with_tools - without_tools}"
    )
