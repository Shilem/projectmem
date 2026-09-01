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
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from projectmem.commands import attempt, decision, fix, log, note
from projectmem.storage import (
    ProjectMemError,
    ai_instructions_path,
    discover_mem_dir,
    plan_path,
    project_map_path,
    read_events,
    summary_path,
)

# ── L-005: pin the project root before tools run ────────────────────────

_PROJECT_ROOT_ERROR: str | None = None
# ``os.chdir`` and ``sys.stdout`` are process-global.  A FastMCP process may
# execute synchronous tools in multiple worker threads, so those two values
# must be protected for the whole duration of a tool call.  The lock is
# re-entrant because the two context managers below are intentionally usable
# independently as well as nested by ``safe_tool``.
_TOOL_GLOBAL_STATE_LOCK = threading.RLock()


def _validate_project_root(path: Path, source: str) -> Path | None:
    """Validate a configured root without falling back to the process CWD."""
    global _PROJECT_ROOT_ERROR
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        _PROJECT_ROOT_ERROR = (
            f"Invalid project root from {source}: cannot resolve {path}: {exc}."
        )
        return None

    if not resolved.exists():
        _PROJECT_ROOT_ERROR = (
            f"Invalid project root from {source}: {resolved} does not exist."
        )
        return None
    if not resolved.is_dir():
        _PROJECT_ROOT_ERROR = (
            f"Invalid project root from {source}: {resolved} is not a directory."
        )
        return None

    mem_dir = resolved / ".projectmem"
    if not mem_dir.is_dir() or not (mem_dir / "config.toml").is_file():
        _PROJECT_ROOT_ERROR = (
            f"Invalid project root from {source}: {resolved} has no initialized "
            "projectmem memory (.projectmem/config.toml). Run `pjm init` first."
        )
        return None
    return resolved


def _resolve_project_root() -> Path | None:
    """Resolve the project root for this MCP session.

    Priority order:
      1. --root <path> CLI argument (highest — explicit beats inferred)
      2. $PROJECTMEM_ROOT environment variable
      3. Parent-walk from CWD looking for `.projectmem/` (like git)
      4. None — tools surface an explicit session-root error.
    """
    global _PROJECT_ROOT_ERROR
    _PROJECT_ROOT_ERROR = None

    if "--root" in sys.argv:
        idx = sys.argv.index("--root")
        if idx + 1 >= len(sys.argv):
            _PROJECT_ROOT_ERROR = (
                "Invalid project root: --root requires a path to an initialized "
                "project directory."
            )
            return None
        return _validate_project_root(Path(sys.argv[idx + 1]), "--root")
    env_root = os.environ.get("PROJECTMEM_ROOT")
    if env_root:
        return _validate_project_root(Path(env_root), "PROJECTMEM_ROOT")
    try:
        found = discover_mem_dir()
    except OSError as exc:
        _PROJECT_ROOT_ERROR = f"Unable to discover project root: {exc}."
        return None
    if found is not None:
        return _validate_project_root(found.parent, "CWD discovery")
    _PROJECT_ROOT_ERROR = (
        "Unable to resolve an initialized project root from CWD. Set --root or "
        "PROJECTMEM_ROOT to a project containing .projectmem/config.toml."
    )
    return None


_PROJECT_ROOT = _resolve_project_root()


@contextlib.contextmanager
def _project_root_cwd():
    """Keep storage helpers inside the validated root for one tool call.

    The legacy command handlers still discover their project through
    ``Path.cwd()``.  Until those handlers all accept an explicit root, this is
    the narrow compatibility boundary that protects their process-global CWD.
    Holding the same lock as ``_suppress_stdout`` prevents either global from
    being observed in a half-switched state by another tool thread.
    """
    with _TOOL_GLOBAL_STATE_LOCK:
        if _PROJECT_ROOT is None:
            raise ProjectMemError(
                _PROJECT_ROOT_ERROR or "Project root is unavailable."
            )
        original = Path.cwd()
        try:
            os.chdir(_PROJECT_ROOT)
        except OSError as exc:
            raise ProjectMemError(
                f"Project root {_PROJECT_ROOT} cannot be used: {exc}."
            ) from exc
        try:
            yield
        finally:
            try:
                os.chdir(original)
            except OSError:
                # Keep the process pinned to the validated root if a tool
                # changed into a directory that disappeared during the call.
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
    with _TOOL_GLOBAL_STATE_LOCK:
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
            if _PROJECT_ROOT_ERROR:
                raise ProjectMemError(_PROJECT_ROOT_ERROR)
            with _project_root_cwd(), _suppress_stdout():
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
        "  - User asks about a past issue, file, error, or decision → call\n"
        "    search_events(query, limit=5) first, then get_issue() only for\n"
        "    the selected issue. Use get_context() for broad work only.\n"
        "\n"
        "DURING work — use MCP write tools, NEVER edit .projectmem/ files\n"
        "directly via filesystem write:\n"
        "  - On a bug discovery → log_issue(summary, location).\n"
        "  - After each fix attempt → record_attempt(summary, outcome).\n"
        "  - After confirmation → use record_fix(summary) for the active issue. If fixing a specific older issue, use record_fix(summary, issue_id=\"<issue_id>\") and replace <issue_id> with the actual Projectmem issue ID.\n"
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
    are not advisory.

    Read-only; does not modify memory."""
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
    work is recorded.

    Read-only; does not modify memory or trigger event logging."""
    path = summary_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No summary found. Run `pjm init` first."


