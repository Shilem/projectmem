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
