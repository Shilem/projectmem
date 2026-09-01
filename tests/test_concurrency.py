from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path
from queue import Empty

import pytest

from projectmem.storage import initialize


def _log_worker(
    root: str, barrier: multiprocessing.synchronize.Barrier, count: int, results
) -> None:
    """Start several command transactions at the same time."""
    try:
        os.chdir(root)
        from projectmem.commands import log as log_command

        # Make the old read -> allocate -> append race deterministic. The
        # transaction must be acquired before this delayed read.
        read_events = log_command.read_events

        def delayed_read_events(*args, **kwargs):
            events = read_events(*args, **kwargs)
            time.sleep(0.02)
            return events

        log_command.read_events = delayed_read_events
        barrier.wait(timeout=30)
        for index in range(count):
            log_command.run(f"concurrent issue {os.getpid()}-{index}")
        results.put(None)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted in parent
        results.put(f"{type(exc).__name__}: {exc}")


def _attempt_worker(
    root: str, barrier: multiprocessing.synchronize.Barrier, count: int, results
) -> None:
    """Exercise the auto-created issue + marker + attempt transaction."""
    try:
        os.chdir(root)
        from projectmem.commands import attempt as attempt_command

        read_events = attempt_command.read_events

        def delayed_read_events(*args, **kwargs):
            events = read_events(*args, **kwargs)
            time.sleep(0.02)
            return events

        attempt_command.read_events = delayed_read_events
        barrier.wait(timeout=30)
        for index in range(count):
            attempt_command.run(
                f"concurrent auto issue {os.getpid()}-{index}",
                worked=False,
                failed=True,
                partial=False,
                auto_issue=True,
            )
        results.put(None)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted in parent
        results.put(f"{type(exc).__name__}: {exc}")


def _run_workers(tmp_path: Path, target, *, workers: int = 6, count: int = 2) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(workers)
    results = context.Queue()
    processes = [
        context.Process(target=target, args=(str(tmp_path), barrier, count, results))
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            pytest.fail("concurrency worker did not finish")
        assert process.exitcode == 0

    errors = []
    for _ in processes:
        try:
            result = results.get(timeout=5)
        except Empty:
            errors.append("worker returned no result")
            continue
        if result is not None:
            errors.append(result)
    assert not errors, "; ".join(errors)


def _events(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (root / ".projectmem" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_concurrent_logs_allocate_unique_issue_ids_and_regenerate_summary(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path, global_enabled=False)

    workers, count = 6, 2
    _run_workers(tmp_path, _log_worker, workers=workers, count=count)

    events = _events(tmp_path)
    issue_ids = [event["issue_id"] for event in events if event["type"] == "issue"]
    assert len(issue_ids) == workers * count
    assert len(issue_ids) == len(set(issue_ids))
    assert set(issue_ids) == {
        f"{number:04d}" for number in range(1, workers * count + 1)
    }

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    assert all(f"#{issue_id}" in summary for issue_id in issue_ids)
    assert all("concurrent issue" in summary for _ in issue_ids)


def test_concurrent_auto_issue_attempts_keep_marker_and_summary_consistent(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path, global_enabled=False)

    workers, count = 6, 2
    _run_workers(tmp_path, _attempt_worker, workers=workers, count=count)

    events = _events(tmp_path)
    issues = [event for event in events if event["type"] == "issue"]
    attempts = [event for event in events if event["type"] == "attempt"]
    # Once the first transaction opens an issue, the remaining calls should
    # attach to that active issue rather than racing to create duplicate
    # parent issue events.
    assert len(issues) == 1
    assert len(attempts) == workers * count
    issue_ids = [event["issue_id"] for event in issues]
    assert len(issue_ids) == len(set(issue_ids))
    assert all(event["issue_id"] == issue_ids[0] for event in attempts)

    marker_path = tmp_path / ".projectmem" / ".current_issue"
    marker = marker_path.read_text(encoding="utf-8").strip()
    assert marker in set(issue_ids)

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    assert f"#{marker} " in summary
    assert issues[0]["summary"] in summary
    # The summary intentionally keeps only the latest three lessons for an
    # issue; checking those generated sections proves the final write is
    # complete without depending on that retention limit changing.
    assert summary.count("Failed attempt:") == 3
