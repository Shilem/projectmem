"""MCP server for projectmem.

Hardened in the v0.0.6 polish-pass to fix three release-blocking bugs found
during Antigravity soft-launch testing:

  L-005: when an MCP client launches the server from its own CWD (which is
    typical — they don't switch to the project root), tool calls used to
    fail with "No .projectmem directory found." The server now resolves the
    project root from --root / $PROJECTMEM_ROOT, or by walking up from CWD
    until it finds a .projectmem/ directory (like git looks for .git/).

  L-009: write-tool CLI commands echo "Logged issue #N" to stdout via
    typer.echo(). MCP communicates JSON-RPC over the same stdio stream, so
    any byte from typer.echo corrupts the protocol and disconnects the
    client. Every tool body now runs inside _suppress_stdout(), which
    redirects sys.stdout to an in-memory buffer for the duration of the
    call. JSON-RPC bytes flow through untouched.

  L-010: an exception raised inside a tool was bubbling all the way up to
    the MCP runtime and killing the connection — one bad tool call ended
    the whole session until the user restarted their AI client. The
    @safe_tool decorator now catches every exception, returns it as a
    readable string the AI can recover from, and keeps the connection alive.

L-004 also strengthens tool descriptions + the FastMCP `instructions=`
field to nudge AI clients toward calling these tools instead of re-scanning
source files from conversation history.
"""
from __future__ import annotations

import contextlib
import functools
import io
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from mcp.server.fastmcp import FastMCP

from projectmem.commands import attempt, decision, fix, log, note
from projectmem.storage import (
    ai_instructions_path,
    discover_mem_dir,
    project_map_path,
    read_events,
    summary_path,
)


# ── L-005: pin the project root before tools run ────────────────────────

