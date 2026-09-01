from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised only on POSIX
    msvcrt = None  # type: ignore[assignment]

from projectmem import project_registry as _project_registry
from projectmem.models import Event, resolve_event_ref, superseded_ids

MEM_DIR = ".projectmem"
SUMMARY_FILE = "summary.md"
EVENTS_FILE = "events.jsonl"
SUMMARY_INDEX_FILE = "summary.index.json"
EVENTS_STATE_FILE = "events.state.json"
CONFIG_FILE = "config.toml"
GLOBAL_MEMORY_ENABLED_CONFIG = "global_memory_enabled"
ISSUES_DIR = "issues"
AI_INSTRUCTIONS_FILE = "AI_INSTRUCTIONS.md"
PROJECT_MAP_FILE = "PROJECT_MAP.md"
PLAN_FILE = "plan.md"
TRANSACTION_LOCK_FILE = ".transaction.lock"


class ProjectMemError(RuntimeError):
    pass


class _TransactionLockState:
    """Process-local state for a re-entrant project transaction lock."""

    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.handle: BinaryIO | None = None


_TRANSACTION_STATES: dict[Path, _TransactionLockState] = {}
_TRANSACTION_STATES_GUARD = threading.Lock()


def mem_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / MEM_DIR


def _is_project_mem_dir(candidate: Path) -> bool:
    """True if `candidate` is a real, initialized project memory dir.

    `pjm init` always writes config.toml; the machine-wide global store at
    `~/.projectmem/` never has one. Without this check, walk-up discovery
    from any directory under $HOME that lacks its own project would land on
    the global store and misread it as project memory (0.1.4 fix — writes
    were silently accreting into `~/.projectmem/events.jsonl`).
    """
    return (
        candidate.is_dir()
        and (candidate / CONFIG_FILE).exists()
    )


