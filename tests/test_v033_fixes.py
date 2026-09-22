from __future__ import annotations

import ctypes
import io
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from projectmem.commands import hooks, precheck, watch
from projectmem.glyphs import _AsciiFoldingStream
from projectmem.models import Event
from projectmem.staleness import find_stale_events


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def test_precheck_uses_the_requested_root_for_git_churn(tmp_path, monkeypatch):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("pass\n", encoding="utf-8")
    seen = {}

    def fake_churn(file_path, days, root=None):
        seen["root"] = root
        return 0

    monkeypatch.setattr(precheck, "_git_recent_changes", fake_churn)
    events = [Event(type="note", summary="a", location="src/a.py", timestamp=_now())]

    precheck._analyze_files(["src/a.py"], events, root=tmp_path)

    assert seen["root"] == tmp_path


def test_precheck_drops_superseded_decisions(tmp_path, monkeypatch):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("pass\n", encoding="utf-8")
    old = Event(
        id="evt_old",
        type="decision",
        summary="old decision",
        location="src/a.py",
        timestamp=_now(),
    )
    current = Event(
        id="evt_current",
        type="decision",
        summary="current decision",
        location="src/a.py",
        timestamp=_now(),
        supersedes=old.id,
    )
    monkeypatch.setattr(precheck, "_git_recent_changes", lambda *args: 0)

    warnings = precheck._analyze_files(
        ["src/a.py"], [old, current], root=tmp_path
    )
    decision_warnings = [w for w in warnings if w["type"] == "relevant_decision"]

    assert len(decision_warnings) == 1
    assert "current decision" in " ".join(decision_warnings[0]["details"])
    assert "old decision" not in " ".join(decision_warnings[0]["details"])


def test_staleness_can_be_scoped_to_checked_files(tmp_path, monkeypatch):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")
    calls = []

    def fake_git(file_path, since_iso, root):
        calls.append(file_path)
        return [datetime(2026, 1, 4, tzinfo=timezone.utc)] * 3

    monkeypatch.setattr("projectmem.staleness._commit_timestamps_touching", fake_git)
    events = [
        Event(type="note", summary="a", location="a.py", timestamp="2026-01-01T00:00:00Z"),
        Event(type="note", summary="b", location="b.py", timestamp="2026-01-01T00:00:00Z"),
    ]

    flagged = find_stale_events(events, tmp_path, only_files={"a.py"})

    assert calls == ["a.py"]
    assert [item["file"] for item in flagged] == ["a.py"]


def test_legacy_mcp_precheck_forwards_resolved_root(tmp_path, monkeypatch):
    from projectmem import mcp_server

    seen = {}
    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mcp_server, "read_events", lambda root: [])

    def fake_analyze(files, events, root=None):
        seen["root"] = root
        return []

    monkeypatch.setattr("projectmem.commands.precheck._analyze_files", fake_analyze)
    # safe_tool's root guard is satisfied by the explicit root override above.
    mcp_server.precheck_file("src/a.py")

    assert seen["root"] == tmp_path


def test_hooks_use_sh_and_shell_safe_windows_paths(monkeypatch):
    monkeypatch.setattr(hooks.shutil, "which", lambda name: r"C:\Users\me\Scripts\pjm.exe")

    assert hooks.HOOK_SHEBANG == "#!/bin/sh\n"
    assert hooks._resolve_pjm_binary() == "C:/Users/me/Scripts/pjm.exe"


def test_watch_daemon_spawns_worker_process(tmp_path, monkeypatch):
    (tmp_path / ".projectmem").mkdir()
    process = MagicMock()
    process.pid = 4242
    process.poll.return_value = None
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)

    with patch.object(watch.subprocess, "Popen", return_value=process) as popen:
        watch._run_as_daemon(tmp_path)

    command = popen.call_args.args[0]
    assert command[-2:] == ["watch", "--worker"]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert (tmp_path / ".projectmem" / "watch.pid").read_text() == "4242"


class _FakeKernel32:
    def __init__(self, alive=(), denied=()):
        self.alive = set(alive)
        self.denied = set(denied)
        self.last_error = 0
        self.handles = {}
        self.next_handle = 10
        self.terminated = []

    def OpenProcess(self, access, inherit, pid):
        if pid in self.denied:
            self.last_error = 5
            return 0
        if pid not in self.alive:
            self.last_error = 87
            return 0
        self.next_handle += 1
        self.handles[self.next_handle] = pid
        return self.next_handle

    def GetLastError(self):
        return self.last_error

    def GetExitCodeProcess(self, handle, out):
        out._obj.value = 259 if self.handles[handle] in self.alive else 0
        return 1

    def TerminateProcess(self, handle, code):
        self.terminated.append(self.handles[handle])
        return 1

    def CloseHandle(self, handle):
        self.handles.pop(handle, None)
        return 1


def _fake_windows(monkeypatch, kernel):
    monkeypatch.setattr(watch.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(kernel32=kernel), raising=False
    )

    class Box:
        def __init__(self, value=0):
            self.value = value

    monkeypatch.setattr(ctypes, "c_ulong", Box)
    monkeypatch.setattr(ctypes, "byref", lambda obj: types.SimpleNamespace(_obj=obj))


def test_windows_pid_liveness_keeps_live_pid_file(tmp_path, monkeypatch):
    mem = tmp_path / ".projectmem"
    mem.mkdir()
    (mem / "watch.pid").write_text("4242", encoding="utf-8")
    _fake_windows(monkeypatch, _FakeKernel32(alive=(4242,)))

    assert watch._running_pid(tmp_path) == 4242
    assert (mem / "watch.pid").exists()


def test_windows_pid_liveness_treats_access_denied_as_alive(monkeypatch):
    _fake_windows(monkeypatch, _FakeKernel32(denied=(4242,)))

    assert watch._pid_alive(4242) is True


def test_ascii_folding_stream_removes_non_ascii_punctuation():
    output = io.StringIO()
    stream = _AsciiFoldingStream(output)

    stream.write("one — two… ‘three’ · four")

    assert output.getvalue() == "one -- two... 'three' * four"


def test_fix_without_issue_has_no_traceback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PROJECTMEM_HOME": str(tmp_path / "pm"),
        "HOME": str(tmp_path),
    }
    initialized = subprocess.run(
        [sys.executable, "-m", "projectmem.cli", "init", "--no-watch"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    result = subprocess.run(
        [sys.executable, "-m", "projectmem.cli", "fix", "nothing open"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "No open issue found" in result.stderr
    assert "Traceback" not in result.stderr
