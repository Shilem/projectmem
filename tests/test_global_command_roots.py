from __future__ import annotations

from projectmem.commands import attempt, decision, fix, log, note, search
from projectmem.search_index import search_index_path
from projectmem.storage import initialize


def test_command_handlers_isolate_explicit_project_roots(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    initialize(project_a, global_enabled=False)
    initialize(project_b, global_enabled=False)

    events_b = project_b / ".projectmem" / "events.jsonl"
    summary_b = project_b / ".projectmem" / "summary.md"
    issues_b = project_b / ".projectmem" / "issues"
    before_b_events = events_b.read_bytes()
    before_b_summary = summary_b.read_bytes()
    before_b_issues = sorted(path.name for path in issues_b.iterdir())

    # Keep a different initialized project as CWD so every omitted root would
    # be observable as a write or derived-artifact leak into project B.
    monkeypatch.chdir(project_b)

    issue = log.run("A-only issue marker", root=project_a)
    attempt.run(
        "A-only attempt marker",
        worked=True,
        failed=False,
        partial=False,
        root=project_a,
    )
    decision.run("A-only decision marker", root=project_a)
    note.run("A-only note marker", root=project_a)
    fix.run("A-only fix marker", issue=issue.issue_id, root=project_a)

    search.run("A-only", root=project_a)
    output_a = capsys.readouterr().out
    assert "A-only issue marker" in output_a
    assert search_index_path(project_a).is_file()
    assert not search_index_path(project_b).exists()

    # Searching B must use B's own source log and projection, not A's index.
    search.run("A-only", root=project_b)
    output_b = capsys.readouterr().out
    assert "No matches." in output_b
    assert search_index_path(project_b).is_file()
    assert b"A-only" not in search_index_path(project_b).read_bytes()

    # A's events, issue projection, summary, and search index are all confined
    # to A; B remains byte-for-byte unchanged until its own search creates its
    # own disposable index.
    assert b"A-only" not in events_b.read_bytes()
    assert events_b.read_bytes() == before_b_events
    assert summary_b.read_bytes() == before_b_summary
    assert sorted(path.name for path in issues_b.iterdir()) == before_b_issues
