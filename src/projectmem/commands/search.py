from __future__ import annotations

import re
from pathlib import Path

import typer

from projectmem.models import Event, superseded_ids
from projectmem.search_index import (
    SearchIndexError,
    search_events as indexed_search_events,
)
from projectmem.storage import read_events


def _regex_search_events(query: str, events: list[Event]) -> list[Event]:
    """Search one explicit project's event list with legacy regex semantics."""
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        # Keep the legacy search command's invalid-regex behavior: treat the
        # query literally rather than making a malformed expression fatal.
        needle = query.casefold()
        return [
            event
            for event in events
            if needle in event.summary.casefold()
            or (event.notes and needle in event.notes.casefold())
            or (event.location and needle in event.location.casefold())
            or any(needle in file_path.casefold() for file_path in event.files)
        ]

    return [
        event
        for event in events
        if pattern.search(event.summary)
        or (event.notes and pattern.search(event.notes))
        or (event.location and pattern.search(event.location))
        or any(pattern.search(file_path) for file_path in event.files)
    ]


def run(
    query: str,
    regex: bool = False,
    failed_only: bool = False,
    limit: int = 20,
    *,
    root: Path | None = None,
) -> None:
    """Search event metadata with FTS5; ``--regex`` keeps the legacy matcher.

    Plain searches use the disposable SQLite FTS5 projection and return at
    most ``limit`` compact records.  Regex is deliberately an explicit
    full-log escape hatch because SQLite FTS5 does not implement Python
    regular expressions.  The raw log remains authoritative in both paths.
    """
    if regex:
        events = read_events(root)
        matches = _regex_search_events(query, events)[-limit:]
        if failed_only:
            matches = [
                event
                for event in matches
                if event.type == "attempt" and event.outcome == "failed"
            ]
        if not matches:
            typer.echo("No matches.")
            return

        retired = superseded_ids(events)
        for event in matches:
            issue = f" #{event.issue_id}" if event.issue_id else ""
            outcome = f" ({event.outcome})" if event.outcome else ""
            retired_tag = " (superseded)" if event.id in retired else ""
            typer.echo(
                f"{event.timestamp} [{event.id}] {event.type}{issue}{outcome}: "
                f"{event.summary}{retired_tag}"
            )
        return

    try:
        matches = indexed_search_events(query, limit=limit, root=root)
    except (SearchIndexError, ValueError, TypeError) as exc:
        typer.echo(f"Search index unavailable: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if failed_only:
        matches = [
            event
            for event in matches
            if event["type"] == "attempt" and event["outcome"] == "failed"
        ]
    if not matches:
        if any(ch in query for ch in r"|*?+()[]\\"):
            typer.echo(
                "No matches. (Tip: plain search treats regex characters "
                "literally; rerun with `--regex` if intended.)"
            )
        else:
            typer.echo("No matches.")
        return

    for event in matches:
        issue = f" #{event['issue_id']}" if event["issue_id"] else ""
        outcome = f" ({event['outcome']})" if event["outcome"] else ""
        retired_tag = " (superseded)" if event["superseded"] else ""
        typer.echo(
            f"{event['timestamp']} [{event['id']}] {event['type']}{issue}{outcome}: "
            f"{event['summary']}{retired_tag}"
        )
