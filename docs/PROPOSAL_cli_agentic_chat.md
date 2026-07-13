# Proposal: Agentic `kim chat` + rendered output (Prompt 7)

> **Status:** done — implemented in commit 1df7456 (`cli/src/agentic.rs`; HITL line also referenced by docs/ROADMAP_PROGRESS.md) — 2026-07-13

Goal: `kim chat` runs the REAL Kim agent (tool loop in the
terminal) when a Kim source root + Python are available, falling back to today's
plain LLM chat otherwise.

## Approach
Reuse what exists — do not invent a protocol or a second event system.

1. **Spawn the orchestrator** (`cli/src/agentic.rs`): run
   `python -m orchestrator.agent --task <prompt> --session-dir <dir> [--resume id]`
   from the detected Kim repo root, with `PYTHONPATH` set. The CLI already has
   `sessions::find_kim_repo_root()` and a Python finder.
2. **Parse the typed stdout protocol** that the desktop already consumes
   (`subprocess.rs` KimEvent / `events.schema.json`). Port the MINIMAL subset to
   the CLI: `status`, `tool`/tool-result lines, `answer`/text, `run_done`,
   `hitl_approval_request`. `parse_agent_line(line) -> Option<AgentLine>` is a
   pure function (unit-tested) mapping each JSON line to an existing `AppEvent`
   (`ThoughtChunk` / `ToolEvent` / `TextChunk` / `Done`) plus a HITL variant.
3. **Stream** tool events as dim activity lines via the existing
   `consume_turn_events` AppEvent renderer. No new rendering loop.
4. **HITL**: on `hitl_approval_request`, print a terminal `[y/N]` prompt
   (respecting the risk threshold the agent already gates on) and write
   `{"type":"hitl_approve","approved":bool}` to the child stdin — the same line
   protocol `StdinApprovalBridge` reads.
5. **Cancel**: Ctrl-C uses Prompt 3's machinery — aborting the task handle drops
   the child (`kill_on_drop`).
6. **Sessions** persist through the orchestrator's own session store
   (`--session-dir`), so resume works.
7. **Fallback**: if no source root or no Python, use today's `stream_kim_request`
   plain chat and print a one-line note.

## Markdown rendering
Hand-rolled `cli/src/markdown.rs` (no new dependency — keeps the binary lean and
the ANSI output fully under our control): headings → bold, `**bold**` → bold,
`` `inline` `` → reverse/dim, fenced code → dim left-border block. Pure
`render_markdown(s) -> String`; unit-tested by asserting structure + ANSI codes.

## Testable vs manual
- Unit-tested: `parse_agent_line`, `render_markdown`, `agentic_available`.
- Manual (needs a provider key + venv): the scripted REPL tool-using session in
  the DoD. The report states exactly what was run vs. could not be on this box.
