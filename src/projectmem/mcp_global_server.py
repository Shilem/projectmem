"""Single-process, multi-project MCP server for ProjectMem.

Unlike :mod:`projectmem.mcp_server`, this entry point has no session project.
Every project operation receives an explicit ``project_id`` and resolves it
through the strict machine-global registry.  The process CWD, environment
variables, MCP roots, and prior tool calls are never used to select a project.

The command modules still emit their normal CLI status lines.  Global MCP
write tools capture those lines under a process-wide lock so they cannot
corrupt the JSON-RPC stdout stream when FastMCP executes calls concurrently.
No CWD changes are needed because the command handlers accept an explicit
``root``.
"""
from __future__ import annotations

import contextlib
import functools
import io
import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from projectmem.commands import attempt, decision, fix, log, note
from projectmem.project_registry import (
    AmbiguousProjectError,
    ProjectDeletedError,
    ProjectNotInitializedError,
    ProjectPathChangedError,
    ProjectRegistryError,
    RegistryCorruptError,
    RegistryIOError,
    UnknownProjectError,
    registered_project_records,
    resolve_project_root,
)
from projectmem.storage import (
    ai_instructions_path,
    events_path,
    global_memory_enabled,
    issues_dir,
    plan_path,
    project_map_path,
    read_events,
    summary_path,
)

_STDOUT_LOCK = threading.RLock()
_T = TypeVar("_T")


@contextlib.contextmanager
def _suppress_legacy_stdout():
    """Capture CLI handler output while holding the stdout process lock."""
    with _STDOUT_LOCK:
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            sys.stdout = saved


def _run_legacy(handler: Callable[..., _T], *args: object, **kwargs: object) -> _T:
    """Run a root-aware CLI handler without polluting MCP stdout."""
    with _suppress_legacy_stdout():
        return handler(*args, **kwargs)


def _resolve_project(project_id: str) -> Path:
    """Resolve one explicit registered id; never infer a project."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise UnknownProjectError(f"Unknown ProjectMem project_id: {project_id!r}")
    return resolve_project_root(project_id.strip())


def safe_tool(fn: Callable[..., _T]) -> Callable[..., str | _T]:
    """Keep one malformed project request from terminating the MCP session."""
    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> str | _T:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001  # defensive connection guard
            return f"projectmem tool error: {type(exc).__name__}: {exc}"

    return wrapper


_GLOBAL_INSTRUCTIONS = (
    "You are connected to projectmem's single-process global MCP server. "
    "There is no implicit current project. Call list_projects() first, then "
    "pass the returned project_id explicitly to every project tool. Never use "
    "a filesystem root, CWD, MCP Roots, environment variable, or prior tool "
    "call as a project selector. Unknown, stale, deleted, or corrupted project "
    "identities must remain visible as errors.\n\n"
    "SESSION START: call list_projects(), then get_instructions(project_id), "
    "get_summary(project_id), and get_project_map(project_id) as needed. "
    "Before changing a file, call precheck_file(project_id, file_path). "
    "For historical context, use search_events(project_id, query) before "
    "get_issue(project_id, issue_id) or get_context(project_id).\n\n"
    "Use the write tools for issues, attempts, fixes, decisions, and notes. "
    "They are project-scoped by the explicit project_id and preserve each "
    "project's append-only event log."
)


mcp = FastMCP("projectmem-global", instructions=_GLOBAL_INSTRUCTIONS)


def _project_error_status(exc: ProjectRegistryError) -> tuple[str, str]:
    """Map registry failures to root-free, stable list-project statuses."""
    if isinstance(exc, ProjectDeletedError):
        return "deleted", "registered project root is missing"
    if isinstance(exc, ProjectNotInitializedError):
        return "uninitialized", "project memory is not initialized"
    if isinstance(exc, ProjectPathChangedError):
        return "identity_mismatch", "registered identity no longer matches the project"
    if isinstance(exc, AmbiguousProjectError):
        return "ambiguous", "project identity maps to multiple records"
    if isinstance(exc, UnknownProjectError):
        return "unknown", "project identity is not registered"
    if isinstance(exc, RegistryCorruptError):
        return "corrupt", "project registry is corrupt or unsupported"
    if isinstance(exc, RegistryIOError):
        return "unavailable", "project registry could not be read"
    return "error", "project registry validation failed"


@mcp.tool()
@safe_tool
def list_projects() -> str:
    """List registered projects without exposing filesystem roots.

    Each entry contains only ``project_id``, a display ``name``, and a
    validation ``status``.  Stale identities are returned as status entries so
    a client cannot mistake a filtered list for a healthy registry.
    """
    try:
        records = registered_project_records()
    except ProjectRegistryError as exc:
        status, error = _project_error_status(exc)
        return json.dumps(
            {"projects": [], "error": {"status": status, "message": error}},
            ensure_ascii=False,
            sort_keys=True,
        )

    projects: list[dict[str, str]] = []
    for record in records:
        # ``name`` is only the final display component.  Do not return the
        # record root itself, including in an error message.
        name = record.root.name or "(unnamed)"
        try:
            resolve_project_root(record.project_id)
        except ProjectRegistryError as exc:
            status, error = _project_error_status(exc)
            projects.append(
                {
                    "project_id": record.project_id,
                    "name": name,
                    "status": status,
                    "error": error,
                }
            )
        else:
            projects.append(
                {
                    "project_id": record.project_id,
                    "name": name,
                    "status": "ready",
                }
            )
    return json.dumps({"projects": projects}, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Project-scoped read tools
# ---------------------------------------------------------------------------


@mcp.tool()
@safe_tool
def get_instructions(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
) -> str:
    """Read one project's mandatory AI instructions."""
    root = _resolve_project(project_id)
    path = ai_instructions_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No instructions found. Run `pjm init` first."


