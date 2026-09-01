from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from projectmem.models import Event
from projectmem.search_index import (
    SearchIndexUnavailableError,
    check_index,
    ensure_index_current,
    is_index_current,
    rebuild_index,
    search_events,
    search_index_path,
)
from projectmem.storage import append_event, initialize


def _event_log(root: Path) -> Path:
    return root / ".projectmem" / "events.jsonl"


def test_rebuild_and_search_returns_bounded_metadata(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    append_event(
        Event(
            type="issue",
            issue_id="0007",
            summary="token expiry in callback",
            notes="long internal investigation details",
            location="src/auth/callback.py:42",
            files=["src/auth/callback.py"],
        ),
        tmp_path,
    )
    append_event(
        Event(type="note", summary="unrelated startup note"),
        tmp_path,
    )

    result = rebuild_index(tmp_path)
    assert result["rebuilt"] is True
    assert result["current"] is True
    assert is_index_current(tmp_path)

    matches = search_events("callback.py", 10, tmp_path)
    assert len(matches) == 1
    assert matches[0]["issue_id"] == "0007"
    assert matches[0]["location"] == "src/auth/callback.py:42"
    assert matches[0]["files"] == ["src/auth/callback.py"]
    assert matches[0]["source"] == "fts5"
    assert "notes" not in matches[0]

    assert search_events("0007", root=tmp_path)[0]["summary"] == (
        "token expiry in callback"
    )


def test_appended_log_is_detected_and_updated(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    append_event(Event(type="note", summary="first indexed event"), tmp_path)
    rebuild_index(tmp_path)
    append_event(Event(type="note", summary="second appended event"), tmp_path)

    stale = check_index(tmp_path)
    assert stale["current"] is False
    assert stale["status"] == "stale"

    refreshed = ensure_index_current(tmp_path)
    assert refreshed["current"] is True
    assert refreshed["rebuilt"] is True
    assert refreshed["event_count"] == 2
    assert search_events("appended", root=tmp_path)[0]["summary"] == (
        "second appended event"
    )


def test_missing_and_corrupt_index_are_rebuilt_without_event_loss(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    events = [
        Event(type="note", summary=f"unique event marker {index}")
        for index in range(6)
    ]
    for event in events:
        append_event(event, tmp_path)
    rebuild_index(tmp_path)
    index_path = search_index_path(tmp_path)

    index_path.unlink()
    missing = check_index(tmp_path)
    assert missing["status"] == "missing"
    ensure_index_current(tmp_path)
    assert check_index(tmp_path)["event_count"] == len(events)

    index_path.write_bytes(b"not a sqlite database")
    corrupt = check_index(tmp_path)
    assert corrupt["status"] == "corrupt"
    ensure_index_current(tmp_path)
    assert check_index(tmp_path)["current"] is True
    assert len(search_events("unique", 100, tmp_path)) == len(events)


def test_no_fts_match_falls_back_to_literal_source_search(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    event = Event(type="note", summary="@@@ literal marker @@@", notes="private")
    append_event(event, tmp_path)
    rebuild_index(tmp_path)

    matches = search_events("@@@", root=tmp_path)
    assert len(matches) == 1
    assert matches[0]["id"] == event.id
    assert matches[0]["source"] == "raw"
    assert "notes" not in matches[0]


def test_search_metadata_marks_superseded_decisions(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    original = Event(type="decision", summary="use bcrypt everywhere")
    append_event(original, tmp_path)
    append_event(
        Event(
            type="decision",
            summary="use argon2 instead",
            supersedes=original.id,
        ),
        tmp_path,
    )

    matches = search_events("bcrypt", root=tmp_path)

    assert len(matches) == 1
    assert matches[0]["id"] == original.id
    assert matches[0]["superseded"] is True


def test_search_limit_and_query_are_strictly_validated(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    for index in range(4):
        append_event(Event(type="note", summary=f"same marker {index}"), tmp_path)
    rebuild_index(tmp_path)

    assert len(search_events("same", 2, tmp_path)) == 2
    with pytest.raises(ValueError):
        search_events("", root=tmp_path)
    with pytest.raises(ValueError):
        search_events("same", 0, tmp_path)
    with pytest.raises(ValueError):
        search_events("same", 101, tmp_path)
    with pytest.raises(TypeError):
        search_events("same", True, tmp_path)


def test_index_contains_every_authoritative_event(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    events = []
    for index in range(12):
        event = Event(type="note", summary=f"all-events-{index}")
        append_event(event, tmp_path)
        events.append(event)
    rebuild_index(tmp_path)

    with sqlite3.connect(search_index_path(tmp_path)) as connection:
        indexed_count = connection.execute(
            "SELECT COUNT(*) FROM indexed_events"
        ).fetchone()[0]
        fts_count = connection.execute(
            "SELECT COUNT(*) FROM events_fts"
        ).fetchone()[0]
    assert indexed_count == fts_count == len(events)

    found_ids = {
        search_events(f"all-events-{index}", root=tmp_path)[0]["id"]
        for index in range(12)
    }
    assert found_ids == {event.id for event in events}
    raw_lines = [json.loads(line) for line in _event_log(tmp_path).read_text().splitlines()]
    assert len(raw_lines) == len(events)


def test_fts_unavailable_is_observable(tmp_path: Path, monkeypatch):
    initialize(tmp_path, global_enabled=False)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("no such module: fts5")

    import projectmem.search_index as module

    monkeypatch.setattr(module.sqlite3, "connect", unavailable)
    with pytest.raises(SearchIndexUnavailableError, match="FTS5"):
        check_index(tmp_path)
