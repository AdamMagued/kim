"""Context-window budget tracking for Kim agent sessions.

The product budget is intentionally based on cumulative *input/context* tokens,
not output tokens. API providers may return exact input usage. Browser providers
usually do not, so Kim emits an explicitly labelled estimate derived from the
prompt that was injected into the browser LLM UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any, Mapping

DEFAULT_CONTEXT_BUDGET_TOKENS = 200_000
CONTEXT_BUDGET_VERSION = 1
WARN_RATIO = 0.80
CRITICAL_RATIO = 0.95
IMAGE_TOKEN_ESTIMATE = 1_500


@dataclass(frozen=True)
class ContextSnapshot:
    """A single context-meter update suitable for logging and persistence."""

    cumulative_input: int
    budget: int
    phase: str
    ratio: float
    last_input: int
    last_output: int
    source: str
    estimated: bool
    budget_version: int = CONTEXT_BUDGET_VERSION
    last_compact_at: str | None = None

    def to_log_line(self) -> str:
        """Return a compact machine-readable stdout line for ChatView."""
        estimate = "1" if self.estimated else "0"
        percent = int(round(self.ratio * 100))
        return (
            "[CONTEXT] "
            f"cumulative_input={self.cumulative_input} "
            f"budget={self.budget} "
            f"phase={self.phase} "
            f"percent={percent} "
            f"last_input={self.last_input} "
            f"last_output={self.last_output} "
            f"source={self.source} "
            f"estimate={estimate}"
        )


class ContextMeter:
    """Tracks cumulative input/context usage for one persisted session."""

    def __init__(
        self,
        *,
        budget: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
        cumulative_input: int = 0,
        last_compact_at: str | None = None,
    ) -> None:
        self.budget = coerce_budget(budget)
        self.cumulative_input = max(0, int(cumulative_input or 0))
        self.last_compact_at = last_compact_at

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None, *, budget: int) -> "ContextMeter":
        metadata = metadata or {}
        return cls(
            budget=budget,
            cumulative_input=_coerce_non_negative_int(metadata.get("cumulative_input"), 0),
            last_compact_at=metadata.get("last_compact_at") or None,
        )

    def observe_usage(
        self,
        usage: Mapping[str, Any] | None,
        *,
        fallback_input_tokens: int | None = None,
        source: str = "api",
        estimated: bool = False,
    ) -> ContextSnapshot | None:
        """Merge provider usage into the cumulative context counter.

        ``usage`` accepts Kim's canonical keys (``input``/``output``) plus common
        vendor aliases. If no input token count is available, the optional
        fallback estimate is used and the snapshot is labelled as estimated.
        """
        usage = usage or {}
        input_tokens = _usage_int(usage, "input", "input_tokens", "prompt_tokens")
        output_tokens = _usage_int(usage, "output", "output_tokens", "completion_tokens")

        usage_estimated = bool(
            usage.get("estimated")
            or usage.get("estimate")
            or usage.get("is_estimate")
            or estimated
        )

        if input_tokens is None and fallback_input_tokens is not None:
            input_tokens = max(0, int(fallback_input_tokens))
            usage_estimated = True

        if input_tokens is None:
            return None

        return self.add_input(
            input_tokens,
            source=str(usage.get("source") or source or "unknown"),
            estimated=usage_estimated,
            output_tokens=output_tokens or 0,
        )

    def add_input(
        self,
        tokens: int,
        *,
        source: str,
        estimated: bool,
        output_tokens: int = 0,
    ) -> ContextSnapshot:
        tokens = max(0, int(tokens or 0))
        # Stateless APIs re-send the full conversation history on every turn, so
        # summing input counts grows ~quadratically and produces spurious WARN/CRITICAL
        # phases. Use the most-recent request size as the window-fill estimate instead.
        self.cumulative_input = tokens
        return self.snapshot(
            last_input=tokens,
            last_output=max(0, int(output_tokens or 0)),
            source=source,
            estimated=estimated,
        )

    def snapshot(
        self,
        *,
        last_input: int = 0,
        last_output: int = 0,
        source: str = "unknown",
        estimated: bool = False,
    ) -> ContextSnapshot:
        ratio = self.cumulative_input / self.budget if self.budget > 0 else 0.0
        return ContextSnapshot(
            cumulative_input=self.cumulative_input,
            budget=self.budget,
            phase=context_phase(self.cumulative_input, self.budget),
            ratio=ratio,
            last_input=max(0, int(last_input or 0)),
            last_output=max(0, int(last_output or 0)),
            source=source,
            estimated=bool(estimated),
            last_compact_at=self.last_compact_at,
        )

    def to_metadata(self, snapshot: ContextSnapshot | None = None) -> dict[str, Any]:
        snap = snapshot or self.snapshot()
        data = asdict(snap)
        data["budget_version"] = CONTEXT_BUDGET_VERSION
        return data

    def reset_after_compact(self, *, compacted_at: str) -> ContextSnapshot:
        self.cumulative_input = 0
        self.last_compact_at = compacted_at
        return self.snapshot(source="compact", estimated=False)


def coerce_budget(value: Any, default: int = DEFAULT_CONTEXT_BUDGET_TOKENS) -> int:
    """Return a safe positive budget, falling back to the product default."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return parsed


