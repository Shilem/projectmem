from __future__ import annotations

import re

from projectmem.models import Event
from projectmem.storage import read_events


def search_events(query: str, regex: bool = False) -> list[Event]:
    """Search the event log.

    Default mode is case-insensitive substring match against the summary,
    notes, `location`, and `files` array. With ``regex=True`` the query is
    treated as a Python regex (case-insensitive) — useful for OR-patterns
    like ``"carousel|favicon"`` that previously returned `No matches.`
    because the literal pipe character isn't in any event (L-027c).
    Location matching (0.1.4) makes per-file lookups work the way precheck
    does — `pjm search payment.py --failed-only` finds attempts logged
    with `--at payment.py`.
    """
    events = read_events()
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            # Bad regex → fall back to literal substring rather than crash.
            return search_events(query, regex=False)
        return [
            event
            for event in events
            if pattern.search(event.summary)
            or (event.notes and pattern.search(event.notes))
            or (event.location and pattern.search(event.location))
            or any(pattern.search(f) for f in event.files)
        ]
    needle = query.casefold()
    return [
        event
        for event in events
        if needle in event.summary.casefold()
        or (event.notes and needle in event.notes.casefold())
        or (event.location and needle in event.location.casefold())
        or any(needle in file_path.casefold() for file_path in event.files)
    ]
