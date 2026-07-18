"""Ollama context-limit parsing helpers.

Pure, side-effect-free text parsers used by `OllamaProvider._resolve_context_limit`
(orchestrator/providers/ollama.py) to read a model's context window out of
`ollama ps` output or `/api/show`'s `parameters`/`modelfile` text blocks.
Extracted out of ollama.py (Q6 file-size gate: ollama.py was already over the
800-line cap, so new code must shrink it, not grow it — this cluster was
self-contained and had no `self` dependency, making it a clean split).
"""

from __future__ import annotations


def _parse_num_ctx(text: str | None) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for line in raw.splitlines():
        s = line.strip().replace("=", " ").replace(":", " ")
        parts = [p for p in s.split() if p]
        for i, part in enumerate(parts[:-1]):
            if part == "num_ctx":
                try:
                    return max(0, int(parts[i + 1]))
                except ValueError:
                    continue
    return None


def _parse_context_column(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    mult = 1
    if text[-1].lower() == "k":
        mult = 1000
        text = text[:-1]
    elif text[-1].lower() == "m":
        mult = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def _ps_context_column_span(header_line: str) -> tuple[int, int] | None:
    """Char span [start, end) of the CONTEXT column in an `ollama ps` header.

    The real header is ``NAME  ID  SIZE  PROCESSOR  CONTEXT  UNTIL`` — CONTEXT is
    NOT the last column; UNTIL ("4 minutes from now") is. Taking the last
    whitespace token therefore grabbed "now", so this path always returned None
    (issue #30). `ollama ps` is column-aligned (Go tabwriter), so the CONTEXT
    header and its values start at the same character offset; slice by that.
    Returns None for older `ollama` builds whose `ps` has no CONTEXT column.
    """
    cols: list[tuple[int, str]] = []
    idx = 0
    for token in header_line.split():
        start = header_line.find(token, idx)
        if start < 0:
            continue
        idx = start + len(token)
        cols.append((start, token))
    for i, (start, name) in enumerate(cols):
        if name.upper() == "CONTEXT":
            end = cols[i + 1][0] if i + 1 < len(cols) else len(header_line) + 1_000_000
            return start, end
    return None


def _parse_ollama_ps_context(output: str, model: str) -> int | None:
    wanted = model.strip().lower()
    span: tuple[int, int] | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("name ") or lowered == "name":
            span = _ps_context_column_span(line)
            continue
        if not lowered.startswith(wanted):
            continue
        if span is None:
            # No CONTEXT column in this `ollama ps` (old build) — no reliable
            # way to read it positionally; let callers fall back to /api/show.
            return None
        start, end = span
        if start >= len(line):
            continue
        segment = line[start:end].strip()
        value = _parse_context_column(segment)
        if value is not None:
            return value
    return None
