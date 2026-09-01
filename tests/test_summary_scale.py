from __future__ import annotations

import json
from pathlib import Path

import projectmem.summary as summary_module
from projectmem.models import Event
from projectmem.storage import (
    append_event,
    events_state_path,
    initialize,
    summary_index_path,
)
from projectmem.summary import regenerate_summary


def test_incremental_projection_parses_only_new_event_after_1k_history(
    tmp_path: Path, monkeypatch
):
    initialize(tmp_path, global_enabled=False)
    for index in range(1000):
        append_event(Event(type="note", summary=f"historical note {index}"), tmp_path)
    regenerate_summary(tmp_path)

    original_from_dict = Event.from_dict
    parsed_summaries: list[str] = []

    def counted_from_dict(data):
        parsed_summaries.append(data.get("summary", ""))
        return original_from_dict(data)

    def unexpected_full_read(*args, **kwargs):
        raise AssertionError("incremental regeneration reparsed the full event log")

    monkeypatch.setattr(Event, "from_dict", counted_from_dict)
    monkeypatch.setattr(summary_module, "read_events", unexpected_full_read)

    append_event(Event(type="note", summary="one new note"), tmp_path)
    regenerate_summary(tmp_path)

    assert parsed_summaries == ["one new note"]
    assert "one new note" in (tmp_path / ".projectmem" / "summary.md").read_text(
        encoding="utf-8"
    )


def test_replaced_log_and_damaged_index_rebuild_projection(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    append_event(Event(type="issue", issue_id="0001", summary="old issue"), tmp_path)
    regenerate_summary(tmp_path)

    replacement = Event(type="issue", issue_id="0002", summary="replacement issue")
    (tmp_path / ".projectmem" / "events.jsonl").write_text(
        json.dumps(replacement.to_dict()) + "\n", encoding="utf-8"
    )
    regenerate_summary(tmp_path)

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    assert "replacement issue" in summary
    assert "old issue" not in summary
    assert [path.name for path in (tmp_path / ".projectmem" / "issues").glob("*.md")] == [
        "0002-replacement-issue.md"
    ]

    summary_index_path(tmp_path).write_text("{not-json", encoding="utf-8")
    regenerate_summary(tmp_path)
    repaired = json.loads(summary_index_path(tmp_path).read_text(encoding="utf-8"))
    assert repaired["events"]["count"] == 1
    assert events_state_path(tmp_path).exists()


def test_decision_projection_and_supersede_state_are_bounded(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)

    first = Event(type="decision", summary="chain decision 0")
    append_event(first, tmp_path)
    current = first
    # Every replacement retires the previous decision.  More replacements
    # than the supersede cap ensure an evicted pointer cannot make a retired
    # decision visible again in summary.md.
    for index in range(summary_module.MAX_SUPERSEDED + 5):
        replacement = Event(
            type="decision",
            summary=f"chain decision {index + 1}",
            supersedes=current.id,
        )
        append_event(replacement, tmp_path)
        current = replacement

    regenerate_summary(tmp_path)

    projection = json.loads(summary_index_path(tmp_path).read_text(encoding="utf-8"))
    assert len(projection["decisions"]) <= summary_module.MAX_DECISIONS
    assert len(projection["superseded"]) <= summary_module.MAX_SUPERSEDED

    public_summary = (tmp_path / ".projectmem" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert current.summary in public_summary
    assert "chain decision 0" not in public_summary
    assert "older or superseded decisions remain in events.jsonl" in public_summary

    # Projection pruning must never rewrite or delete the append-only source.
    event_ids = {event.id for event in summary_module.read_events(tmp_path)}
    assert first.id in event_ids
    assert current.id in event_ids


def test_public_summary_bounds_issue_count_and_rendered_event_text(tmp_path: Path):
    initialize(tmp_path, global_enabled=False)
    oversized = "x" * (summary_module.MAX_RENDERED_EVENT_CHARS + 80)
    total_issues = summary_module.MAX_SUMMARY_ISSUES + 3
    for index in range(total_issues):
        issue_id = f"{index + 1:04d}"
        append_event(
            Event(type="issue", issue_id=issue_id, summary=f"issue {index} {oversized}"),
            tmp_path,
        )
        append_event(
            Event(
                type="attempt",
                issue_id=issue_id,
                outcome="failed",
                summary=f"attempt {index} {oversized}",
            ),
            tmp_path,
        )
    regenerate_summary(tmp_path)

    public_summary = (tmp_path / ".projectmem" / "summary.md").read_text(
        encoding="utf-8"
    )
    issue_lines = [line for line in public_summary.splitlines() if line.startswith("- [")]
    assert len(issue_lines) == summary_module.MAX_SUMMARY_ISSUES
    assert "3 older issues remain searchable in events.jsonl." in public_summary
    assert oversized not in public_summary
    assert "issue 0" not in public_summary

    # The source log keeps all closed or omitted history for FTS retrieval.
    assert len(summary_module.read_events(tmp_path)) == total_issues * 2
