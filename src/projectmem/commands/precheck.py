"""Pre-commit warnings — the killer feature.

Compares staged changes against project memory and surfaces warnings BEFORE
the developer commits. This is the unique differentiator: nobody else can
warn you about repeating your own mistakes because nobody else has the
memory layer.

Usage:
    pjm precheck                          # Check staged files (default)
    pjm precheck --working                # Check working tree (not staged)
    pjm precheck --files X Y              # Check specific files
    pjm precheck --level info|warn|block  # Strictness
    pjm precheck --quiet                  # Only show warnings
"""
from __future__ import annotations

import subprocess
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event
from projectmem.storage import MEM_DIR, read_events


# ── Thresholds ──
HIGH_CHURN_THRESHOLD = 4         # changes in CHURN_WINDOW_COMMITS to trigger
CHURN_WINDOW_COMMITS = 7         # rolling window for churn detection
FAILED_ATTEMPT_BLOCK_COUNT = 3   # 3+ failed attempts → block (at --level block)
RECENT_DAYS = 30                 # only consider events newer than this

# ── Severity ──
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"

SEVERITY_LEVELS = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_BLOCK: 2}


def run(
    level: str = SEVERITY_WARN,
    working: bool = False,
    files: list[str] | None = None,
    quiet: bool = False,
    root: Path | None = None,
) -> None:
    """Run the pre-commit check."""
    root_path = root or Path.cwd()

    # Guard: only run if .projectmem exists
    if not (root_path / MEM_DIR).exists():
        return

    # Determine which files to check
    if files:
        target_files = files
    elif working:
        target_files = _get_working_tree_files(root_path)
    else:
        target_files = _get_staged_files(root_path)

    if not target_files:
        if not quiet:
            typer.echo("projectmem: No files to check.")
        return

    # Read events and build warnings
    try:
        events = read_events(root_path)
    except Exception:
        return  # Silent if memory can't be read

    warnings = _analyze_files(target_files, events)

    if not warnings:
        if not quiet:
            typer.echo("\033[32mprojectmem:\033[0m no warnings — looking good!")
        return

    # Render warnings
    has_blocking = any(w["severity"] == SEVERITY_BLOCK for w in warnings)
    _render_warnings(warnings, level)

    # Exit with non-zero if blocking and level is "block"
    if has_blocking and level == SEVERITY_BLOCK:
        raise typer.Exit(1)


def _analyze_files(
    files: list[str], events: list[Event]
) -> list[dict[str, Any]]:
    """Analyze each file against project memory, return warnings."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENT_DAYS)

    warnings: list[dict[str, Any]] = []

    for file_path in files:
        # Find all events referencing this file
        file_events = _events_for_file(file_path, events)

        if not file_events:
            continue

        # Filter to recent
        recent = []
        for e in file_events:
            try:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent.append(e)
            except (ValueError, AttributeError):
                recent.append(e)

        # ── Check 1: Failed attempts ──
        failed_attempts = [
            e for e in recent if e.type == "attempt" and e.outcome == "failed"
        ]
        if failed_attempts:
            count = len(failed_attempts)
            severity = SEVERITY_BLOCK if count >= FAILED_ATTEMPT_BLOCK_COUNT else SEVERITY_WARN
            last = failed_attempts[-1]
            warnings.append({
                "file": file_path,
                "severity": severity,
                "type": "failed_attempts",
                "title": f"{count} failed attempt{'s' if count != 1 else ''} on this file",
                "details": [
                    f"Last failure: {last.summary[:100]}",
                    f"  ({_age(last.timestamp)})",
                ],
            })

        # ── Check 2: Open issues ──
        open_issues = _find_open_issues(file_events, events)
        if open_issues:
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "open_issues",
                "title": f"{len(open_issues)} unresolved issue{'s' if len(open_issues) != 1 else ''} on this file",
                "details": [
                    f"#{issue.issue_id}: {issue.summary[:80]}"
                    for issue in open_issues[:3]
                ],
            })

        # ── Check 3: High churn ──
        # Source of truth is `git log` over the window, not the event log
        # (L-023a). Counting events would understate fresh, repeated edits
        # that the memory layer hasn't captured yet.
        git_churn = _git_recent_changes(file_path, RECENT_DAYS)
        churn_count = git_churn if git_churn is not None else sum(
            1 for e in recent if e.git_commit
        )
        if churn_count >= HIGH_CHURN_THRESHOLD:
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "high_churn",
                "title": f"HIGH CHURN: {churn_count} changes in last {RECENT_DAYS} days",
                "details": [
                    "May indicate unresolved architectural issue",
                ],
            })

        # ── Check 4: Recent reverts ──
        reverts = [
            e for e in recent
            if e.type == "attempt" and e.outcome == "failed"
            and e.capture_source == "git_post_revert"
        ]
        if reverts:
            last_revert = reverts[-1]
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "recent_revert",
                "title": "Recent revert affected this file",
                "details": [
                    f"Reverted: {last_revert.git_message or last_revert.summary[:80]}",
                    f"  ({_age(last_revert.timestamp)})",
                ],
            })

        # ── Check 5: Recent decisions ──
        decisions = [e for e in recent if e.type == "decision"]
        if decisions:
            last = decisions[-1]
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_INFO,
                "type": "relevant_decision",
                "title": "Recent decision affects this file",
                "details": [
                    f"{last.summary[:100]}",
                    f"  ({_age(last.timestamp)})",
                ],
            })

    return warnings


def _events_for_file(file_path: str, events: list[Event]) -> list[Event]:
    """Return all events that reference this file."""
    matching: list[Event] = []
    for e in events:
        # Direct files list
        if file_path in e.files:
            matching.append(e)
            continue
        # Location field
        if e.location:
            loc_file = e.location.split(":")[0]
            if loc_file == file_path:
                matching.append(e)
                continue
        # Summary mention
        if file_path in e.summary:
            matching.append(e)
    return matching


def _find_open_issues(
    file_events: list[Event], all_events: list[Event]
) -> list[Event]:
    """Find issues on this file that haven't been fixed."""
    issues = [e for e in file_events if e.type == "issue"]
    if not issues:
        return []

    # Find fix IDs to exclude resolved issues
    resolved_ids = {
        e.issue_id for e in all_events if e.type == "fix" and e.issue_id
    }

    return [i for i in issues if i.issue_id not in resolved_ids]


