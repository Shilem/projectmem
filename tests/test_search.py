from __future__ import annotations

from typer.testing import CliRunner

from projectmem.cli import app


def test_search_finds_matching_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "token expiry bug"], catch_exceptions=False)
    runner.invoke(app, ["note", "startup is slow"], catch_exceptions=False)

    result = runner.invoke(app, ["search", "token"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "token expiry bug" in result.stdout
    assert "startup is slow" not in result.stdout
    assert (tmp_path / ".projectmem" / "search.sqlite3").is_file()


def test_plain_search_uses_index_and_preserves_failed_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "callback failure"], catch_exceptions=False)
    runner.invoke(
        app,
        ["attempt", "callback token redirect failed", "--failed"],
        catch_exceptions=False,
    )
    runner.invoke(app, ["note", "callback implementation note"], catch_exceptions=False)

    result = runner.invoke(
        app, ["search", "callback", "--failed-only", "--limit", "5"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "callback token redirect failed" in result.stdout
    assert "callback implementation note" not in result.stdout
