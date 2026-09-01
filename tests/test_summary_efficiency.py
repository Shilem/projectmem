from __future__ import annotations

import json
from pathlib import Path

import projectmem.summary as summary_module
from projectmem.models import Event
from projectmem.storage import append_event, initialize, summary_index_path


def _append_issue_with_attempt(root: Path) -> None:
    append_event(
        Event(type="issue", issue_id="0001", summary="summary write race"),
        root,
    )
    append_event(
        Event(
            type="attempt",
            issue_id="0001",
            summary="measure complete issue output",
            outcome="partial",
        ),
        root,
    )


def test_regenerate_does_not_replace_unchanged_derived_files(tmp_path, monkeypatch):
    initialize(tmp_path)
    _append_issue_with_attempt(tmp_path)

    summary_module.regenerate_summary(tmp_path)
    summary_path = tmp_path / ".projectmem" / "summary.md"
    issue_path = next((tmp_path / ".projectmem" / "issues").glob("0001-*.md"))
    original_content = {
        summary_path: summary_path.read_text(encoding="utf-8"),
        issue_path: issue_path.read_text(encoding="utf-8"),
    }
    writes: list[tuple[Path, str]] = []

    def record_write(path, content):
        writes.append((path, content))
        raise AssertionError("unchanged derived content should not be written")

    monkeypatch.setattr(summary_module, "_atomic_write", record_write)
    summary_module.regenerate_summary(tmp_path)

    assert writes == []
    assert summary_path.read_text(encoding="utf-8") == original_content[summary_path]
    assert issue_path.read_text(encoding="utf-8") == original_content[issue_path]


def test_regenerate_atomically_replaces_changed_files_with_complete_content(
    tmp_path, monkeypatch
):
    initialize(tmp_path)
    append_event(
        Event(type="issue", issue_id="0001", summary="summary write race"),
        tmp_path,
    )
    summary_module.regenerate_summary(tmp_path)

    append_event(
        Event(
            type="attempt",
            issue_id="0001",
            summary="measure complete issue output",
            outcome="partial",
        ),
        tmp_path,
    )
    summary_path = tmp_path / ".projectmem" / "summary.md"
    issue_path = next((tmp_path / ".projectmem" / "issues").glob("0001-*.md"))
    observations: list[tuple[Path, Path, str]] = []
    real_replace = summary_module.os.replace

    def inspect_before_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        observations.append(
            (source_path, destination_path, source_path.read_text(encoding="utf-8"))
        )
        return real_replace(source, destination)

    monkeypatch.setattr(summary_module.os, "replace", inspect_before_replace)
    summary_module.regenerate_summary(tmp_path)

    assert {destination for _, destination, _ in observations} == {
        summary_path,
        issue_path,
        tmp_path / ".projectmem" / "summary.index.json",
    }
    for source, destination, content in observations:
        assert source.parent == destination.parent
        assert content == destination.read_text(encoding="utf-8")
    assert "measure complete issue output" in summary_path.read_text(encoding="utf-8")
    assert "measure complete issue output" in issue_path.read_text(encoding="utf-8")


def test_incremental_regeneration_writes_only_affected_issue_file(tmp_path, monkeypatch):
    initialize(tmp_path, global_enabled=False)
    append_event(Event(type="issue", issue_id="0001", summary="first issue"), tmp_path)
    append_event(Event(type="issue", issue_id="0002", summary="second issue"), tmp_path)
    summary_module.regenerate_summary(tmp_path)

    writes: list[Path] = []
    real_write_if_changed = summary_module._write_if_changed

    def record_write(path, content, **kwargs):
        writes.append(path)
        return real_write_if_changed(path, content, **kwargs)

    monkeypatch.setattr(summary_module, "_write_if_changed", record_write)
    append_event(
        Event(
            type="attempt",
            issue_id="0001",
            summary="only first issue changed",
            outcome="partial",
        ),
        tmp_path,
    )
    summary_module.regenerate_summary(tmp_path)

    assert tmp_path / ".projectmem/issues/0001-first-issue.md" in writes
    assert tmp_path / ".projectmem/issues/0002-second-issue.md" not in writes


def test_legacy_oversized_decision_projection_is_trimmed_without_replay(
    tmp_path, monkeypatch
):
    initialize(tmp_path, global_enabled=False)
    summary_module.regenerate_summary(tmp_path)

    index_path = summary_index_path(tmp_path)
    projection = json.loads(index_path.read_text(encoding="utf-8"))
    projection["decisions"] = [
        {
            "id": f"legacy-{index}",
            "type": "decision",
            "summary": f"legacy decision {index}",
        }
        for index in range(summary_module.MAX_DECISIONS + 5)
    ]
    projection["superseded"] = [
        f"legacy-retired-{index}"
        for index in range(summary_module.MAX_SUPERSEDED + 5)
    ]
    index_path.write_text(json.dumps(projection), encoding="utf-8")

    def unexpected_full_read(*args, **kwargs):
        raise AssertionError("bounded legacy projection should not replay events")

    monkeypatch.setattr(summary_module, "read_events", unexpected_full_read)
    summary_module.regenerate_summary(tmp_path)

    repaired = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(repaired["decisions"]) <= summary_module.MAX_DECISIONS
    assert len(repaired["superseded"]) <= summary_module.MAX_SUPERSEDED
    public_summary = (tmp_path / ".projectmem" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert "legacy decision 0" not in public_summary
