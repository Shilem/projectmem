from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import (
    append_event,
    get_git_commit,
    project_transaction,
    read_events,
    validate_supersede_target,
)
from projectmem.summary import regenerate_summary


def run(
    text: str,
    location: str | None = None,
    supersedes: str | None = None,
    *,
    root: Path | None = None,
) -> Event:
    with project_transaction(root) as project_root:
        superseded = None
        events = read_events(project_root)
        if supersedes:
            # Resolve BEFORE appending so a bad reference never half-writes.
            # Raises ValueError — the CLI converts it to exit code 1, the MCP
            # safe_tool wrapper converts it to a readable error string.
            superseded = validate_supersede_target(events, supersedes)
        event = Event(
            type="decision",
            summary=text,
            git_commit=get_git_commit(project_root),
            location=location,
            supersedes=superseded.id if superseded else None,
        )
        append_event(event, project_root)
        regenerate_summary(project_root)
    if superseded:
        typer.echo(
            f'Recorded decision (supersedes {superseded.id}: '
            f'"{superseded.summary[:60]}")'
        )
    else:
        typer.echo("Recorded decision")
    return event
