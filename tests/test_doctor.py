from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectmem.commands import auto_capture, doctor
from projectmem.search_index import rebuild_index
from projectmem.storage import initialize


def _init_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "projectmem-home"))
    initialize(tmp_path, global_enabled=False)
    return tmp_path


def test_doctor_reports_corrupt_events_and_recent_capture_failures(
    tmp_path, monkeypatch, capsys
):
    root = _init_project(tmp_path, monkeypatch)
    events_path = root / ".projectmem" / "events.jsonl"
    events_path.write_text("{broken event}\n", encoding="utf-8")
    auto_capture._record_diagnostic(
        root, "append", RuntimeError("append failed token=secret")
    )

    report = doctor.run(root, "json")
    output = capsys.readouterr().out
    rendered = json.loads(output)

    assert report["status"] == "problems"
    assert rendered["events"]["status"] == "problem"
    assert rendered["auto_capture"]["status"] == "problem"
    assert rendered["auto_capture"]["records"][0]["operation"] == "append"
    assert rendered["auto_capture"]["records"][0]["message"] == (
        "append failed token=<redacted>"
    )


def test_doctor_healthy_report_does_not_rebuild_or_write_state(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    memory_dir = root / ".projectmem"
    before = {
        name: (memory_dir / name).read_bytes()
        for name in ("events.jsonl", "summary.md")
    }

    report = doctor.build_report(root)

    assert report["status"] == "healthy"
    assert report["events"]["status"] == "ok"
    assert report["summary"]["status"] == "ok"
    assert report["projection"]["status"] == "skipped"
    assert report["search_index"]["status"] == "skipped"
    assert report["auto_capture"]["status"] == "skipped"
    assert {
        name: (memory_dir / name).read_bytes()
        for name in ("events.jsonl", "summary.md")
    } == before


def test_doctor_flags_invalid_optional_projection(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    projection = root / ".projectmem" / "structure.json"
    projection.write_text("{broken projection}\n", encoding="utf-8")

    report = doctor.build_report(root)

    assert report["status"] == "problems"
    assert report["projection"]["status"] == "problem"
    assert report["projection"]["files"]["structure.json"]["status"] == "problem"


def test_doctor_reports_current_search_index_without_rebuilding(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    rebuild_index(root)

    report = doctor.build_report(root)

    assert report["status"] == "healthy"
    assert report["search_index"]["status"] == "ok"
    assert report["search_index"]["current"] is True
    assert report["search_index"]["events"] == 0
