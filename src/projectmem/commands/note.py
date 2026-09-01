from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import append_event, get_git_commit, project_transaction
from projectmem.summary import regenerate_summary


def run(
    text: str,
    location: str | None = None,
    *,
    root: Path | None = None,
) -> Event:
    with project_transaction(root) as project_root:
        event = Event(
            type="note",
            summary=text,
            git_commit=get_git_commit(project_root),
            location=location,
        )
        append_event(event, project_root)
        regenerate_summary(project_root)
    typer.echo("Recorded note")
    return event