def _render_warnings(warnings: list[dict[str, Any]], level: str) -> None:
    """Render warnings to terminal with colors."""
    bold = "\033[1m"
    dim = "\033[2m"
    yellow = "\033[33m"
    red = "\033[31m"
    cyan = "\033[36m"
    reset = "\033[0m"

    severity_threshold = SEVERITY_LEVELS.get(level, 1)

    # Filter by severity level
    visible = [
        w for w in warnings
        if SEVERITY_LEVELS.get(w["severity"], 1) >= severity_threshold - 1  # always show at level-1+
    ]
    # Always show warn+ regardless of level
    visible = [
        w for w in warnings
        if SEVERITY_LEVELS.get(w["severity"], 1) >= 1  # warn or block
        or level == SEVERITY_INFO
    ]

    if not visible:
        return

    typer.echo("")
    typer.echo(f"{bold}projectmem: Pre-Commit Check{reset}")
    typer.echo(f"{dim}{'─' * 60}{reset}")
    typer.echo("")

    # Group by file
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for w in visible:
        by_file[w["file"]].append(w)

    for file_path, file_warnings in by_file.items():
        typer.echo(f"  {bold}{file_path}{reset}")
        for w in file_warnings:
            if w["severity"] == SEVERITY_BLOCK:
                icon = f"{red}BLOCK{reset}"
            elif w["severity"] == SEVERITY_WARN:
                icon = f"{yellow}WARN{reset}"
            else:
                icon = f"{cyan}INFO{reset}"
            typer.echo(f"    {icon}  {w['title']}")
            for detail in w["details"]:
                typer.echo(f"           {dim}{detail}{reset}")
        typer.echo("")

    typer.echo(f"{dim}{'─' * 60}{reset}")

    blocking = sum(1 for w in visible if w["severity"] == SEVERITY_BLOCK)
    warning = sum(1 for w in visible if w["severity"] == SEVERITY_WARN)

    if blocking and level == SEVERITY_BLOCK:
        typer.echo(f"{red}Blocked: {blocking} critical warning(s).{reset}")
        typer.echo(f"  Bypass once: git commit --no-verify")
    elif warning or blocking:
        typer.echo(
            f"{dim}{warning + blocking} warning(s). Review before committing.{reset}"
        )
    typer.echo("")


def _get_staged_files(root: Path) -> list[str]:
    """Get list of files staged for commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _git_recent_changes(file_path: str, days: int, root: Path | None = None) -> int | None:
    """Count commits touching `file_path` in the last `days` days.

    Returns None if git is unavailable or the call fails — caller falls back
    to event-log counting.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days}.days.ago", "--oneline", "--", file_path],
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def _get_working_tree_files(root: Path) -> list[str]:
    """Get list of modified files in working tree (not yet staged)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _age(timestamp: str) -> str:
    """Convert timestamp to human-readable age."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            return "just now" if hours == 0 else f"{hours}h ago"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{days // 7}w ago"
        return f"{days // 30}mo ago"
    except (ValueError, AttributeError):
        return "unknown"