@mcp.tool()
@safe_tool
def get_project_map() -> str:
    """Read PROJECT_MAP.md to understand the repo structure.

    Call this at session start when structure matters (file layout,
    entry points, ownership). Cheaper than scanning the filesystem.

    Read-only. Returns 'No project map found.' if PROJECT_MAP.md hasn't
    been initialized — run `pjm init` first if so."""
    path = project_map_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No project map found."


@mcp.tool()
@safe_tool
def get_plan() -> str:
    """Read plan.md — the project's INTENT file (ideas + plans).

    Call this at session start alongside get_summary(). plan.md records what
    the team MEANS to do (ideas, active plans, next steps) — distinct from
    the event log, which records what HAPPENED. When the user shares an idea
    or a plan, edit plan.md directly (add a bullet, check items off, move
    done work to Shipped); do NOT log plans as events.

    Read-only. Returns 'No plan found.' if plan.md hasn't been initialized."""
    path = plan_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No plan found."


@mcp.tool()
@safe_tool
def precheck_file(
    file_path: Annotated[str, Field(
        description="Project-relative or absolute file path to check "
                    "(e.g., 'src/auth.py'). Matched against the `location` "
                    "field of logged events — no file content is read from "
                    "disk. Returns 'no warnings' if the file has no "
                    "failure history."
    )],
) -> str:
    """Check a file's failure history BEFORE modifying it.

    MANDATORY: call this BEFORE proposing any change to a file.
    Surfaces failed past approaches, unresolved issues, and high churn
    so you don't repeat known dead-ends. Cheap (~100 tokens) and prevents
    expensive re-debugging cycles.

    Read-only; does not modify memory."""
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
def get_issue(
    issue_id: Annotated[str, Field(
        description="Issue ID returned by log_issue (e.g., '0042'). "
                    "Numeric strings are normalized to the zero-padded "
                    "4-digit form, so '42' resolves to issue '0042'."
    )],
) -> str:
    """Read one specific issue's full history by ID (token-efficient).

    Use this when you only need one issue's context instead of the whole
    summary. Numeric IDs are normalized before lookup (for example,
    get_issue('42') reads the same issue as get_issue('0042')).

    Read-only."""
    from projectmem.storage import issues_dir
    raw_id = issue_id.strip()
    normalized_id = str(int(raw_id)).zfill(4) if raw_id.isdecimal() else None
    if normalized_id is None:
        return f"No issue found with ID {issue_id}."
    idir = issues_dir()
    matches = list(idir.glob(f"{normalized_id}-*.md"))
    if not matches:
        return f"No issue found with ID {normalized_id}."
    return matches[0].read_text(encoding="utf-8")


