from __future__ import annotations

from pathlib import Path

import pytest

from projectmem.commands import auto_capture
from projectmem.storage import initialize, read_events


def _init_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "projectmem-home"))
    initialize(tmp_path, global_enabled=False)
    return tmp_path


def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_capture, "_git_last_message", lambda root: "fix: test")
    monkeypatch.setattr(
        auto_capture, "_git_last_changed_files", lambda root: ["src/example.py"]
    )
    monkeypatch.setattr(auto_capture, "get_git_commit", lambda root: "abc123")


def test_append_failure_is_recorded_and_does_not_escape(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    _stub_git(monkeypatch)

    def fail_append(*args, **kwargs):
        raise RuntimeError("append token=should-not-be-stored")

    monkeypatch.setattr(auto_capture, "append_event", fail_append)

    # A post-commit hook is best effort: an append failure must not fail Git.
    auto_capture.run(trigger="commit", root=root)

    diagnostics = auto_capture.read_diagnostics(root)
    assert len(diagnostics) == 1
    record = diagnostics[0]
    assert record["operation"] == "append"
    assert record["exception_type"] == "RuntimeError"
    assert record["timestamp"].endswith("Z")
    assert record["message"] == "append token=<redacted>"
    assert "should-not-be-stored" not in (
        root / ".projectmem" / auto_capture.AUTO_CAPTURE_DIAGNOSTICS_FILE
    ).read_text(encoding="utf-8")


def test_rebuild_failure_is_recorded_after_event_append(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    _stub_git(monkeypatch)

    def fail_rebuild(*args, **kwargs):
        raise OSError("summary rebuild failed password=secret")

    monkeypatch.setattr(auto_capture, "regenerate_summary", fail_rebuild)

    auto_capture.run(trigger="commit", root=root)

    events = read_events(root)
    assert any(event.git_commit == "abc123" for event in events)
    diagnostics = auto_capture.read_diagnostics(root)
    assert len(diagnostics) == 1
    assert diagnostics[0]["operation"] == "rebuild"
    assert diagnostics[0]["exception_type"] == "OSError"
    assert diagnostics[0]["message"] == "summary rebuild failed password=<redacted>"


def test_diagnostic_reader_rejects_corrupt_record(tmp_path, monkeypatch):
    root = _init_project(tmp_path, monkeypatch)
    path = root / ".projectmem" / auto_capture.AUTO_CAPTURE_DIAGNOSTICS_FILE
    path.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid diagnostic"):
        auto_capture.read_diagnostics(root)