def context_phase(cumulative_input: int, budget: int) -> str:
    """Map usage to the UI phase. Critical includes >=95% and over-budget."""
    budget = coerce_budget(budget)
    ratio = max(0, int(cumulative_input or 0)) / budget
    if ratio >= CRITICAL_RATIO:
        return "critical"
    if ratio >= WARN_RATIO:
        return "warn"
    return "ok"


def estimate_text_tokens(text: str) -> int:
    """Cheap deterministic tokenizer used only for clearly-labelled estimates.

    Four characters per token is the common rough planning heuristic for English
    text. We also count whitespace-separated chunks and take the larger value so
    short command-heavy prompts do not undercount too aggressively.
    """
    if not text:
        return 0
    by_chars = math.ceil(len(text) / 4)
    by_words = math.ceil(len(text.split()) * 1.3)
    return max(1, by_chars, by_words)


def estimate_content_tokens(content: Any) -> int:
    """Estimate tokens in Kim's canonical message content format."""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, str):
                total += estimate_text_tokens(item)
            elif isinstance(item, Mapping):
                if item.get("type") == "image":
                    total += IMAGE_TOKEN_ESTIMATE
                else:
                    total += estimate_text_tokens(str(item.get("text") or item.get("content") or item))
            else:
                total += estimate_text_tokens(str(item))
        return total
    return estimate_text_tokens(str(content))


# Cache the tools-schema token count between iterations.
# The tools list rarely changes within a run so we avoid re-serializing it
# (~50 schemas, ~8 KB) on every iteration.  The cache is keyed by the
# object identity and length of the list; any list replacement or length
# change invalidates the entry.  A single entry suffices because the agent
# uses one tools list per run.
_tools_token_cache: dict[tuple[int, int], int] = {}


def _tools_token_count(tools: list[dict]) -> int:
    """Return the serialized token count for a tools list, with caching."""
    key = (id(tools), len(tools))
    cached = _tools_token_cache.get(key)
    if cached is not None:
        return cached
    try:
        count = estimate_text_tokens(json.dumps(tools, separators=(",", ":")))
    except TypeError:
        count = estimate_text_tokens(str(tools))
    _tools_token_cache.clear()   # keep at most one entry
    _tools_token_cache[key] = count
    return count


def estimate_request_tokens(
    messages: list[dict] | None,
    *,
    system: str = "",
    tools: list[dict] | None = None,
) -> int:
    """Estimate a full provider request from canonical messages, system, tools."""
    total = estimate_text_tokens(system)
    if tools:
        total += _tools_token_count(tools)
    for message in messages or []:
        total += 4  # per-message wrapper overhead
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_content_tokens(message.get("content"))
    return total


def _coerce_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def _usage_int(usage: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None
