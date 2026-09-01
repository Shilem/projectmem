from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    append_event,
    clear_current_issue,
    get_git_commit,
    latest_open_issue_within,
    next_issue_id,
    normalize_issue_id,
    project_transaction,
    read_current_issue,
    read_events,
    validate_issue_reference,
    write_current_issue,
)
from projectmem.summary import regenerate_summary

# How recent the most-recent OPEN issue must be for a markerless attempt
# to silently auto-attach. Beyond this, we error rather than misattribute.
AUTO_ATTACH_WINDOW_MINUTES = 5


def run(
    text: str,
    *,
    worked: bool,
    failed: bool,
    partial: bool,
    location: str | None = None,
    issue: str | None = None,
    auto_issue: bool = False,
    root: Path | None = None,
) -> Event:
    """Record an attempt on the active issue.

    Resolution order for the target issue:
      1. ``issue`` argument (explicit attribution).
      2. ``.projectmem/.current_issue`` marker (written by `pjm log` / `pjm fix`).
      3. The most-recent OPEN issue *if* opened within the last
         ``AUTO_ATTACH_WINDOW_MINUTES`` minutes — protects against the
         L-027a misattribution where a closed-then-attempted flow would
         silently latch onto an unrelated open issue.
      4. If ``auto_issue`` is true (or no recent issue exists at all),
         auto-create an implicit parent issue using ``text`` as its summary
         (L-008: removes the "no open issue" UX friction).
    """
    selected = [
        name
        for name, flag in [
            ("worked", worked),
            ("failed", failed),
            ("partial", partial),
        ]
        if flag
    ]
    if len(selected) > 1:
        raise ProjectMemError("Use only one of --worked, --failed, or --partial.")

    with project_transaction(root) as project_root:
        events = read_events(project_root)

        issue_id: str | None = None
        if issue is not None:
            # Explicit attribution must never silently create a parent issue or
            # attach to a closed one. Normalize the same numeric forms accepted by
            # `pjm fix` (1 / 001 / 0001).
            issue_id = validate_issue_reference(events, issue, require_open=True)
        else:
            marker = normalize_issue_id(read_current_issue(project_root))
            if marker is not None:
                try:
                    issue_id = validate_issue_reference(
                        events, marker, require_open=True
                    )
                except ProjectMemError:
                    # A stale marker is advisory. Clear it before considering the
                    # time-fenced fallback so it cannot route a new attempt into a
                    # closed or deleted issue.
                    clear_current_issue(project_root)
            if issue_id is None:
                issue_id = latest_open_issue_within(
                    events, minutes=AUTO_ATTACH_WINDOW_MINUTES
                )

        if issue_id is None:
            if not auto_issue:
                raise ProjectMemError(
                    "No active issue. Run `pjm log \"<summary>\"` first, "
                    "pass `--issue <id>` to attach explicitly, or rerun with "
                    "`--auto-issue` to auto-create a parent issue from this "
                    "attempt's text."
                )
            # Auto-create an implicit parent issue (L-008).
            new_id = next_issue_id(events)
            append_event(
                Event(
                    type="issue",
                    issue_id=new_id,
                    summary=text,
                    git_commit=get_git_commit(project_root),
                    location=location,
                ),
                project_root,
            )
            write_current_issue(new_id, project_root)
            issue_id = new_id

        outcome = selected[0] if selected else "partial"
        event = Event(
            type="attempt",
            issue_id=issue_id,
            summary=text,
            outcome=outcome,
            git_commit=get_git_commit(project_root),
            location=location,
        )
        append_event(event, project_root)
        regenerate_summary(project_root)
    typer.echo(f"Recorded {outcome} attempt on #{issue_id}")
    return event
