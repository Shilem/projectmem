from __future__ import annotations

import typer

from projectmem.search import search_events


def run(query: str, regex: bool = False) -> None:
    """Plain-text or regex search across events.

    By default this is a case-insensitive substring match. Use ``--regex``
    to enable Python regex syntax — including OR-patterns like
    ``"carousel|favicon"`` (L-027c).
    """
    matches = search_events(query, regex=regex)
    if not matches:
        if not regex and any(ch in query for ch in r"|*?+()[]\\"):
            typer.echo(
                "No matches. (Tip: substring match is the default — "
                "rerun with `--regex` if you intended an OR/regex pattern.)"
            )
        else:
            typer.echo("No matches.")
        return

    for event in matches:
        issue = f" #{event.issue_id}" if event.issue_id else ""
        outcome = f" ({event.outcome})" if event.outcome else ""
        typer.echo(f"{event.timestamp} {event.type}{issue}{outcome}: {event.summary}")
