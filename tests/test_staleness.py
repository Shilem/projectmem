from __future__ import annotations

import subprocess

from projectmem.models import Event
from projectmem.staleness import find_stale_events


def test_staleness_uses_one_git_query_per_file(tmp_path, monkeypatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("pass\n", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "2026-01-05T00:00:00+00:00\n"
                "2026-01-04T00:00:00+00:00\n"
                "2026-01-03T00:00:00+00:00\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("projectmem.staleness.subprocess.run", fake_run)
    events = [
        Event(
            type="decision",
            summary="older choice",
            location="src/engine.py",
            timestamp="2026-01-01T00:00:00Z",
        ),
        Event(
            type="note",
            summary="newer note",
            location="src/engine.py",
            timestamp="2026-01-02T00:00:00Z",
        ),
    ]

    flagged = find_stale_events(events, tmp_path)

    assert len(calls) == 1
    assert {item["event"].summary for item in flagged} == {
        "older choice",
        "newer note",
    }