@mcp.tool()
@safe_tool
def get_summary(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
) -> str:
    """Read one project's derived memory summary."""
    root = _resolve_project(project_id)
    path = summary_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No summary found. Run `pjm init` first."


@mcp.tool()
@safe_tool
def get_project_map(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
) -> str:
    """Read one project's structural map."""
    root = _resolve_project(project_id)
    path = project_map_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No project map found."


@mcp.tool()
@safe_tool
def get_plan(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
) -> str:
    """Read one project's intent plan."""
    root = _resolve_project(project_id)
    path = plan_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "No plan found."


@mcp.tool()
@safe_tool
def precheck_file(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    file_path: Annotated[
        str,
        Field(description="Project-relative or absolute file path to check."),
    ],
) -> str:
    """Check one project's failure history before changing a file."""
    from projectmem.commands.precheck import _analyze_files

    root = _resolve_project(project_id)
    events = read_events(root)
    warnings = _analyze_files([file_path], events, root=root)
    if not warnings:
        return f"{file_path}: no warnings. Safe to modify."
    lines = [f"projectmem precheck: {file_path}"]
    for warning in warnings:
        lines.append(f"  [{warning['severity'].upper()}] {warning['title']}")
        for detail in warning.get("details", []):
            lines.append(f"    - {detail}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
def get_issue(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    issue_id: Annotated[str, Field(description="Issue ID, for example '0042' or '42'.")],
) -> str:
    """Read one project's full issue history by numeric issue ID."""
    root = _resolve_project(project_id)
    raw_id = issue_id.strip()
    normalized_id = str(int(raw_id)).zfill(4) if raw_id.isdecimal() else None
    if normalized_id is None:
        return f"No issue found with ID {issue_id}."
    matches = list(issues_dir(root).glob(f"{normalized_id}-*.md"))
    if not matches:
        return f"No issue found with ID {normalized_id}."
    return matches[0].read_text(encoding="utf-8")


@mcp.tool()
@safe_tool
def search_events(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    query: Annotated[str, Field(description="Plain terms to search in this project's events.")],
    limit: Annotated[
        int,
        Field(description="Maximum result count (1-20).", ge=1, le=20),
    ] = 5,
) -> str:
    """Search only the selected project's indexed event metadata."""
    from projectmem.search_index import SearchIndexError
    from projectmem.search_index import search_events as indexed_search_events

    root = _resolve_project(project_id)
    try:
        matches = indexed_search_events(query, limit=limit, root=root)
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
def get_score(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
) -> str:
    """Calculate one project's failure-prevention score."""
    from projectmem.commands.score import calculate_score

    root = _resolve_project(project_id)
    raw: list[dict[str, object]] = []
    for line in events_path(root).read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw.append(json.loads(line))
    result = calculate_score(raw)
    components = result["components"]
    value = result["value"]
    return (
        f"projectmem Prevention Score: {result['grade']} ({result['score']}/100)\n"
        f"  Failed approaches on record: {components['failed_approaches']}\n"
        f"  Decisions documented: {components['decisions_documented']}\n"
        f"  Fixes with context: {components['fixes_with_context']}\n"
        f"  Debugging hours saved: ~{value['debugging_hours_saved']}h\n"
        f"  Tokens saved: {value['tokens_saved']:,}\n"
        f"  Estimated USD saved: ${value['usd_saved']:.2f}"
    )


@mcp.tool()
@safe_tool
def get_context(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    tokens: Annotated[
        int | None,
        Field(description="Optional token budget (100-20000).", ge=100, le=20000),
    ] = None,
    focus: Annotated[
        str | None,
        Field(description="Optional path prefix or keyword focus."),
    ] = None,
) -> str:
    """Generate a token-budgeted context block for one project."""
    from projectmem.commands.context import generate_context, resolve_token_budget

    root = _resolve_project(project_id)
    effective_tokens = resolve_token_budget(tokens, root)
    result = generate_context(
        read_events(root),
        token_budget=effective_tokens,
        focus=focus,
        recent_days=30,
        root=root,
        use_config=False,
    )
    return result["markdown"]


@mcp.tool()
@safe_tool
def get_global_gotchas(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    library: Annotated[
        str | None,
        Field(description="Optional library substring filter."),
    ] = None,
) -> str:
    """Return stack-relevant global gotchas for one opted-in project.

    The project setting is checked before reading global memory.  Results are
    always restricted to libraries/tags detected in that project's manifests;
    ``library`` narrows that already stack-filtered set further.
    """
    from projectmem.global_memory import detect_stack, read_gotchas

    root = _resolve_project(project_id)
    if not global_memory_enabled(root):
        return "Global memory is disabled for this project."

    stack = detect_stack(root)
    relevant = {str(value).casefold() for value in stack.get("libraries", [])}
    relevant.update(str(value).casefold() for value in stack.get("tags", []))
    relevant.update(str(value).casefold() for value in stack.get("frameworks", []))
    aliases = {"nextjs": "next", "tailwind": "tailwindcss"}
    for key, value in aliases.items():
        if key in relevant:
            relevant.add(value)
        if value in relevant:
            relevant.add(key)

    requested = library.casefold() if library else None
    gotchas = []
    for gotcha in read_gotchas():
        gotcha_library = str(gotcha.get("library", "")).casefold()
        if gotcha_library not in relevant:
            continue
        if requested and requested not in gotcha_library:
            continue
        gotchas.append(gotcha)

    if not gotchas:
        suffix = f" for {library}" if library else ""
        return f"No global gotchas found{suffix}."
    lines = [f"Global gotchas ({len(gotchas)}):"]
    for gotcha in gotchas[:15]:
        lib = gotcha.get("library", "unknown")
        source = gotcha.get("source_project", "")
        source_str = f" (from {source})" if source else ""
        lines.append(f"  [{lib}] {gotcha.get('gotcha', '')}{source_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project-scoped write tools
# ---------------------------------------------------------------------------


@mcp.tool()
@safe_tool
def log_issue(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    summary: Annotated[str, Field(description="One-line issue summary.")],
    location: Annotated[str | None, Field(description="Optional file or component location.")] = None,
) -> str:
    """Open an issue in the explicitly selected project."""
    root = _resolve_project(project_id)
    event = _run_legacy(log.run, summary, location=location, root=root)
    return f"Logged issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def record_attempt(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    summary: Annotated[str, Field(description="One-line attempted approach summary.")],
    outcome: Annotated[
        str,
        Field(description="Exactly 'worked', 'failed', or 'partial'.", pattern="^(worked|failed|partial)$"),
    ] = "failed",
    location: Annotated[str | None, Field(description="Optional file or component location.")] = None,
    issue_id: Annotated[str | None, Field(description="Optional issue ID to attach to.")] = None,
) -> str:
    """Record an attempt in the explicitly selected project."""
    root = _resolve_project(project_id)
    event = _run_legacy(
        attempt.run,
        summary,
        worked=outcome == "worked",
        failed=outcome == "failed",
        partial=outcome == "partial",
        location=location,
        issue=issue_id,
        auto_issue=True,
        root=root,
    )
    return f"Recorded {outcome} attempt on #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def record_fix(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    summary: Annotated[str, Field(description="One-line confirmed fix summary.")],
    location: Annotated[str | None, Field(description="Optional file or component location.")] = None,
    issue_id: Annotated[str | None, Field(description="Optional issue ID to close.")] = None,
) -> str:
    """Record a confirmed fix in the explicitly selected project."""
    root = _resolve_project(project_id)
    event = _run_legacy(
        fix.run,
        summary,
        location=location,
        issue=issue_id,
        root=root,
    )
    return f"Fixed issue #{event.issue_id}: {summary}"


