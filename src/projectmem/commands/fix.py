from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    append_event,
    clear_current_issue,
    current_issue_id,
    get_git_commit,
    normalize_issue_id,
    project_transaction,
    read_current_issue,
    read_events,
    validate_issue_reference,
)
from projectmem.summary import regenerate_summary


def _normalize_issue_id(issue_id: str | None) -> str | None:
    """Backward-compatible wrapper around the shared issue parser."""
    return normalize_issue_id(issue_id)


def run(
    text: str,
    location: str | None = None,
    issue: str | None = None,
    *,
    root: Path | None = None,
) -> Event:
    """Close an issue with a fix. Returns the created fix Event.

    When `issue` is omitted, this preserves the existing behavior:
    close the current issue and clear the current-issue marker.

    When `issue` is provided, the fix is attached to that specific issue.
    The current-issue marker is only cleared if it points at the same issue.
    """
    with project_transaction(root) as project_root:
        events = read_events(project_root)
        requested_issue_id = _normalize_issue_id(issue)
        active_issue_id: str | None = None
        marker = _normalize_issue_id(read_current_issue(project_root))
        if marker is not None:
            try:
                active_issue_id = validate_issue_reference(
                    events, marker, require_open=True
                )
            except ProjectMemError:
                # The marker is advisory and may outlive a manually edited or
                # imported event log. Do not let it route a fix into a closed or
                # missing issue.
                clear_current_issue(project_root)
        if active_issue_id is None:
            active_issue_id = current_issue_id(events)

        if issue is not None:
            issue_id = validate_issue_reference(
                events, requested_issue_id, require_open=True
            )
        else:
            issue_id = active_issue_id

        if issue_id is None:
            raise ProjectMemError("No open issue found. Run `pjm log <text>` first.")

        event = Event(
            type="fix",
            issue_id=issue_id,
            summary=text,
            git_commit=get_git_commit(project_root),
            location=location,
        )
        append_event(event, project_root)

        # Preserve old behavior when no specific issue was requested. For targeted
        # fixes, only clear the active marker if it matches the issue being fixed.
        if issue is None or active_issue_id == issue_id:
            clear_current_issue(project_root)

        regenerate_summary(project_root)
    typer.echo(f"Fixed issue #{issue_id}")
    return event
