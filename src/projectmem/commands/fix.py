from __future__ import annotations

import typer

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    append_event,
    clear_current_issue,
    current_issue_id,
    get_git_commit,
    read_current_issue,
    read_events,
)
from projectmem.summary import regenerate_summary


def run(text: str, location: str | None = None) -> Event:
    """Close the current issue with a fix. Returns the created fix Event.

    Clears the current-issue marker so subsequent `pjm attempt` calls do not
    silently re-attach to the just-closed issue (the L-027a misattribution bug).
    """
    events = read_events()
    issue_id = read_current_issue() or current_issue_id(events)
    if issue_id is None:
        raise ProjectMemError("No open issue found. Run `pjm log <text>` first.")

    event = Event(
        type="fix",
        issue_id=issue_id,
        summary=text,
        git_commit=get_git_commit(),
        location=location,
    )
    append_event(event)
    clear_current_issue()
    regenerate_summary()
    typer.echo(f"Fixed issue #{issue_id}")
    return event