@mcp.tool()
@safe_tool
def add_decision(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    summary: Annotated[str, Field(description="One-line architectural or product decision.")],
    location: Annotated[str | None, Field(description="Optional file or scope location.")] = None,
    supersedes: Annotated[str | None, Field(description="Optional prior decision event id.")] = None,
) -> str:
    """Record a decision in the explicitly selected project."""
    root = _resolve_project(project_id)
    _run_legacy(
        decision.run,
        summary,
        location=location,
        supersedes=supersedes,
        root=root,
    )
    if supersedes:
        return f"Recorded decision (supersedes {supersedes}): {summary}"
    return f"Recorded decision: {summary}"


@mcp.tool()
@safe_tool
def add_note(
    project_id: Annotated[str, Field(description="Explicit registered ProjectMem project_id.")],
    summary: Annotated[str, Field(description="One-line gotcha, setup detail, or note.")],
    location: Annotated[str | None, Field(description="Optional file or library location.")] = None,
) -> str:
    """Record a note in the explicitly selected project."""
    root = _resolve_project(project_id)
    _run_legacy(note.run, summary, location=location, root=root)
    return f"Recorded note: {summary}"


def main() -> None:
    """Run the global MCP server over the standard MCP transport."""
    mcp.run()


if __name__ == "__main__":
    main()
