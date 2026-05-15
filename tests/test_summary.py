from __future__ import annotations

from typer.testing import CliRunner

from projectmem.cli import app


def test_summary_and_issue_file_are_regenerated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "login redirect loop"], catch_exceptions=False)
    runner.invoke(
        app,
        ["attempt", "changed redirect in auth/redirect.py:88", "--worked"],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["fix", "fixed redirect guard in auth/redirect.py:88"],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["decision", "Keep redirect handling in one middleware"],
        catch_exceptions=False,
    )

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    issue_files = list((tmp_path / ".projectmem" / "issues").glob("0001-*.md"))

    assert "# projectmem" in summary
    assert "[DONE] #0001 login redirect loop" in summary
    assert "fixed redirect guard in auth/redirect.py:88" in summary
    assert "Keep redirect handling in one middleware" in summary
    assert "`auth/redirect.py:88`" in summary
    assert len(issue_files) == 1
    assert "login redirect loop" in issue_files[0].read_text(encoding="utf-8")


def test_regenerate_preserves_project_purpose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    summary_path = tmp_path / ".projectmem" / "summary.md"
    summary = summary_path.read_text(encoding="utf-8")
    summary_path.write_text(
        summary.replace(
            "Replace this placeholder with a concise description of what this project "
            "does, who it serves, and the main technologies or runtime assumptions.",
            "A prediction ML project for evaluating demand forecasts.",
        ),
        encoding="utf-8",
    )

    runner.invoke(app, ["note", "models live in src/models"], catch_exceptions=False)

    regenerated = summary_path.read_text(encoding="utf-8")
    assert "A prediction ML project for evaluating demand forecasts." in regenerated
    assert "models live in src/models" in regenerated


def test_project_purpose_auto_syncs_from_project_map(tmp_path, monkeypatch):
    """L-037: when PROJECT_MAP.md has a real Project purpose and summary.md
    is still placeholder, regeneration should pull the purpose from
    PROJECT_MAP.md instead of echoing the placeholder back into summary.md."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)

    # Simulate the Setup Mode flow: AI writes a real Project purpose into
    # PROJECT_MAP.md (allowed — PROJECT_MAP.md is hand-editable) but
    # summary.md still has the init placeholder.
    map_path = tmp_path / ".projectmem" / "PROJECT_MAP.md"
    map_path.write_text(
        "# Project Map - test\n\n"
        "Status: created\n\n"
        "## Project purpose\n"
        "A neural network framework for academic research on graph "
        "convolutional networks (GCN) and attention mechanisms.\n\n"
        "## Main folders\n"
        "- `src/` — core library\n",
        encoding="utf-8",
    )

    # Any write tool triggers regenerate_summary. Use add_note (lightest).
    result = runner.invoke(
        app, ["note", "uses PyTorch for tensor ops"], catch_exceptions=False
    )
    assert result.exit_code == 0

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")

    # Real Project purpose from PROJECT_MAP.md now in summary.md
    assert "neural network framework for academic research" in summary
    # Placeholder is gone
    assert "Replace this placeholder" not in summary


def test_project_purpose_falls_back_when_project_map_is_placeholder(tmp_path, monkeypatch):
    """L-037: if PROJECT_MAP.md is still placeholder too, fall back to
    whatever summary.md already had (or the default placeholder)."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    # Both files still have init placeholders.

    runner.invoke(app, ["note", "first note"], catch_exceptions=False)

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    # Should remain placeholder — neither file has real Project purpose yet
    assert "Replace this placeholder" in summary
    assert "first note" in summary