@mcp.tool()
@safe_tool
def search_events(
    query: Annotated[str, Field(
        description="Plain terms matched locally against event IDs, summaries, "
                    "notes, file paths, locations, and issue IDs. Results are "
                    "compact metadata; no regex or boolean operators."
    )],
    limit: Annotated[int, Field(
        description="Maximum number of matching events to return "
                    "(default: 5; use the smallest useful number). "
                    "Range: 1-20.",
        ge=1, le=20,
    )] = 5,
) -> str:
    """Search compact local event metadata before loading full context.

    Uses a disposable SQLite FTS5 projection that is rebuilt from the
    append-only log when missing, stale, or corrupt.  It returns only compact
    metadata, so use ``get_issue`` after selecting a concrete issue instead
    of requesting generic context.

    Read-only. Empty result returns a friendly message, not an error."""
    from projectmem.search_index import SearchIndexError
    from projectmem.search_index import search_events as search_index_events

    try:
        matches = search_index_events(query, limit=limit, root=_PROJECT_ROOT)
    except (SearchIndexError, ValueError, TypeError) as exc:
        return f"Search index unavailable: {type(exc).__name__}: {exc}"
    if not matches:
        return f"No events match '{query}'."
    lines = [f"Found {len(matches)} match(es) for '{query}':"]
    for event in matches:
        outcome = f" ({event['outcome']})" if event["outcome"] else ""
        issue = f" #{event['issue_id']}" if event["issue_id"] else ""
        retired_tag = " (superseded)" if event["superseded"] else ""
        location = f" @ {event['location']}" if event["location"] else ""
        summary = event["summary"]
        if len(summary) > 240:
            summary = summary[:239].rstrip() + "…"
        lines.append(
            f"  [{event['type']}{outcome}]{issue} {summary}{retired_tag}{location} "
            f"(event {event['id']})"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
def get_score() -> str:
    """Get the project's failure-prevention score.

    Returns an A+→F grade with concrete ROI numbers: debugging hours
    saved, tokens prevented, dollars protected. Use when the user asks
    about progress or value.

    Read-only; computes the score from events.jsonl on each call."""
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
def get_context(
    tokens: Annotated[int | None, Field(
        description="Optional target token budget for the returned markdown. "
                    "When omitted, uses context_token_budget from the "
                    "project's .projectmem/config.toml or the 2000-token "
                    "default. The generated output is capped at this budget.",
        ge=100, le=20000,
    )] = None,
    focus: Annotated[str | None, Field(
        description="Optional path prefix or keyword to bias selection "
                    "toward (e.g., 'src/auth/'). When omitted, the "
                    "context is project-wide."
    )] = None,
) -> str:
    """Generate a token-budgeted memory context block.

    Use for broad project work. For a specific historical error, file, or
    decision, call ``search_events`` first; it returns smaller metadata and
    lets you fetch only the selected issue. ``focus`` (e.g. 'src/auth/')
    biases this broader context toward one area.

    Read-only; assembles a freshly-budgeted context block from
    events.jsonl."""
    from projectmem.commands.context import generate_context, resolve_token_budget
    effective_tokens = resolve_token_budget(tokens, _PROJECT_ROOT)
    events = read_events(_PROJECT_ROOT)
    result = generate_context(
        events,
        token_budget=effective_tokens,
        focus=focus,
        recent_days=30,
        root=_PROJECT_ROOT,
        use_config=False,
    )
    return result["markdown"]


@mcp.tool()
@safe_tool
def get_global_gotchas(
    library: Annotated[str | None, Field(
        description="Optional library name to filter by (case-insensitive "
                    "substring match — 'react' also matches "
                    "'react-router'). When omitted, returns all gotchas "
                    "across every library — useful when starting a new "
                    "feature to scan for any relevant past lessons."
    )] = None,
) -> str:
    """Query cross-project library gotchas from ~/.projectmem/global/.

    Returns lessons learned in past projects that apply to the libraries
    you're about to use. Call whenever working with an unfamiliar library
    or starting a new feature.

    Read-only. Reads from ~/.projectmem/global/ (cross-project memory,
    not this repo's .projectmem/)."""
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
def log_issue(
    summary: Annotated[str, Field(
        description="One-line description of the bug or unexpected "
                    "behavior (~140 chars recommended). Becomes the "
                    "issue title and is matched by search_events."
    )],
    location: Annotated[str | None, Field(
        description="Optional file path or component where the issue "
                    "manifests (e.g., 'src/auth.py' or "
                    "'login/double-submit'). Used by precheck_file to "
                    "surface this history later."
    )] = None,
) -> str:
    """Open a new issue. Returns the issue ID.

    MANDATORY: call this IMMEDIATELY when you encounter a bug, regression,
    or unexpected behavior — BEFORE writing fix code. Logging up-front
    means the issue survives interruptions and session boundaries.

    Side effects: appends an `issue` event to .projectmem/events.jsonl,
    creates an issue file in .projectmem/issues/, updates summary.md,
    and marks this issue as the active one for subsequent
    record_attempt calls."""
    event = log.run(summary, location=location)
    return f"Logged issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def record_attempt(
    summary: Annotated[str, Field(
        description="One-line description of what you tried (e.g., "
                    "'tried contain: layout — preview still jumps')."
    )],
    outcome: Annotated[str, Field(
        description="Result of the attempt. Must be exactly one of "
                    "'worked', 'failed', or 'partial'. Defaults to "
                    "'failed' — the safer default when an outcome is "
                    "uncertain.",
        pattern="^(worked|failed|partial)$",
    )] = "failed",
    location: Annotated[str | None, Field(
        description="Optional file path or component touched by this "
                    "attempt."
    )] = None,
    issue_id: Annotated[str | None, Field(
        description="Optional zero-padded issue ID (e.g., '0042') to "
                    "attach this attempt to. When omitted, attaches to "
                    "the active issue; if no active issue exists, an "
                    "implicit parent issue is auto-created from this "
                    "attempt's text."
    )] = None,
) -> str:
    """Record a fix attempt on the current issue.

    MANDATORY: call IMMEDIATELY after each distinct fix attempt — do NOT
    batch multiple attempts into one call.

    `outcome` must be 'worked', 'failed', or 'partial'. Pass `issue_id`
    explicitly to attach to a specific issue; otherwise the attempt
    attaches to the active issue. If no active issue exists, an implicit
    parent issue is auto-created from this attempt's text (L-008).

    Side effects: appends an `attempt` event and updates the issue file.
    Does NOT close the issue — call record_fix for that."""
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
def record_fix(
    summary: Annotated[
        str,
        Field(
            description=(
                "One-line description of the confirmed fix "
                "(e.g., 'guarded submit handler with isSubmitting ref')."
            )
        ),
    ],
    location: Annotated[
        str | None,
        Field(
            description="Optional file path or component where the fix was applied."
        ),
    ] = None,
    issue_id: Annotated[
        str | None,
        Field(
            description=(
                "Optional zero-padded issue ID (e.g., '0042') to close. "
                "When omitted, closes the active issue. Numeric strings without "
                "padding are accepted."
            )
        ),
    ] = None,
) -> str:
    """Record a confirmed fix and close an issue.

    Only call AFTER you have evidence the fix works: test passes, error is gone,
    or the user confirmed.

    If `issue_id` is provided, the fix is attached to that specific issue.
    If `issue_id` is omitted, the active issue is closed.

    Side effects: appends a `fix` event and updates summary.md. The active-issue
    marker is cleared only when the active issue is the issue being fixed."""
    event = fix.run(summary, location=location, issue=issue_id)
    return f"Fixed issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def add_decision(
    summary: Annotated[str, Field(
        description="One-line description of the architectural or "
                    "product decision (e.g., 'use bcrypt rounds=12 for "
                    "password hashing'). Becomes part of the project's "
                    "permanent record — write it for a future contributor."
    )],
    location: Annotated[str | None, Field(
        description="Optional file path or scope where the decision "
                    "applies (e.g., 'src/auth/' for a module-level "
                    "choice). Helps precheck_file cite the decision "
                    "when the file is later touched."
    )] = None,
    supersedes: Annotated[str | None, Field(
        description="Optional event id (evt_...) of a prior decision this "
                    "one retires. The old event stays in the log tagged "
                    "(superseded); only the new decision appears in "
                    "summary.md. Use when precheck_file flags a decision "
                    "as possibly stale and you are revising it."
    )] = None,
) -> str:
    """Record an architectural or product decision permanently.

    Call when you make a choice that future sessions or contributors
    should know about. Decisions show up in `summary.md` and in
    `pjm wrap` context blocks.

    Side effects: appends a `decision` event and updates summary.md.
    Decisions are append-only — to revise, pass `supersedes` with the old
    decision's event id instead of editing history."""
    decision.run(summary, location=location, supersedes=supersedes)
    if supersedes:
        return f"Recorded decision (supersedes {supersedes}): {summary}"
    return f"Recorded decision: {summary}"


@mcp.tool()
@safe_tool
def add_note(
    summary: Annotated[str, Field(
        description="One-line description of the gotcha, setup detail, "
                    "or context worth preserving. Prefix with 'gotcha:' "
                    "or 'lesson:' to enable cross-project promotion — "
                    "e.g., 'gotcha: bcrypt v4 silently truncates "
                    "passwords longer than 72 bytes'."
    )],
    location: Annotated[str | None, Field(
        description="Optional file path or library this note applies to "
                    "(e.g., 'bcrypt' for a library-specific gotcha)."
    )] = None,
) -> str:
    """Record a gotcha, setup detail, or other durable context.

    Use when you discover something important that doesn't fit as an
    issue or decision. Notes survive across sessions and appear in
    wrap context blocks.

    Side effects: appends a `note` event. Notes prefixed `gotcha:`,
    `lesson:`, or `warning:` are eligible for auto-promotion to
    ~/.projectmem/global/ for cross-project recall (L-046)."""
    note.run(summary, location=location)
    return f"Recorded note: {summary}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
