"""Internal auto-capture command called by git hooks.

This is NOT a user-facing command.  Git hooks invoke it as:
    pjm _auto-capture commit
    pjm _auto-capture merge

It reads the latest git state, classifies the event, and appends an
auto-captured event to events.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event, utc_now_iso
from projectmem.storage import (
    MEM_DIR,
    append_event,
    get_git_commit,
    project_transaction,
    read_events,
)
from projectmem.summary import regenerate_summary

# ── Classification Patterns ──────────────────────────────────────────
# Order matters — first match wins. Patterns are tested case-insensitively
# against the full commit message.

COMMIT_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "revert",
        "pattern": re.compile(r"^revert\b|^Revert\b", re.IGNORECASE),
        "event_type": "attempt",
        "outcome": "failed",
        "prefix": "Reverted",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "fix",
        "pattern": re.compile(
            r"^fix[\s(:]|^hotfix[\s(:]|^bugfix[\s(:]|^patch[\s(:]", re.IGNORECASE
        ),
        "event_type": "fix",
        "outcome": None,
        "prefix": "Fix",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "breaking",
        "pattern": re.compile(r"BREAKING[\s_-]?CHANGE|^break[\s(:]", re.IGNORECASE),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Breaking change",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "feature",
        "pattern": re.compile(r"^feat[\s(:]|^feature[\s(:]|^add[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "New feature",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "refactor",
        "pattern": re.compile(
            r"^refactor[\s(:]|^cleanup[\s(:]|^reorganize|^restructure",
            re.IGNORECASE,
        ),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Refactor",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "docs",
        "pattern": re.compile(r"^docs?[\s(:]|^readme|^changelog", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Documentation",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
    {
        "name": "test",
        "pattern": re.compile(r"^test[\s(:]|^tests?[\s(:]|^spec[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Tests",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
]

# Minimum confidence to actually log (skip "low" by default to reduce noise)
MIN_CONFIDENCE = "medium"
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# This file is runtime diagnostic state, not another project-memory event
# stream.  Keeping it separate means a failed capture never has to recurse
# through ``append_event`` while reporting the failure.
AUTO_CAPTURE_DIAGNOSTICS_FILE = "auto_capture_diagnostics.jsonl"
DIAGNOSTICS_FILE = AUTO_CAPTURE_DIAGNOSTICS_FILE
MAX_DIAGNOSTIC_MESSAGE = 240
MAX_DIAGNOSTICS = 20


def diagnostics_path(root: Path | None = None) -> Path:
    """Return the local auto-capture diagnostic log path."""
    return (root or Path.cwd()) / MEM_DIR / AUTO_CAPTURE_DIAGNOSTICS_FILE


def auto_capture_diagnostics_path(root: Path | None = None) -> Path:
    """Backwards-compatible descriptive alias for :func:`diagnostics_path`."""
    return diagnostics_path(root)


def _safe_diagnostic_message(exc: BaseException, root: Path | None = None) -> str:
    """Reduce an exception to a short, single-line, non-sensitive message.

    Exception text is untrusted input: parser and subprocess errors can
    contain source fragments, credentials, or a full local path.  Diagnostics
    need enough detail to identify a failure class, but must not become a
    second transcript or traceback store.
    """
    try:
        message = " ".join(str(exc).split())
    except Exception:  # noqa: BLE001  # pragma: no cover - pathological exception objects
        message = "operation failed"
    if root is not None:
        try:
            message = message.replace(str(root.resolve()), "<project>")
        except OSError:
            pass
    # Apply the project's known high-confidence secret patterns regardless of
    # the event-redaction opt-out: diagnostics are always safety-critical.
    try:
        from projectmem.redaction import redact

        message, _ = redact(message)
    except Exception:  # noqa: BLE001, S110  # redaction is optional at this edge
        pass
    # Catch common ad-hoc ``token=...`` / ``password: ...`` forms too.  This
    # is deliberately narrow so ordinary error messages remain useful.
    message = re.sub(
        r"(?i)\b(api[_ -]?key|token|secret|password|authorization|bearer)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        message,
    )
    message = message.replace("\x00", " ").strip()
    if not message:
        message = "operation failed"
    return message[:MAX_DIAGNOSTIC_MESSAGE]


def _record_diagnostic(
    root: Path, operation: str, exc: BaseException
) -> bool:
    """Best-effort append of one structured auto-capture failure record.

    The caller is running from a Git hook, so diagnostics must never turn a
    non-blocking capture path into a blocking Git failure.  A single
    ``O_APPEND`` write keeps concurrent hook processes from interleaving their
    short JSONL records.
    """
    record = {
        "operation": operation,
        "exception_type": type(exc).__name__,
        "timestamp": utc_now_iso(),
        "message": _safe_diagnostic_message(exc, root),
    }
    path = diagnostics_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        fd = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("short diagnostic write")
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001  # reporting must never block a Git hook
        # Reporting is strictly subordinate to the hook's best-effort
        # contract.  There is no safe place to surface a second failure here.
        return False


def read_diagnostics(
    root: Path | None = None, *, limit: int = MAX_DIAGNOSTICS
) -> list[dict[str, Any]]:
    """Read recent structured diagnostics without changing project state.

    Invalid JSON or non-object records raise ``ValueError`` so ``doctor`` can
    report a damaged diagnostic log instead of silently hiding it.
    """
    path = diagnostics_path(root)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid diagnostic at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(  # noqa: TRY004  # malformed records are data errors
                f"Invalid diagnostic at {path}:{line_number}"
            )
        records.append(value)
    if limit <= 0:
        return []
    return records[-limit:]


def run(trigger: str = "commit", root: Path | None = None) -> None:
    """Classify the latest git action and log it as an auto-captured event."""
    root_path = root or Path.cwd()

    # Guard: only run if .projectmem exists
    if not (root_path / MEM_DIR).exists():
        return

    # Check auto-capture config
    config_path = root_path / MEM_DIR / "config.toml"
    if config_path.exists():
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001  # hook path is best effort
            _record_diagnostic(root_path, "config", exc)
            return
        if "auto_capture = false" in config_text:
            return

    if trigger == "commit":
        _capture_commit(root_path)
    elif trigger == "merge":
        _capture_merge(root_path)


def _capture_commit(root: Path) -> None:
    """Classify and capture a git commit."""
    msg = _git_last_message(root)
    if not msg:
        return

    files = _git_last_changed_files(root)
    commit_hash = get_git_commit(root)

    # Classify
    matched = _classify_message(msg)
    if not matched:
        return

    # Check confidence threshold
    if CONFIDENCE_RANK.get(matched["confidence"], 0) < CONFIDENCE_RANK.get(
        MIN_CONFIDENCE, 2
    ):
        return

    # Build summary
    first_line = msg.strip().split("\n")[0][:120]
    summary = f"{matched['prefix']}: {first_line}"

    event = Event(
        type=matched["event_type"],
        summary=summary,
        outcome=matched["outcome"],
        files=files[:10],  # Cap at 10 files
        git_commit=commit_hash,
        location=files[0] if files else None,
        auto_captured=True,
        capture_source=matched["capture_source"],
        capture_confidence=matched["confidence"],
        git_message=first_line,
        command="auto-capture",
    )

    try:
        # The dedupe check and append must share one transaction. Otherwise
        # two post-commit hook processes can both observe a missing commit and
        # record it twice.
        with project_transaction(root) as project_root:
            try:
                existing = read_events(project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "read", exc)
                return
            existing_commits = {e.git_commit for e in existing if e.git_commit}
            if commit_hash and commit_hash in existing_commits:
                return
            try:
                append_event(event, project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "append", exc)
                return
            try:
                regenerate_summary(project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "rebuild", exc)
                return
        # Color output for terminal feedback
        colors = {
            "attempt": "\033[0;33m",  # yellow for reverts
            "fix": "\033[0;32m",      # green for fixes
            "decision": "\033[0;31m", # red for breaking/decisions
            "note": "\033[0;36m",     # cyan for features/notes
        }
        color = colors.get(event.type, "\033[0;37m")
        typer.echo(
            f"{color}[projectmem] Auto-captured: {summary}\033[0m"
        )
    except Exception as exc:  # noqa: BLE001  # hook path is best effort
        _record_diagnostic(root, "capture", exc)
        # Never block the developer's workflow on an auto-capture failure.


def _capture_merge(root: Path) -> None:
    """Capture a branch merge event."""
    msg = _git_last_message(root)
    if not msg:
        return

    commit_hash = get_git_commit(root)
    first_line = msg.strip().split("\n")[0][:120]

    event = Event(
        type="note",
        summary=f"Merge: {first_line}",
        git_commit=commit_hash,
        location=None,
        auto_captured=True,
        capture_source="git_post_merge",
        capture_confidence="high",
        git_message=first_line,
        command="auto-capture",
    )

    try:
        with project_transaction(root) as project_root:
            try:
                existing = read_events(project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "read", exc)
                return
            existing_commits = {e.git_commit for e in existing if e.git_commit}
            if commit_hash and commit_hash in existing_commits:
                return
            try:
                append_event(event, project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "append", exc)
                return
            try:
                regenerate_summary(project_root)
            except Exception as exc:  # noqa: BLE001  # hook path is best effort
                _record_diagnostic(project_root, "rebuild", exc)
                return
        typer.echo(
            f"\033[0;35m[projectmem] Auto-captured merge: {first_line}\033[0m"
        )
    except Exception as exc:  # noqa: BLE001  # hook path is best effort
        _record_diagnostic(root, "capture", exc)


def _classify_message(message: str) -> dict[str, Any] | None:
    """Match a commit message against classification patterns."""
    for pattern in COMMIT_PATTERNS:
        if pattern["pattern"].search(message):
            return pattern
    return None


def _git_last_message(root: Path) -> str | None:
    """Get the most recent commit message."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _git_last_changed_files(root: Path) -> list[str]:
    """Get files changed in the most recent commit."""
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