def discover_mem_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for `.projectmem/`, like git does for `.git/`.

    Returns the discovered .projectmem path, or None if none found in any
    parent. Only initialized project dirs count — see _is_project_mem_dir.
    """
    cur = (start or Path.cwd()).resolve()
    for path in [cur, *cur.parents]:
        candidate = path / MEM_DIR
        if _is_project_mem_dir(candidate):
            return candidate
    return None


def require_mem_dir(root: Path | None = None) -> Path:
    # If an explicit root was given, honor only that root (back-compat).
    if root is not None:
        path = mem_path(root)
        if path.exists():
            return path
        raise ProjectMemError(
            f"No .projectmem directory found in {root}. Run `projectmem init`."
        )

    # No explicit root: try CWD first, then walk up the directory tree.
    # The CWD candidate gets the same initialized-project validation as the
    # walk-up — running pjm from $HOME must not mistake the global store
    # (`~/.projectmem/`, no config.toml) for a project.
    cwd_path = mem_path(None)
    if _is_project_mem_dir(cwd_path):
        return cwd_path
    found = discover_mem_dir(None)
    if found is not None:
        return found
    raise ProjectMemError(
        f"No .projectmem directory found in {Path.cwd()} or any parent. "
        f"If running over MCP, set the project root via the `cwd` field in your "
        f"MCP client config or via the PROJECTMEM_ROOT environment variable. "
        f"Otherwise run `projectmem init` to create one."
    )


def _file_lock(handle: BinaryIO) -> None:
    """Acquire an exclusive cross-process lock on ``handle``.

    ``fcntl.flock`` is the native primitive on POSIX (the supported runtime
    for the local-first CLI).  Keep a small ``msvcrt`` fallback so the same
    package remains usable on Windows without adding a locking dependency.
    """
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - defensive for unusual runtimes
        raise ProjectMemError("Project transactions require a file-locking API.")

    # msvcrt.locking works on a byte range starting at the current cursor.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _file_unlock(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised only on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _transaction_state(lock_path: Path) -> _TransactionLockState:
    with _TRANSACTION_STATES_GUARD:
        state = _TRANSACTION_STATES.get(lock_path)
        if state is None:
            state = _TransactionLockState()
            _TRANSACTION_STATES[lock_path] = state
        return state


@contextmanager
def project_transaction(root: Path | None = None) -> Iterator[Path]:
    """Serialize one project's event/marker/summary state.

    The per-project lock is both thread-safe within this process and
    cross-process via a lock file.  It is intentionally re-entrant so the
    low-level ``append_event`` and marker helpers can enforce their own safety
    when called outside a command transaction without deadlocking a command
    that already owns the same project lock.

    The yielded path is the canonical project root, allowing callers to pass
    it explicitly to every read/write in the transaction even when the caller
    started in a nested directory.
    """
    project_dir = require_mem_dir(root)
    lock_path = (project_dir / TRANSACTION_LOCK_FILE).resolve()
    state = _transaction_state(lock_path)

    state.thread_lock.acquire()
    entered = False
    try:
        if state.depth == 0:
            handle = lock_path.open("a+b")
            try:
                _file_lock(handle)
            except Exception:
                handle.close()
                raise
            state.handle = handle
        state.depth += 1
        entered = True
        yield lock_path.parent.parent
    finally:
        try:
            if entered:
                state.depth -= 1
                if state.depth == 0:
                    handle = state.handle
                    state.handle = None
                    if handle is not None:
                        try:
                            _file_unlock(handle)
                        finally:
                            handle.close()
        finally:
            state.thread_lock.release()


def events_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / EVENTS_FILE


def summary_index_path(root: Path | None = None) -> Path:
    """Return the disposable summary projection path for ``root``."""
    return require_mem_dir(root) / SUMMARY_INDEX_FILE


def events_state_path(root: Path | None = None) -> Path:
    """Return the append-state receipt used to detect out-of-band rewrites."""
    return require_mem_dir(root) / EVENTS_STATE_FILE


def summary_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / SUMMARY_FILE


def ai_instructions_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / AI_INSTRUCTIONS_FILE


def project_map_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / PROJECT_MAP_FILE


def plan_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / PLAN_FILE


def issues_dir(root: Path | None = None) -> Path:
    return require_mem_dir(root) / ISSUES_DIR


def _set_global_memory_enabled(root: Path, enabled: bool) -> None:
    """Persist the per-project global-memory preference in ``config.toml``."""
    config = mem_path(root) / CONFIG_FILE
    try:
        content = config.read_text(encoding="utf-8")
    except OSError:
        return

    value = "true" if enabled else "false"
    pattern = re.compile(
        rf"^(\s*{re.escape(GLOBAL_MEMORY_ENABLED_CONFIG)}\s*=\s*)"
        rf"(?:true|false)(\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf"\g<1>{value}\g<2>", content, count=1)
    if count == 0:
        suffix = "" if content.endswith("\n") else "\n"
        updated = (
            f"{content}{suffix}{GLOBAL_MEMORY_ENABLED_CONFIG} = {value}\n"
        )
    if updated != content:
        config.write_text(updated, encoding="utf-8")


def global_memory_enabled(root: Path | None = None) -> bool:
    """Return whether automatic cross-project memory is enabled for a project.

    The setting is deliberately opt-out: projects initialized before this
    setting existed, and projects with malformed or missing configuration,
    retain the historical auto-promotion behavior.  Only an explicit
    ``global_memory_enabled = false`` disables global reads/writes.
    """
    try:
        config = require_mem_dir(root) / CONFIG_FILE
        content = config.read_text(encoding="utf-8")
    except (OSError, ProjectMemError):
        return True

    for line in content.splitlines():
        # This is intentionally a tiny TOML reader for one root-level boolean;
        # the rest of config.toml is already treated as simple key/value data.
        without_comment = line.split("#", 1)[0].strip()
        if not without_comment or without_comment.startswith("["):
            continue
        key, separator, value = without_comment.partition("=")
        if separator and key.strip() == GLOBAL_MEMORY_ENABLED_CONFIG:
            return value.strip().lower() != "false"
    return True


def set_global_memory_enabled(root: Path, enabled: bool) -> None:
    """Set the project-level global-memory preference.

    This small public wrapper is useful to callers that already know the
    desired preference; ``initialize`` uses it for the CLI's ``--no-global``
    flag without changing the existing command signature.
    """
    _set_global_memory_enabled(root, enabled)


def initialize(
    root: Path | None = None,
    *,
    global_enabled: bool | None = None,
) -> Path:
    root_path = root or Path.cwd()
    project_dir = mem_path(root_path)
    project_dir.mkdir(exist_ok=True)
    (project_dir / ISSUES_DIR).mkdir(exist_ok=True)
    (project_dir / EVENTS_FILE).touch(exist_ok=True)

    config = project_dir / CONFIG_FILE
    if not config.exists():
        config.write_text(
            'summary_size_limit_kb = 20\nrecent_days = 30\n'
            'context_token_budget = 2000\nproject_description = ""\n',
            encoding="utf-8",
        )

    # ``--no-global`` used to affect inheritance only.  Persist an explicit
    # opt-out so later event writes cannot silently promote data.  A plain
    # re-run of ``pjm init`` leaves an existing preference untouched; this is
    # what makes the opt-out durable while preserving the historical default.
    if global_enabled is not None:
        _set_global_memory_enabled(root_path, global_enabled)

    summary = project_dir / SUMMARY_FILE
    if not summary.exists():
        summary.write_text(initial_summary(root_path), encoding="utf-8")

    instructions = project_dir / AI_INSTRUCTIONS_FILE
    if not instructions.exists():
        instructions.write_text(ai_instructions(), encoding="utf-8")

    project_map = project_dir / PROJECT_MAP_FILE
    if not project_map.exists():
        project_map.write_text(initial_project_map(root_path), encoding="utf-8")

    plan = project_dir / PLAN_FILE
    if not plan.exists():
        plan.write_text(initial_plan(root_path), encoding="utf-8")

    ensure_gitignore_entry(root_path)
    register_project(root_path)
    return project_dir


def registry_path() -> Path:
    """Compatibility wrapper for the machine-global registry path."""
    return _project_registry.registry_path()


def register_project(root: Path) -> None:
    """Add an initialized project to the registry (idempotent).

    The historical helper returned ``None``; retain that return contract while
    exposing the richer ``register_project_record`` API from this module.
    """
    _project_registry.register_project_record(root)


def registered_projects() -> list[Path]:
    """Compatibility reader returning live initialized roots only.

    The old dashboard deliberately filtered stale entries.  Keep that
    behavior here; strict global-MCP callers must use ``resolve_project_root``
    or ``registered_project_records`` so deleted/uninitialized entries and
    registry corruption remain observable.
    """
    records = _project_registry.load_registry()
    out: list[Path] = []
    seen: set[Path] = set()
    for record in records:
        try:
            root = _project_registry.canonicalize_project_root(
                record.root, require_initialized=True
            )
        except (
            _project_registry.ProjectDeletedError,
            _project_registry.ProjectNotInitializedError,
            _project_registry.ProjectPathChangedError,
        ):
            continue
        if root not in seen:
            seen.add(root)
            out.append(root)
    return out


def initial_summary(root: Path) -> str:
    project_name = root.name
    return (
        f"# projectmem - {project_name}\n\n"
        "_Last updated: never (placeholder — populate via `pjm decision` / "
        "`pjm note` or the `add_decision` / `add_note` MCP tools)._\n\n"
        "## Project purpose\n"
        "Replace this placeholder with a concise description of what this project "
        "does, who it serves, and the main technologies or runtime assumptions.\n\n"
        "## How to use this memory\n"
        "AI assistants and human contributors should read this file before making "
        "changes. Keep it focused on durable context: current issues, decisions, "
        "failed attempts, gotchas, and files that matter.\n\n"
        "**For AI assistants finding this placeholder:** you are in Setup Mode. "
        "Read README, package metadata, and obvious entry points, then call "
        "`add_decision` and `add_note` (MCP) — or `pjm decision` / `pjm note` "
        "(CLI) — to record what you learned. Each call appends an event and "
        "auto-regenerates this summary. **Do NOT edit this file directly** — "
        "it is derived from `events.jsonl`. See `.projectmem/AI_INSTRUCTIONS.md` "
        "for the full Setup Mode workflow.\n\n"
        "## Current issues\n"
        "- None logged yet.\n\n"
        "## Recent fixes\n"
        "- None logged yet.\n\n"
        "## Decisions\n"
        "- None logged yet.\n\n"
        "## Known gotchas\n"
        "- None logged yet.\n\n"
        "## Key files\n"
        "- None logged yet.\n\n"
        "## Open questions\n"
        "- None logged yet.\n"
    )


def ai_instructions() -> str:
    return (
        "# projectmem AI Instructions\n\n"
        "These instructions are MANDATORY for all AI coding agents working in this "
        "project. Failure to follow them means your work is incomplete and the audit "
        "trail is corrupted.\n\n"
        "This file is stable operating guidance. Do not rewrite it unless the user "
        "asks or projectmem itself changes.\n\n"
        "## Start of every session\n\n"
        "**Step 1 — Identify your mode by reading `.projectmem/summary.md` and "
        "`.projectmem/PROJECT_MAP.md`.**\n\n"
        "- **Setup Mode** — `summary.md` and/or `PROJECT_MAP.md` still contain the "
        "**placeholder text** from `pjm init`. Concrete signals you are in Setup Mode:\n"
        "  - `summary.md` contains the phrase *\"Replace this placeholder with a "
        "concise description...\"*\n"
        "  - Section bodies say *\"None logged yet.\"*\n"
        "  - `PROJECT_MAP.md` contains *\"Status: not created yet\"* and its "
        "`## Structure` / `## Relationships` sections are still empty (or say "
        "*\"Not described yet.\"*).\n\n"
        "  → **You MUST populate both files with real project content before doing "
        "any other work for the user.** This is not optional and not deferred — your "
        "first response in a Setup Mode session is the memory-population pass. "
        "Procedure:\n\n"
        "  1. Read `README.md`, `package.json` / `pyproject.toml` / `Cargo.toml`, "
        "entry-point files (typically `src/main.*`, `index.html`, "
        "`app/__init__.py`, etc.), and any obvious architectural files.\n"
        "  2. For **each architectural choice** you identify (frameworks, language, "
        "build system, deployment target, data flow): call `add_decision` (MCP) or "
        "`pjm decision` (CLI) — one call per decision.\n"
        "  3. For **each gotcha / setup detail / library quirk**: call `add_note` "
        "(MCP) or `pjm note` (CLI) — one call per gotcha.\n"
        "  4. Each `add_decision` / `add_note` call appends to `events.jsonl` AND "
        "auto-regenerates `summary.md`. **NEVER edit `summary.md` directly** — it is "
        "derived; your edits will be overwritten on the next event.\n"
        "  5. **DO edit `PROJECT_MAP.md` directly** to replace its placeholder. "
        "`PROJECT_MAP.md` is structural and is NOT derived from events. Author these "
        "core sections (if `pjm init` already pre-added `## Stack` or `## Entry "
        "points` from manifest detection, keep them):\n"
        "     - `## Project purpose` — one or two sentences on what the project does. "
        "REQUIRED: this section is auto-copied into `summary.md`'s Project purpose on "
        "the next regeneration (the only path by which it gets populated; there is "
        "intentionally no MCP tool for it).\n"
        "     - `## Structure` — the project's real folders and key files **as "
        "paths**, nested, each with a one-line description. `pjm init` already "
        "pre-seeds the top-level folders (e.g. `core/`, `features/`); your job is to "
        "list the key files under each with their real paths and describe them, "
        "like:\n\n"
        "       ```\n"
        "       ## Structure\n"
        "       - `core/` — engine\n"
        "         - `core/run.py` — entry point / runner\n"
        "         - `core/parser.py` — input parsing\n"
        "       ```\n\n"
        "       Treat `## Structure` as a **navigable path index**: a later session "
        "must be able to locate any important file from this section alone, WITHOUT "
        "re-scanning the repo. That path index is exactly what saves tokens — it "
        "replaces re-reading the codebase every session.\n"
        "     - `## Relationships` — how the main pieces connect, one bullet each, "
        "using real paths and a verb: `core/run.py calls core/parser.py`, "
        "`ui/Chart.tsx reads store/events.py`, `api/auth.py writes store/events.py`.\n"
        "  6. After step 5, summary.md and PROJECT_MAP.md both contain real content "
        "(summary.md picks up the Project purpose from PROJECT_MAP.md on the next "
        "`add_decision` / `add_note` call's auto-regen — or you can force it now "
        "with `pjm regenerate`). The project is in Maintenance Mode for every "
        "subsequent session.\n\n"
        "- **Maintenance Mode** — `summary.md` AND `PROJECT_MAP.md` contain **real "
        "project content, NOT the `pjm init` placeholder text**. Concrete signals "
        "you are in Maintenance Mode:\n"
        "  - `summary.md` describes the actual project, lists real issues / "
        "decisions / notes by content.\n"
        "  - `PROJECT_MAP.md` has a real `## Structure` (folders and files with "
        "paths) and `## Relationships` — not *\"Status: not created yet.\"*\n\n"
        "  → **STOP analyzing the project structure.** The memory is already built. "
        "Use the existing summary + map. Focus exclusively on the user's actual task "
        "and on logging your own work via the trigger table.\n"
        "  - Do NOT re-scan source files. Trust the memory.\n"
        "  - Do NOT re-write `summary.md` or `PROJECT_MAP.md`. They are already "
        "correct; if you find an out-of-date detail, fix it through the trigger "
        "table (`add_note` / `add_decision` / `log_issue`) — never via direct file "
        "edit on summary.md.\n\n"
        "**Step 2 — Read these files (or call the MCP equivalents):**\n\n"
        "| File | MCP tool | Purpose |\n"
        "| --- | --- | --- |\n"
        "| `.projectmem/AI_INSTRUCTIONS.md` | `get_instructions()` | Workflow rules (this file) |\n"
        "| `.projectmem/summary.md` | `get_summary()` | Distilled project memory (what HAPPENED) |\n"
        "| `.projectmem/PROJECT_MAP.md` | `get_project_map()` | Structural layout |\n"
        "| `.projectmem/plan.md` | `get_plan()` | Ideas + plans (what we INTEND to do) |\n\n"
        "**`plan.md` — intent, not memory.** It records what the team *means to do* "
        "(ideas, active plans, next steps) — separate from the event log, which "
        "records what *happened*. When the user shares an idea or a plan, **edit "
        "`plan.md` directly** (add a bullet, tick `- [x]` items off, move finished "
        "plans down to Shipped) — exactly like you edit `PROJECT_MAP.md`. NEVER log a "
        "plan/idea as an event, and never add a new event type for it; the vocabulary "
        "stays the six typed events.\n\n"
        "Prefer the MCP tools when available — they're cheaper (~500 tokens) than "
        "reading files individually and they auto-resolve the project root regardless "
        "of your working directory.\n\n"
        "**Step 3 — Check `.projectmem/issues/` only when a logged issue looks "
        "relevant to the current task** (use `get_issue(issue_id)` via MCP, or read "
        "the file). Don't read every issue on every session — that's wasteful.\n\n"
        "**Step 4 — Treat `.projectmem/events.jsonl` as the append-only raw log.** "
        "Do not edit it by hand unless repairing corruption. Use write tools.\n\n"
        "## Working on a file — navigate by the map, do NOT scan the repo\n\n"
        "Before you read, grep, or edit any file, use the map instead of exploring "
        "the codebase:\n\n"
        "1. **Locate it in `PROJECT_MAP.md` `## Structure`** — the path index tells "
        "you where the file lives and what it does; check `## Relationships` for what "
        "it connects to. (`get_project_map()` via MCP.)\n"
        "2. **Call `precheck_file(path)`** (MCP) or `pjm precheck <path>` (CLI) — "
        "MANDATORY before proposing ANY change to a file. It surfaces that file's "
        "failed past approaches, open issues, and churn in ~100 tokens, so you never "
        "re-try a known dead-end.\n"
        "3. **Then read ONLY that file** — not the whole codebase.\n\n"
        "This is the core token-saving loop: navigate by the map + memory instead of "
        "grepping the repo, and avoid repeating a fix that already failed. A richer "
        "`## Structure` (more files listed with paths) means fewer tokens spent "
        "searching.\n\n"
        "## MANDATORY Triggers — You MUST act on these automatically\n\n"
        "When a trigger fires, you MUST call the corresponding tool IMMEDIATELY, "
        "before continuing any other work. **Prefer MCP tools** (left column) when "
        "you're connected via an MCP-capable client; **fall back to CLI** (right "
        "column) otherwise.\n\n"
        "| Trigger | MCP tool | CLI command |\n"
        "| --- | --- | --- |\n"
        "| Bug, error, or unexpected behavior | `log_issue(summary, location)` | `pjm log \"<text>\" --at \"<file:line>\"` |\n"
        "| Fix attempt FAILED | `record_attempt(summary, outcome=\"failed\")` | `pjm attempt \"<text>\" --failed --at \"<file:line>\"` |\n"
        "| Fix attempt PARTIAL (helped but didn't fully fix) | `record_attempt(summary, outcome=\"partial\")` | `pjm attempt \"<text>\" --partial --at \"<file:line>\"` |\n"
        "| Fix attempt WORKED | `record_attempt(summary, outcome=\"worked\")` | `pjm attempt \"<text>\" --worked --at \"<file:line>\"` |\n"
        "| Fix confirmed — close the issue | `record_fix(summary)` | `pjm fix \"<text>\" --at \"<file:line>\"` |\n"
        "| Architectural / design decision | `add_decision(summary)` | `pjm decision \"<text>\" --at \"<file:line>\"` |\n"
        "| Gotcha / setup detail / constraint discovered | `add_note(summary)` | `pjm note \"<text>\" --at \"<file:line>\"` |\n"
        "| Before finishing the session | `get_summary()` | `pjm show` |\n\n"
        "All write tools auto-append to `events.jsonl` AND auto-regenerate "
        "`summary.md`. You do NOT need to call a separate \"save\" or \"regenerate\" "
        "command after each tool. The summary follows the events automatically.\n\n"
        "## Execution Rules\n\n"
        "1. **Log BEFORE you fix.** When you see a bug, call `log_issue` (or "
        "`pjm log`) BEFORE writing fix code. The issue survives interruptions and "
        "session boundaries; in-flight fix work does not.\n"
        "2. **Record IMMEDIATELY after each attempt.** Do not batch multiple attempts "
        "into one entry. Each distinct approach gets its own `record_attempt` call.\n"
        "3. **Close with `record_fix` only after evidence.** Test passes, error is "
        "gone, or the user confirms — anything less and the issue stays open.\n"
        "4. **Never skip logging because it feels minor.** A small fix today is a "
        "mystery regression tomorrow. Log it.\n"
        "5. **NEVER edit `.projectmem/summary.md` or `.projectmem/events.jsonl` "
        "directly via filesystem write.** Both are derived/append-only. Use the "
        "write tools. (You MAY edit `PROJECT_MAP.md` directly when restructuring it; "
        "it's not derived from events.)\n\n"
        "## What to track\n\n"
        "Use projectmem to preserve the development story that would otherwise be "
        "lost between chats, terminal sessions, and commits.\n\n"
        "Track:\n\n"
        "- new issues, bugs, regressions, unclear behavior, or investigation topics\n"
        "- hypotheses about causes\n"
        "- attempted fixes or experiments (each as its own `record_attempt`)\n"
        "- whether each attempt worked, failed, or partially helped\n"
        "- final fixes and the files involved\n"
        "- architectural, product, or implementation decisions and their reasons\n"
        "- gotchas, setup requirements, flaky tests, environment notes, "
        "important constraints\n"
        "- key files future contributors or AI agents should read first\n\n"
        "Do NOT track secrets, credentials, private customer data, access tokens, "
        "or large transcripts.\n\n"
        "## Auto-Capture (active)\n\n"
        "Git hooks installed by `pjm init` automatically capture:\n\n"
        "- Commits (post-commit hook)\n"
        "- Reverts (auto-classified as failed approaches)\n"
        "- Merges (auto-classified as milestones)\n"
        "- File churn (the `pjm watch` daemon flags rapid same-file edits)\n\n"
        "You do NOT need to manually log any of those. You SHOULD still manually log:\n\n"
        "- Decisions with rationale (`add_decision` / `pjm decision`)\n"
        "- Pre-attempt context for complex fixes (`record_attempt` / `pjm attempt`)\n"
        "- External factors and gotchas (`add_note` / `pjm note`)\n"
        "- Failure context that commit messages don't capture\n\n"
        "## Pre-commit safety net\n\n"
        "Every `git commit` automatically runs `pjm precheck` against the staged "
        "files. If you're about to commit a file with unresolved issues, recent "
        "failed attempts, or high churn, you'll see a warning block before the "
        "commit lands. Read it; it exists to stop you from repeating known "
        "failures. To bypass once: `git commit --no-verify`.\n\n"
        "## Rules summary\n\n"
        "- **MANDATORY: Log before you exit.** Work is not finished until project "
        "memory reflects what happened.\n"
        "- **MANDATORY: Record failed and partial attempts.** Negative and "
        "partial-credit knowledge is often the most valuable part of project memory.\n"
        "- Keep entries concise but specific enough that another person or AI can "
        "avoid repeating work. Include file paths, error names, test names.\n"
        "- Prefer several small accurate entries over one vague long entry.\n"
        "- Do not claim something is fixed until tests, reproduction, or user "
        "confirmation supports it.\n"
        "- Do not overwrite history. `events.jsonl` is append-only; `summary.md` "
        "is derived from it.\n"
        "- If MCP is unavailable, use the CLI (`pjm log`, `pjm attempt`, "
        "`pjm fix`, `pjm decision`, `pjm note`). If neither is available, clearly "
        "tell the user what should be recorded.\n"
        "- **`pjm` is the canonical CLI command** (since v0.0.4). The legacy "
        "`projectmem` alias still works if installed.\n\n"
        "## Minimal prompt for AI tools (Universal Mode)\n\n"
        "Read `.projectmem/AI_INSTRUCTIONS.md`, `.projectmem/summary.md`, and "
        "`.projectmem/PROJECT_MAP.md` before working. This project uses mandatory "
        "memory tracking with auto-capture enabled. If summary.md contains "
        "placeholder text, populate it via `pjm decision` and `pjm note` (or the "
        "`add_decision` / `add_note` MCP tools) — never edit summary.md directly. "
        "Git hooks log commits, reverts, and merges automatically. You MUST still "
        "run `pjm log` when you find a bug, `pjm attempt` for fix attempts, "
        "`pjm fix` when confirmed, and `pjm decision` for architectural choices. "
        "Skipping these steps means your work is incomplete.\n"
    )


def initial_project_map(root: Path) -> str:
    project_name = root.name
    return (
        f"# Project Map - {project_name}\n\n"
        "Status: not created yet. Fill the sections below with the project's real "
        "structure and key relationships — the first AI session can do it "
        "(see `.projectmem/AI_INSTRUCTIONS.md`).\n\n"
        "## Project purpose\n"
        "Not described yet.\n\n"
        "## Structure\n\n"
        "## Relationships\n"
    )


def initial_plan(root: Path) -> str:
    project_name = root.name
    return (
        f"# {project_name} — plan\n\n"
        "> Editable **intent** file: ideas + plans — what we *mean to do*.\n"
        "> This is NOT the event log. `events.jsonl` -> `summary.md` records what\n"
        "> *happened*; this file records what we *intend*. The AI reads it at\n"
        "> session start and edits it directly (like `PROJECT_MAP.md`): add ideas\n"
        "> and plans, check items off, move done work down to Shipped. Plans are\n"
        "> never logged as events.\n\n"
        "## Ideas\n"
        "_Loose thoughts, not yet committed to._\n\n"
        "## Active plans\n"
        "_What we're working toward now. Use `- [ ]` / `- [x]` checklists._\n\n"
        "## Next\n"
        "_Queued, but not started._\n\n"
        "## Someday / maybe\n\n"
        "## Shipped\n"
        "_Move completed plans here so the top stays about the future._\n"
    )


def ensure_gitignore_entry(root: Path) -> None:
    """Add projectmem's runtime + scratch files to .gitignore.

    Default policy: commit distilled team knowledge (summary.md, PROJECT_MAP.md,
    AI_INSTRUCTIONS.md, issues/), ignore the raw log + runtime files.
    For total privacy, users can manually add `.projectmem/` to their .gitignore.
    """
    gitignore = root / ".gitignore"
    entries = [
        f"{MEM_DIR}/{EVENTS_FILE}",
        f"{MEM_DIR}/{SUMMARY_INDEX_FILE}",
        f"{MEM_DIR}/{EVENTS_STATE_FILE}",
        f"{MEM_DIR}/search.sqlite3",
        f"{MEM_DIR}/search.sqlite3-journal",
        f"{MEM_DIR}/search.sqlite3-wal",
        f"{MEM_DIR}/search.sqlite3-shm",
        f"{MEM_DIR}/.search.sqlite3.*.tmp",
        f"{MEM_DIR}/{TRANSACTION_LOCK_FILE}",
        f"{MEM_DIR}/watch.pid",
        f"{MEM_DIR}/watch.log",
        f"{MEM_DIR}/structure.json",
    ]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = existing.splitlines()
    new_entries = [e for e in entries if e not in existing_lines]
    if not new_entries:
        return
    prefix = "" if not existing_lines or existing_lines[-1] == "" else "\n"
    gitignore.write_text(existing + prefix + "\n".join(new_entries) + "\n", encoding="utf-8")


def _append_event_line(path: Path, event: Event) -> None:
    """Append one complete JSONL record with a single system-level write."""
    payload = (json.dumps(event.to_dict(), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - guarded against broken filesystems
                raise OSError("Could not append event record")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a text file atomically, cleaning up an interrupted temp write."""
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _events_metadata(path: Path) -> dict[str, int]:
    """Return the cheap identity/length receipt for an events log."""
    stat_result = path.stat()
    return {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def events_metadata(root: Path | None = None) -> dict[str, int]:
    """Return identity/length metadata for a project's events log."""
    return _events_metadata(events_path(root))


def events_state_matches(
    state: dict[str, object] | None, metadata: dict[str, int]
) -> bool:
    """Check whether an append receipt describes the current events file.

    The receipt is maintained by ``append_event`` while holding the project
    transaction lock.  A missing/stale receipt means the file may have been
    replaced out of band; consumers must then rebuild the projection.
    """
    if not isinstance(state, dict):
        return False
    for key in ("device", "inode", "size", "mtime_ns"):
        if state.get(key) != metadata.get(key):
            return False
    return True


def read_events_state(root: Path | None = None) -> dict[str, object] | None:
    """Read the disposable append receipt, returning ``None`` if invalid."""
    try:
        path = events_state_path(root)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ProjectMemError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if not all(
        isinstance(data.get(key), int)
        for key in ("device", "inode", "size", "mtime_ns")
    ):
        return None
    if not isinstance(data.get("rebuild_required", False), bool):
        return None
    return data


def write_events_state(
    root: Path | None = None, *, rebuild_required: bool = False
) -> dict[str, object]:
    """Record the current events file identity under the project lock.

    This is a derived receipt, not another source of truth.  If it is lost or
    stale, the summary generator deliberately falls back to parsing the full
    append-only log.
    """
    project_root = root or Path.cwd()
    path = events_path(project_root)
    metadata = _events_metadata(path)
    state: dict[str, object] = {
        "version": 1,
        **metadata,
        "rebuild_required": bool(rebuild_required),
    }
    _atomic_write_text(
        events_state_path(project_root),
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return state


def read_events(root: Path | None = None) -> list[Event]:
    path = events_path(root)
    events: list[Event] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(Event.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ProjectMemError(f"Invalid event at {path}:{line_number}: {exc}") from exc
    return events


def read_events_from_offset(
    root: Path | None, offset: int
) -> tuple[list[Event], int]:
    """Parse only complete JSONL records at or after ``offset``.

    ``offset`` must be a byte boundary previously returned by this function or
    by a completed full read.  A partial final line is rejected so an
    interrupted append cannot be accepted as a valid incremental projection.
    """
    path = events_path(root)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ProjectMemError(f"Invalid events projection offset: {offset!r}")
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise ProjectMemError(f"Could not stat events file {path}: {exc}") from exc
    if offset > file_size:
        raise ProjectMemError(
            f"Events projection offset {offset} exceeds log size {file_size}."
        )

    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()
    end_offset = offset + len(raw)
    if not raw:
        return [], end_offset
    if not raw.endswith(b"\n"):
        raise ProjectMemError(f"Incomplete event record at end of {path}.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectMemError(f"Invalid UTF-8 in events suffix {path}: {exc}") from exc

    events: list[Event] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(Event.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ProjectMemError(
                f"Invalid event in suffix {path} (line {line_number}): {exc}"
            ) from exc
    return events, end_offset


def normalize_issue_id(issue_id: str | None) -> str | None:
    """Normalize numeric issue references while preserving legacy IDs."""
    if issue_id is None:
        return None
    cleaned = issue_id.strip().lstrip("#")
    if not cleaned:
        return None
    return cleaned.zfill(4) if cleaned.isdigit() else cleaned


def issue_exists(events: list[Event], issue_id: str | None) -> bool:
    """Return whether an issue event exists for ``issue_id``."""
    normalized = normalize_issue_id(issue_id)
    if normalized is None:
        return False
    return any(
        event.type == "issue"
        and normalize_issue_id(event.issue_id) == normalized
        for event in events
    )


def closed_issue_ids(events: list[Event]) -> set[str]:
    """Return issue IDs that already have a fix event."""
    return {
        normalized
        for event in events
        if event.type == "fix"
        and (normalized := normalize_issue_id(event.issue_id)) is not None
        and normalized
    }


def validate_issue_reference(
    events: list[Event], issue_id: str | None, *, require_open: bool = True
) -> str:
    """Validate and canonicalize an issue reference before appending an event."""
    normalized = normalize_issue_id(issue_id)
    if normalized is None:
        raise ProjectMemError("Issue ID cannot be empty.")
    if not issue_exists(events, normalized):
        raise ProjectMemError(
            f"Issue #{normalized} was not found. "
            "Run `pjm search <query>` or `pjm brief` to find the issue ID."
        )
    if require_open and normalized in closed_issue_ids(events):
        raise ProjectMemError(f"Issue #{normalized} is already closed.")
    return normalized


def validate_supersede_target(events: list[Event], reference: str) -> Event:
    """Resolve a supersede reference and enforce decision-only transitions."""
    target = resolve_event_ref(events, reference)
    if target.type != "decision":
        raise ValueError(
            f"Event {target.id} is a {target.type}; only decision events "
            "can be superseded."
        )
    if target.id in superseded_ids(events):
        raise ValueError(f"Decision {target.id} is already superseded.")
    return target


def append_event(event: Event, root: Path | None = None) -> Event:
    # Scrub known secret patterns out of user-supplied text fields BEFORE
    # they touch disk. "100% local" implies "100% your responsibility" only
    # if we let secrets leak through; default-on redaction is the trust
    # contract. Opt out via ``PROJECTMEM_NO_REDACT=1``.
    try:
        from projectmem.redaction import redact_event_fields

        fired = redact_event_fields(event)
        if fired:
            kinds = ", ".join(sorted(set(fired)))
            print(
                f"projectmem: redacted {len(fired)} secret(s) before write ({kinds})",
                file=sys.stderr,
            )
    except Exception:
        # Redaction is a guardrail, not a gatekeeper — if anything goes
        # wrong inside the scrubber we still write the event. Better a
        # logged secret than a lost event in a tool whose job is logging.
        pass

    path = events_path(root)
    project_root = path.parent.parent

    # Validate stateful references at the append boundary too.  Command
    # handlers perform the same checks for friendly UX, but auto-capture and
    # direct library callers also need to be unable to create dangling or
    # duplicate state transitions.  Keep the validation and append under the
    # same project lock so two callers cannot both validate against the same
    # stale snapshot.
    with project_transaction(project_root):
        # Keep a tiny writer-owned receipt alongside the append-only log.  It
        # lets summary regeneration distinguish a normal append from an
        # out-of-band truncate/replacement without hashing or reparsing the
        # historical JSONL prefix on every write.
        try:
            before_metadata = _events_metadata(path)
            before_state = read_events_state(project_root)
            rebuild_required = not events_state_matches(
                before_state, before_metadata
            ) or bool(
                before_state and before_state.get("rebuild_required", False)
            )
        except OSError:
            before_state = None
            rebuild_required = True

        existing_events: list[Event] | None = None

        def loaded_events() -> list[Event]:
            nonlocal existing_events
            if existing_events is None:
                existing_events = read_events(project_root)
            return existing_events

        if event.issue_id:
            normalized_issue_id = normalize_issue_id(event.issue_id)
            if normalized_issue_id is not None:
                event.issue_id = normalized_issue_id

        if event.type in ("attempt", "fix") and event.issue_id:
            validate_issue_reference(loaded_events(), event.issue_id, require_open=True)

        if event.supersedes:
            if event.type != "decision":
                raise ValueError("Only decision events can supersede another event.")
            validate_supersede_target(loaded_events(), event.supersedes)

        _append_event_line(path, event)
        try:
            write_events_state(project_root, rebuild_required=rebuild_required)
        except OSError:
            # The event itself is authoritative.  A missing receipt is safe:
            # the next summary regeneration will perform a full rebuild.
            pass

    # Auto-promote library-mentioning attempts/decisions/notes to the
    # machine-wide global store so projects with overlapping stacks inherit
    # the lesson. We pass the project's detected libraries so the promotion
    # is filtered by the current stack — a vite project mentioning "next"
    # in plain English won't surface a fake Next.js gotcha to other projects.
    # Failures here are non-fatal — the local event is already persisted;
    # only the optional cross-project propagation is at risk.
    if (
        event.type in ("attempt", "decision", "note")
        and event.summary
        and global_memory_enabled(project_root)
    ):
        try:
            from projectmem.global_memory import auto_promote_event, detect_stack

            # The global-memory module predates multi-project concurrency and
            # intentionally has no project registry dependency.  Reuse the
            # independent machine-global lock here so stack cache and gotcha
            # JSONL writes cannot interleave with another project's promotion.
            with _project_registry.global_registry_lock():
                stack = detect_stack(project_root)
                auto_promote_event(
                    event_summary=event.summary,
                    event_type=event.type,
                    project_name=project_root.name,
                    project_libraries=(
                        stack.get("libraries", []) + stack.get("tags", [])
                    ),
                    outcome=getattr(event, "outcome", None),
                )
        except Exception:
            # Auto-promote is a best-effort enrichment. Never let it break
            # the primary write path.
            pass

    return event


def next_issue_id(events: list[Event]) -> str:
    issue_ids = [
        int(event.issue_id)
        for event in events
        if event.type == "issue" and event.issue_id and event.issue_id.isdigit()
    ]
    return f"{(max(issue_ids) if issue_ids else 0) + 1:04d}"


def current_issue_id(events: list[Event]) -> str | None:
    closed = closed_issue_ids(events)
    for event in reversed(events):
        normalized = normalize_issue_id(event.issue_id)
        if event.type == "issue" and normalized and normalized not in closed:
            return normalized
    return None


CURRENT_ISSUE_MARKER = ".current_issue"


def current_issue_marker_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / CURRENT_ISSUE_MARKER


def write_current_issue(issue_id: str, root: Path | None = None) -> None:
    """Persist the active issue ID. Cleared on `pjm fix`."""
    try:
        with project_transaction(root) as project_root:
            _atomic_write_text(
                current_issue_marker_path(project_root), issue_id
            )
    except OSError:
        pass  # marker is advisory; don't fail the command


def read_current_issue(root: Path | None = None) -> str | None:
    """Read the active issue ID, if any. Returns None if no marker present."""
    try:
        path = current_issue_marker_path(root)
    except ProjectMemError:
        return None
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def clear_current_issue(root: Path | None = None) -> None:
    """Clear the active-issue marker. No-op if it does not exist."""
    try:
        with project_transaction(root) as project_root:
            path = current_issue_marker_path(project_root)
            path.unlink(missing_ok=True)
    except ProjectMemError:
        return
    except OSError:
        pass


def latest_open_issue_within(
    events: list[Event], minutes: int = 5
) -> str | None:
    """Return the most recent OPEN issue ID iff it was opened within `minutes`.

    Used as a time-fenced fallback for `pjm attempt` when no current-issue
    marker exists — avoids silently attaching an attempt to a stale issue.
    """
    from datetime import datetime, timedelta, timezone
    closed = closed_issue_ids(events)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for event in reversed(events):
        normalized = normalize_issue_id(event.issue_id)
        if event.type != "issue" or normalized is None or normalized in closed:
            continue
        try:
            ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if ts >= cutoff:
            return normalized
        return None  # most recent open is older than the window — no auto-attach
    return None


def get_git_commit(root: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit or None
