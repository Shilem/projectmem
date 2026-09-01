"""Read-only project memory health checks.

``pjm doctor`` is intentionally diagnostic: it never regenerates summaries,
rewrites events, or repairs derived files.  It reports whether the append-only
event log, available derived projections, and recent auto-capture diagnostics
can be read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from projectmem.commands.auto_capture import (
    MAX_DIAGNOSTICS,
    _safe_diagnostic_message,
    diagnostics_path,
    read_diagnostics,
)
from projectmem.models import Event
from projectmem.search_index import SearchIndexError, check_index
from projectmem.storage import EVENTS_FILE, MEM_DIR, read_events

SUMMARY_FILE = "summary.md"
# ``structure.json`` is the current code-structure projection.  The other
# names are accepted because older/local builds may expose a JSON projection
# under a different name; all are optional and read-only here.
PROJECTION_FILES = (
    "structure.json",
    "summary.index.json",
    "events.state.json",
    "summary.json",
    "summary.jsonl",
)


def _root_path(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _safe_error(exc: BaseException, root: Path) -> dict[str, str]:
    return {
        "exception_type": type(exc).__name__,
        "message": _safe_diagnostic_message(exc, root),
    }


def _check_events(root: Path) -> dict[str, Any]:
    path = root / MEM_DIR / EVENTS_FILE
    if not (root / MEM_DIR).is_dir():
        return {
            "status": "problem",
            "path": str(path),
            "message": ".projectmem directory is not present",
        }
    if not path.exists():
        return {
            "status": "skipped",
            "path": str(path),
            "message": "events.jsonl is not present",
        }
    try:
        events: list[Event] = read_events(root)
    except Exception as exc:  # noqa: BLE001  # doctor must report, not abort
        return {
            "status": "problem",
            "path": str(path),
            **_safe_error(exc, root),
        }
    return {"status": "ok", "path": str(path), "events": len(events)}


def _check_summary(root: Path) -> dict[str, Any]:
    path = root / MEM_DIR / SUMMARY_FILE
    if not path.exists():
        return {
            "status": "skipped",
            "path": str(path),
            "message": "summary.md is not present",
        }
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001  # doctor must report, not abort
        return {
            "status": "problem",
            "path": str(path),
            **_safe_error(exc, root),
        }
    if not content.strip():
        return {
            "status": "problem",
            "path": str(path),
            "message": "summary.md is empty",
        }
    return {"status": "ok", "path": str(path), "bytes": len(content.encode("utf-8"))}


def _read_projection(path: Path) -> int:
    """Parse a JSON/JSONL projection and return its record count."""
    content = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        count = 0
        for line_number, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            json.loads(line)
            count += 1
        if count == 0:
            raise ValueError(f"{path.name} is empty")
        return count
    value = json.loads(content)
    if not isinstance(value, (dict, list)):
        raise ValueError(  # noqa: TRY004  # malformed projection is a data error
            f"{path.name} must contain a JSON object or array"
        )
    return len(value) if isinstance(value, list) else 1


def _check_projections(root: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for filename in PROJECTION_FILES:
        path = root / MEM_DIR / filename
        if not path.exists():
            continue
        try:
            records = _read_projection(path)
        except Exception as exc:  # noqa: BLE001  # doctor must report, not abort
            found[filename] = {
                "status": "problem",
                "path": str(path),
                **_safe_error(exc, root),
            }
            continue
        found[filename] = {
            "status": "ok",
            "path": str(path),
            "records": records,
        }
    if not found:
        return {"status": "skipped", "files": {}}
    status = "problem" if any(item["status"] == "problem" for item in found.values()) else "ok"
    return {"status": status, "files": found}


def _normalize_diagnostic(record: dict[str, Any], root: Path) -> dict[str, Any]:
    """Keep doctor output bounded even if the local log was hand-edited."""
    operation = str(record.get("operation", "unknown")).strip()[:64] or "unknown"
    exception_type = str(
        record.get("exception_type", record.get("error_type", "Unknown"))
    ).strip()[:120] or "Unknown"
    timestamp = str(record.get("timestamp", "")).strip()[:64]
    raw_message = record.get("message", "operation failed")
    message = _safe_diagnostic_message(ValueError(str(raw_message)), root)
    return {
        "operation": operation,
        "exception_type": exception_type,
        "timestamp": timestamp,
        "message": message,
    }


def _check_diagnostics(root: Path) -> dict[str, Any]:
    path = diagnostics_path(root)
    if not path.exists():
        return {"status": "skipped", "path": str(path), "records": []}
    try:
        records = read_diagnostics(root, limit=MAX_DIAGNOSTICS)
    except Exception as exc:  # noqa: BLE001  # doctor must report, not abort
        return {
            "status": "problem",
            "path": str(path),
            "records": [],
            **_safe_error(exc, root),
        }
    normalized = [_normalize_diagnostic(record, root) for record in records]
    return {
        "status": "problem" if normalized else "ok",
        "path": str(path),
        "records": normalized,
        "count": len(normalized),
    }


def _check_search_index(root: Path) -> dict[str, Any]:
    """Report derived-index health without creating or rebuilding it."""
    try:
        index = check_index(root)
    except (SearchIndexError, OSError, ValueError) as exc:
        return {
            "status": "problem",
            **_safe_error(exc, root),
        }

    # The index is deliberately lazy.  A missing file is normal until the
    # first plain search, while a healthy but stale file will be rebuilt by
    # that search before it returns any data.
    if index["status"] == "missing":
        return {
            "status": "skipped",
            "path": index["path"],
            "message": "created automatically on the first plain search",
        }
    if index["healthy"]:
        return {
            "status": "ok",
            "path": index["path"],
            "current": index["current"],
            "events": index.get("event_count"),
            "message": index["reason"],
        }
    return {
        "status": "problem",
        "path": index["path"],
        "message": index["reason"],
        "error": index.get("error"),
    }


def build_report(root: Path | None = None) -> dict[str, Any]:
    """Build a read-only health report for ``root``."""
    root_path = _root_path(root)
    checks = {
        "events": _check_events(root_path),
        "summary": _check_summary(root_path),
        "projection": _check_projections(root_path),
        "search_index": _check_search_index(root_path),
        "auto_capture": _check_diagnostics(root_path),
    }
    problems = [check for check in checks.values() if check["status"] == "problem"]
    status = "problems" if problems else "healthy"
    # Keep the individual checks both nested (for consumers) and addressable
    # at the top level (for simple scripts and backwards-compatible callers).
    return {
        "status": status,
        "healthy": not problems,
        "checks": checks,
        **checks,
    }


def format_report(report: dict[str, Any]) -> str:
    """Render a compact human-readable doctor report."""
    lines = [f"projectmem doctor: {report['status']}"]
    checks = report["checks"]
    labels = {
        "events": "events",
        "summary": "summary",
        "projection": "projection",
        "search_index": "search index",
        "auto_capture": "auto-capture diagnostics",
    }
    for name in (
        "events",
        "summary",
        "projection",
        "search_index",
        "auto_capture",
    ):
        check = checks[name]
        line = f"  {labels[name]}: {check['status']}"
        if name == "events" and "events" in check:
            line += f" ({check['events']} events)"
        elif name == "auto_capture" and "count" in check:
            line += f" ({check['count']} recent failure(s))"
        elif name == "projection" and check.get("files"):
            line += f" ({', '.join(check['files'])})"
        elif name == "search_index" and check.get("events") is not None:
            freshness = "current" if check.get("current") else "stale"
            line += f" ({check['events']} events; {freshness})"
        if check.get("message"):
            line += f" — {check['message']}"
        lines.append(line)
        if name == "auto_capture":
            for record in check.get("records", []):
                lines.append(
                    "    - {timestamp} {operation}: {exception_type}: {message}".format(
                        **record
                    )
                )
    return "\n".join(lines)


def run(root: Path | None = None, fmt: str = "text") -> dict[str, Any]:
    """Run doctor checks and print ``text`` or ``json`` output.

    The return value is useful to embedded callers; printing remains aligned
    with the other command modules and lets the main CLI register this module
    without adding command-specific formatting logic.
    """
    if fmt not in {"text", "json"}:
        raise ValueError("Doctor format must be 'text' or 'json'.")
    report = build_report(root)
    if fmt == "json":
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        typer.echo(format_report(report))
    return report