def _resolve_project_root() -> Path | None:
    """Resolve the project root for this MCP session.

    Priority order:
      1. --root <path> CLI argument (highest — explicit beats inferred)
      2. $PROJECTMEM_ROOT environment variable
      3. Parent-walk from CWD looking for `.projectmem/` (like git)
      4. None — tools will surface the helpful error from require_mem_dir.
    """
    if "--root" in sys.argv:
        idx = sys.argv.index("--root")
        if idx + 1 < len(sys.argv):
            return Path(sys.argv[idx + 1]).expanduser().resolve()
    env_root = os.environ.get("PROJECTMEM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    found = discover_mem_dir()
    if found is not None:
        return found.parent
    return None


_PROJECT_ROOT = _resolve_project_root()
if _PROJECT_ROOT is not None:
    # All tools rely on Path.cwd() via storage helpers; pin it once at startup
    # so the server keeps working even if some library chdir()s out from under
    # us mid-session.
    try:
        os.chdir(_PROJECT_ROOT)
    except OSError:
        pass


# ── L-009: stdout suppression for any tool that calls into CLI code ─────

@contextlib.contextmanager
def _suppress_stdout():
    """Redirect stdout to an in-memory buffer for the duration of the call.

    The MCP protocol multiplexes JSON-RPC over the server's real stdout.
    CLI command modules call ``typer.echo`` which writes to that stream,
    so we replace it for the body of every tool. The captured text is
    discarded — return values carry the user-facing message.
    """
    saved = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = saved


# ── L-010: never let a tool exception crash the connection ──────────────

def safe_tool(fn: Callable) -> Callable:
    """Wrap a tool so exceptions become text the AI can read and recover from.

    Every tool body also runs inside ``_suppress_stdout``. Combined, these
    two layers guarantee:
      - JSON-RPC stdio is never polluted (L-009)
      - A single tool failure never tears down the session (L-010)
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            with _suppress_stdout():
                return fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive
            return f"projectmem tool error: {type(exc).__name__}: {exc}"
    return wrapper


# ── FastMCP server + L-004 hardened system prompt ───────────────────────

mcp = FastMCP(
    "projectmem",
    instructions=(
        "You are connected to projectmem — a mandatory project memory + "
        "judgment layer. The tools below are FAR cheaper than re-deriving "
        "the same information from source: a get_summary call costs ~500 "
        "tokens; re-scanning the project to answer the same question "
        "costs ~5,000.\n"
        "\n"
        "SESSION START — call these three tools, in this order, BEFORE\n"
        "answering ANY question about this project:\n"
        "  1. get_instructions() — loads the project's mandatory workflow\n"
        "     rules. Without this you will not know how to log work\n"
        "     correctly, when to use add_note vs add_decision, or how\n"
        "     the event log is structured. Skipping this means bypassing\n"
        "     the memory layer and corrupting the audit trail.\n"
        "  2. get_summary() — loads project content. Do NOT answer from\n"
        "     conversation history or by re-reading package.json / README /\n"
        "     source files.\n"
        "  3. get_project_map() — loads structural layout when relevant.\n"
        "\n"
        "CONTEXT-SPECIFIC reads:\n"
        "  - User mentions a specific file → call precheck_file(path).\n"
        "  - User mentions a library → call get_global_gotchas(library).\n"
        "\n"
        "DURING work — use MCP write tools, NEVER edit .projectmem/ files\n"
        "directly via filesystem write:\n"
        "  - On a bug discovery → log_issue(summary, location).\n"
        "  - After each fix attempt → record_attempt(summary, outcome).\n"
        "  - After confirmation → record_fix(summary).\n"
        "  - On a design choice → add_decision(summary).\n"
        "  - On a gotcha / setup detail → add_note(summary).\n"
        "Editing .projectmem/summary.md or .projectmem/PROJECT_MAP.md\n"
        "directly bypasses event logging and breaks audit replay. The\n"
        "summary.md file is auto-regenerated from events.jsonl — write\n"
        "via the MCP tools and the summary will follow.\n"
        "\n"
        "Do NOT batch tool calls. Do NOT skip the session-start trio\n"
        "because 'I think I remember' — last session was a different\n"
        "conversation, and your memory of this project may be stale or\n"
        "wrong. These tools work regardless of working directory; the\n"
        "server already knows the project root."
    ),
)


# ════════════════════════════════════════════════════════════════════════
# Read tools
# ════════════════════════════════════════════════════════════════════════


@mcp.tool()
@safe_tool
def get_instructions() -> str:
    """Read the project's mandatory AI instructions.

    MANDATORY: call this at session start. The instructions describe the
    workflow rules you MUST follow while working in this project — they
    are not advisory."""
    path = ai_instructions_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No instructions found. Run `pjm init` first."


@mcp.tool()
@safe_tool
def get_summary() -> str:
    """Read the project memory summary.

    MANDATORY: call this BEFORE answering ANY question about the project.

    Do NOT answer from conversation history alone.
    Do NOT re-scan source files (package.json, README, src/) to understand
    the project — `summary.md` is the distilled authoritative source and
    costs ~500 tokens versus ~5,000 to re-derive.

    Your prior assumptions about this project may be stale. Call this
    cheaply at session start (and again before ending) to verify your
    work is recorded."""
    path = summary_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No summary found. Run `pjm init` first."


@mcp.tool()
@safe_tool
def get_project_map() -> str:
    """Read PROJECT_MAP.md to understand the repo structure.

    Call this at session start when structure matters (file layout,
    entry points, ownership). Cheaper than scanning the filesystem."""
    path = project_map_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No project map found."


@mcp.tool()
@safe_tool
def precheck_file(file_path: str) -> str:
    """Check a file's failure history BEFORE modifying it.

    MANDATORY: call this BEFORE proposing any change to a file.
    Surfaces failed past approaches, unresolved issues, and high churn
    so you don't repeat known dead-ends. Cheap (~100 tokens) and prevents
    expensive re-debugging cycles."""
    from projectmem.commands.precheck import _analyze_files
    events = read_events()
    warnings = _analyze_files([file_path], events)
    if not warnings:
        return f"{file_path}: no warnings. Safe to modify."
    lines = [f"projectmem precheck: {file_path}"]
    for w in warnings:
        lines.append(f"  [{w['severity'].upper()}] {w['title']}")
        for detail in w.get("details", []):
            lines.append(f"    - {detail}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
def get_issue(issue_id: str) -> str:
    """Read one specific issue's full history by ID (token-efficient).

    Use this when you only need one issue's context instead of the whole
    summary. Example: get_issue('0042')."""
    from projectmem.storage import issues_dir
    idir = issues_dir()
    matches = list(idir.glob(f"{issue_id}-*.md"))
    if not matches:
        return f"No issue found with ID {issue_id}."
    return matches[0].read_text(encoding="utf-8")


@mcp.tool()
@safe_tool
def search_events(query: str, limit: int = 10) -> str:
    """Plain-text search across all logged events.

    Token-efficient alternative to get_summary when you only need events
    matching a keyword. Returns matching event summaries with type and
    timestamp."""
    events = read_events()
    q = query.lower()
    matches = []
    for e in events:
        if q in e.summary.lower() or (e.notes and q in e.notes.lower()):
            matches.append(e)
    matches = matches[-limit:]
    if not matches:
        return f"No events match '{query}'."
    lines = [f"Found {len(matches)} match(es) for '{query}':"]
    for e in matches:
        outcome = f" ({e.outcome})" if e.outcome else ""
        loc = f" @ {e.location}" if e.location else ""
        lines.append(f"  [{e.type}{outcome}] {e.summary}{loc}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
def get_score() -> str:
    """Get the project's failure-prevention score.

    Returns an A+→F grade with concrete ROI numbers: debugging hours
    saved, tokens prevented, dollars protected. Use when the user asks
    about progress or value."""
    import json
    from projectmem.commands.score import calculate_score
    from projectmem.storage import events_path
    raw = []
    path = events_path()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw.append(json.loads(line))
    result = calculate_score(raw)
    c = result["components"]
    v = result["value"]
    return (
        f"projectmem Prevention Score: {result['grade']} ({result['score']}/100)\n"
        f"  Failed approaches on record: {c['failed_approaches']}\n"
        f"  Decisions documented: {c['decisions_documented']}\n"
        f"  Fixes with context: {c['fixes_with_context']}\n"
        f"  Debugging hours saved: ~{v['debugging_hours_saved']}h\n"
        f"  Tokens saved: {v['tokens_saved']:,}\n"
        f"  Estimated USD saved: ${v['usd_saved']:.2f}"
    )


@mcp.tool()
@safe_tool
def get_context(tokens: int = 2000, focus: Optional[str] = None) -> str:
    """Generate a token-budgeted memory context block.

    Use when you don't want to read the full summary. ``focus`` (e.g.
    'src/auth/') biases the context toward a specific area."""
    from projectmem.commands.context import generate_context
    events = read_events()
    result = generate_context(events, token_budget=tokens, focus=focus, recent_days=30)
    return result["markdown"]


@mcp.tool()
@safe_tool
def get_global_gotchas(library: Optional[str] = None) -> str:
    """Query cross-project library gotchas from ~/.projectmem/global/.

    Returns lessons learned in past projects that apply to the libraries
    you're about to use. Call whenever working with an unfamiliar library
    or starting a new feature."""
    from projectmem.global_memory import read_gotchas
    gotchas = read_gotchas()
    if library:
        gotchas = [g for g in gotchas if library.lower() in g.get("library", "").lower()]
    if not gotchas:
        return f"No global gotchas found{' for ' + library if library else ''}."
    lines = [f"Global gotchas ({len(gotchas)}):"]
    for g in gotchas[:15]:
        lib = g.get("library", "unknown")
        src = g.get("source_project", "")
        src_str = f" (from {src})" if src else ""
        lines.append(f"  [{lib}] {g.get('gotcha', '')}{src_str}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
# Write tools — wrapped in _suppress_stdout via @safe_tool. The CLI
# commands underneath these still call typer.echo, which would otherwise
# corrupt the JSON-RPC stream (L-009).
# ════════════════════════════════════════════════════════════════════════


@mcp.tool()
@safe_tool
def log_issue(summary: str, location: Optional[str] = None) -> str:
    """Open a new issue. Returns the issue ID.

    MANDATORY: call this IMMEDIATELY when you encounter a bug, regression,
    or unexpected behavior — BEFORE writing fix code. Logging up-front
    means the issue survives interruptions and session boundaries."""
    event = log.run(summary, location=location)
    return f"Logged issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def record_attempt(
    summary: str,
    outcome: str = "failed",
    location: Optional[str] = None,
    issue_id: Optional[str] = None,
) -> str:
    """Record a fix attempt on the current issue.

    MANDATORY: call IMMEDIATELY after each distinct fix attempt — do NOT
    batch multiple attempts into one call.

    `outcome` must be 'worked', 'failed', or 'partial'. Pass `issue_id`
    explicitly to attach to a specific issue; otherwise the attempt
    attaches to the active issue. If no active issue exists, an implicit
    parent issue is auto-created from this attempt's text (L-008)."""
    worked = outcome == "worked"
    failed = outcome == "failed"
    partial = outcome == "partial"
    event = attempt.run(
        summary, worked=worked, failed=failed, partial=partial,
        location=location, issue=issue_id, auto_issue=True,
    )
    return f"Recorded {outcome} attempt on #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def record_fix(summary: str, location: Optional[str] = None) -> str:
    """Record a confirmed fix and close the current issue.

    Only call AFTER you have evidence the fix works (test passes, error
    gone, user confirmed). Closing the issue clears the active-issue
    marker so the next record_attempt won't silently latch onto this
    closed issue (L-027a)."""
    event = fix.run(summary, location=location)
    return f"Fixed issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def add_decision(summary: str, location: Optional[str] = None) -> str:
    """Record an architectural or product decision permanently.

    Call when you make a choice that future sessions or contributors
    should know about. Decisions show up in `summary.md` and in
    `pjm wrap` context blocks."""
    decision.run(summary, location=location)
    return f"Recorded decision: {summary}"


@mcp.tool()
@safe_tool
def add_note(summary: str, location: Optional[str] = None) -> str:
    """Record a gotcha, setup detail, or other durable context.

    Use when you discover something important that doesn't fit as an
    issue or decision. Notes survive across sessions and appear in
    wrap context blocks."""
    note.run(summary, location=location)
    return f"Recorded note: {summary}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
