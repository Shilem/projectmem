"""Disposable SQLite FTS5 index for the append-only event log.

``events.jsonl`` remains the source of truth.  The database in this module is
only a projection: it can be removed and rebuilt at any time.  Keeping the
projection here, instead of adding state to :mod:`projectmem.storage`, also
keeps the event write path independent from search implementation details.
"""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from urllib.parse import quote

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    events_metadata,
    events_path,
    project_transaction,
    read_events,
    require_mem_dir,
)


INDEX_FILE = "search.sqlite3"
SCHEMA_VERSION = 3
MAX_SEARCH_LIMIT = 100
MAX_QUERY_LENGTH = 4096
MAX_SUMMARY_LENGTH = 2000
MAX_FILE_COUNT = 50
MAX_FILE_LENGTH = 512
MAX_LOCATION_LENGTH = 512

_SOURCE_METADATA_KEYS = ("device", "inode", "size", "mtime_ns")
_TOKEN_RE = re.compile(r"[^\W_]+(?:_[^\W_]+)*", re.UNICODE)


class SearchIndexError(RuntimeError):
    """Base error for an unavailable or unrepairable search projection."""


class SearchIndexUnavailableError(SearchIndexError):
    """Raised when the Python SQLite build does not provide FTS5."""


def _project_root(root: Path | str | None) -> Path:
    """Resolve the project root while preserving storage's discovery rules."""
    requested = None if root is None else Path(root)
    return require_mem_dir(requested).parent.resolve()


def search_index_path(root: Path | str | None = None) -> Path:
    """Return the disposable ``.projectmem/search.sqlite3`` path."""
    return require_mem_dir(_project_root(root)) / INDEX_FILE


def _fts5_error(exc: BaseException) -> SearchIndexUnavailableError | None:
    message = str(exc).lower()
    if "fts5" in message or "no such module" in message:
        return SearchIndexUnavailableError(
            "SQLite FTS5 is unavailable in this Python build; "
            "the ProjectMem search index cannot be used."
        )
    return None


def _verify_fts5(connection: sqlite3.Connection) -> None:
    """Probe the extension on this connection and report a clear failure."""
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.projectmem_fts5_probe USING fts5(content)"
        )
        connection.execute("DROP TABLE temp.projectmem_fts5_probe")
    except sqlite3.OperationalError as exc:
        unavailable = _fts5_error(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise


def _probe_fts5() -> None:
    """Check FTS5 without creating an index file."""
    try:
        connection = sqlite3.connect(":memory:")
    except sqlite3.OperationalError as exc:
        unavailable = _fts5_error(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise
    try:
        _verify_fts5(connection)
    finally:
        connection.close()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a database and verify FTS5 on the same connection."""
    if read_only:
        # URI mode prevents a health check from creating a missing database.
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        _verify_fts5(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _source_metadata(root: Path) -> dict[str, int]:
    try:
        metadata = events_metadata(root)
    except OSError as exc:
        raise ProjectMemError(
            f"Could not read the authoritative events log for {root}: {exc}"
        ) from exc
    return {key: int(metadata[key]) for key in _SOURCE_METADATA_KEYS}


def _status(
    *,
    path: Path,
    source_metadata: dict[str, int] | None,
    status: str,
    healthy: bool,
    current: bool,
    reason: str,
    event_count: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "status": status,
        "healthy": healthy,
        "current": current,
        "reason": reason,
    }
    if source_metadata is not None:
        result["source_metadata"] = source_metadata
    if event_count is not None:
        result["event_count"] = event_count
    if error:
        result["error"] = error[:240]
    return result


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def check_index(root: Path | str | None = None) -> dict[str, Any]:
    """Return whether the search projection is healthy and current.

    A stale but structurally readable database is reported as ``healthy`` and
    ``current=False``.  Missing or corrupt projections are unhealthy and are
    repaired by :func:`ensure_index_current`.  Source-log read errors are
    surfaced rather than turning an unreadable source into an empty index.
    """
    project_root = _project_root(root)
    path = search_index_path(project_root)
    try:
        source = _source_metadata(project_root)
    except ProjectMemError as exc:
        return _status(
            path=path,
            source_metadata=None,
            status="source_missing",
            healthy=False,
            current=False,
            reason="authoritative events.jsonl is missing",
            error=_safe_error(exc),
        )

    # Even a missing projection must produce an explicit FTS5 error instead of
    # pretending that a future rebuild can work.
    _probe_fts5()
    if not path.exists():
        return _status(
            path=path,
            source_metadata=source,
            status="missing",
            healthy=False,
            current=False,
            reason="search index does not exist",
        )

    try:
        with _connect(path, read_only=True) as connection:
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {"index_metadata", "indexed_events", "events_fts"}
            if not required_tables.issubset(table_names):
                return _status(
                    path=path,
                    source_metadata=source,
                    status="corrupt",
                    healthy=False,
                    current=False,
                    reason="index schema is incomplete",
                )

            rows = connection.execute(
                "SELECT key, value FROM index_metadata"
            ).fetchall()
            metadata_values = {str(key): value for key, value in rows}
            if metadata_values.get("schema_version") != str(SCHEMA_VERSION):
                return _status(
                    path=path,
                    source_metadata=source,
                    status="corrupt",
                    healthy=False,
                    current=False,
                    reason="unsupported index schema version",
                )
            try:
                indexed_source = json.loads(metadata_values["source_metadata"])
                indexed_count = int(metadata_values["event_count"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return _status(
                    path=path,
                    source_metadata=source,
                    status="corrupt",
                    healthy=False,
                    current=False,
                    reason="index metadata is invalid",
                    error=_safe_error(exc),
                )
            if not isinstance(indexed_source, dict) or any(
                indexed_source.get(key) != source.get(key)
                for key in _SOURCE_METADATA_KEYS
            ):
                return _status(
                    path=path,
                    source_metadata=source,
                    status="stale",
                    healthy=True,
                    current=False,
                    reason="authoritative events.jsonl changed",
                    event_count=indexed_count,
                )

            table_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM indexed_events"
                ).fetchone()[0]
            )
            fts_count = int(
                connection.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
            )
            if indexed_count != table_count or table_count != fts_count:
                return _status(
                    path=path,
                    source_metadata=source,
                    status="corrupt",
                    healthy=False,
                    current=False,
                    reason="indexed event rows are inconsistent",
                    event_count=table_count,
                )

            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check.lower() != "ok":
                return _status(
                    path=path,
                    source_metadata=source,
                    status="corrupt",
                    healthy=False,
                    current=False,
                    reason="SQLite integrity check failed",
                    event_count=table_count,
                    error=quick_check,
                )
    except SearchIndexUnavailableError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        return _status(
            path=path,
            source_metadata=source,
            status="corrupt",
            healthy=False,
            current=False,
            reason="SQLite search index cannot be read",
            error=_safe_error(exc),
        )

    return _status(
        path=path,
        source_metadata=source,
        status="current",
        healthy=True,
        current=True,
        reason="search index matches events.jsonl",
        event_count=indexed_count,
    )


def is_index_current(root: Path | str | None = None) -> bool:
    """Return ``True`` only for a healthy projection of the current log."""
    status = check_index(root)
    return bool(status["healthy"] and status["current"])


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        CREATE TABLE index_metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        );
        CREATE TABLE indexed_events (
            source_order INTEGER PRIMARY KEY NOT NULL,
            event_id TEXT NOT NULL,
            type TEXT NOT NULL,
            outcome TEXT,
            timestamp TEXT NOT NULL,
            issue_id TEXT,
            supersedes TEXT,
            location TEXT,
            summary TEXT NOT NULL,
            files_json TEXT NOT NULL,
            notes TEXT
        );
        CREATE VIRTUAL TABLE events_fts USING fts5(
            event_id,
            type,
            issue_id,
            location,
            summary,
            files,
            notes
        );
        """
    )


def _insert_events(
    connection: sqlite3.Connection,
    events: list[Event],
    source_metadata: dict[str, int],
) -> None:
    for source_order, event in enumerate(events, 1):
        files = [str(item) for item in (event.files or [])]
        connection.execute(
            """
            INSERT INTO indexed_events(
                source_order, event_id, type, outcome, timestamp, issue_id,
                supersedes, location, summary, files_json, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_order,
                event.id,
                event.type,
                event.outcome,
                event.timestamp,
                event.issue_id,
                event.supersedes,
                event.location,
                event.summary,
                json.dumps(files, ensure_ascii=False, separators=(",", ":")),
                event.notes,
            ),
        )
        connection.execute(
            """
            INSERT INTO events_fts(
                rowid, event_id, type, issue_id, location, summary, files, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_order,
                event.id,
                event.type,
                event.issue_id or "",
                event.location or "",
                event.summary,
                "\n".join(files),
                event.notes or "",
            ),
        )
    connection.executemany(
        "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
        (
            ("schema_version", str(SCHEMA_VERSION)),
            ("source_metadata", json.dumps(source_metadata, sort_keys=True)),
            ("event_count", str(len(events))),
        ),
    )


def rebuild_index(root: Path | str | None = None) -> dict[str, Any]:
    """Rebuild the disposable search database from the complete event log.

    The new database is written beside the current one and atomically swapped
    into place.  If parsing or indexing fails, the old projection remains
    untouched and the authoritative event log is never changed.
    """
    project_root = _project_root(root)
    _probe_fts5()
    target = search_index_path(project_root)

    # Writers already use this lock.  Taking it here gives a rebuild a stable
    # snapshot for normal ProjectMem writes without coupling storage to FTS5.
    with project_transaction(project_root):
        source = events_path(project_root)
        if not source.exists():
            raise ProjectMemError(
                f"Authoritative events log is missing: {source}; "
                "refusing to build an empty search index."
            )
        events = read_events(project_root)
        source_metadata = _source_metadata(project_root)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{INDEX_FILE}.", suffix=".tmp", dir=target.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            connection = _connect(temporary)
            try:
                _create_schema(connection)
                _insert_events(connection, events, source_metadata)
                connection.commit()
                quick_check = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                if quick_check.lower() != "ok":
                    raise SearchIndexError(
                        f"SQLite integrity check failed while rebuilding: {quick_check}"
                    )
            finally:
                connection.close()
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    result = check_index(project_root)
    result["rebuilt"] = True
    return result


def ensure_index_current(root: Path | str | None = None) -> dict[str, Any]:
    """Ensure a healthy, current index and return its status."""
    project_root = _project_root(root)
    status = check_index(project_root)
    if status["healthy"] and status["current"]:
        status["rebuilt"] = False
        return status
    if status["status"] == "source_missing":
        raise ProjectMemError(
            f"Cannot build the search index: {status.get('error', 'events.jsonl is missing')}"
        )
    return rebuild_index(project_root)


def _validate_search_args(query: str, limit: int) -> str:
    if not isinstance(query, str):
        raise TypeError("Search query must be a string.")
    value = query.strip()
    if not value:
        raise ValueError("Search query cannot be empty.")
    if len(value) > MAX_QUERY_LENGTH:
        raise ValueError(
            f"Search query is too long (maximum {MAX_QUERY_LENGTH} characters)."
        )
    if "\x00" in value:
        raise ValueError("Search query cannot contain NUL bytes.")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("Search limit must be an integer.")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(
            f"Search limit must be between 1 and {MAX_SEARCH_LIMIT}."
        )
    return value


def _fts_query(value: str) -> str:
    # Every token is quoted and joined by a fixed operator.  The user text is
    # never interpolated into SQL or passed to FTS5's operator grammar.
    tokens = _TOKEN_RE.findall(value)
    return " AND ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
    )


def _bounded_metadata(
    event_id: str,
    event_type: str,
    outcome: str | None,
    timestamp: str,
    issue_id: str | None,
    superseded: bool,
    location: str | None,
    summary: str,
    files_json: str,
    *,
    relevance: float | None,
    source: str,
) -> dict[str, Any]:
    try:
        files_value = json.loads(files_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        files_value = []
    if not isinstance(files_value, list):
        files_value = []
    files = [str(item)[:MAX_FILE_LENGTH] for item in files_value if item is not None]
    return {
        "id": str(event_id)[:200],
        "type": str(event_type)[:64],
        "outcome": None if outcome is None else str(outcome)[:64],
        "timestamp": str(timestamp)[:64],
        "issue_id": None if issue_id is None else str(issue_id)[:128],
        "superseded": bool(superseded),
        "location": None if location is None else str(location)[:MAX_LOCATION_LENGTH],
        "summary": str(summary)[:MAX_SUMMARY_LENGTH],
        "files": files[:MAX_FILE_COUNT],
        "relevance": relevance,
        "source": source,
    }


def _fts_search(
    project_root: Path, fts_query: str, limit: int
) -> list[dict[str, Any]]:
    if not fts_query:
        return []
    path = search_index_path(project_root)
    with _connect(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT e.event_id, e.type, e.outcome, e.timestamp, e.issue_id,
                   EXISTS(
                       SELECT 1 FROM indexed_events AS successor
                       WHERE successor.supersedes = e.event_id
                   ) AS superseded,
                   e.location, e.summary, e.files_json, bm25(events_fts) AS relevance,
                   e.source_order
            FROM events_fts
            JOIN indexed_events AS e ON e.source_order = events_fts.rowid
            WHERE events_fts MATCH ?
            ORDER BY relevance ASC, e.source_order DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    return [
        _bounded_metadata(
            str(row[0]),
            str(row[1]),
            row[2],
            row[3],
            row[4],
            bool(row[5]),
            row[6],
            str(row[7]),
            str(row[8]),
            relevance=float(row[9]),
            source="fts5",
        )
        for row in rows
    ]


def _event_contains(event: Event, needle: str) -> bool:
    values = [
        event.id,
        event.type,
        event.issue_id or "",
        event.location or "",
        event.summary,
        event.notes or "",
        event.timestamp,
        *event.files,
    ]
    return any(needle in str(value).casefold() for value in values)


def _raw_literal_search(
    project_root: Path, needle: str, limit: int
) -> list[dict[str, Any]]:
    """Search the source log literally while bounding retained results."""
    path = events_path(project_root)
    matches: deque[Event] = deque(maxlen=limit)
    retired_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event_data = json.loads(line)
                event = Event.from_dict(event_data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ProjectMemError(
                    f"Invalid event at {path}:{line_number}: {exc}"
                ) from exc
            # Structured fields handle non-ASCII text that json.dumps may have
            # escaped; the raw line check preserves literal JSONL semantics for
            # unusual punctuation and fields added by older producers.
            if _event_contains(event, needle) or needle in line.casefold():
                matches.append(event)
            if event.supersedes:
                retired_ids.add(event.supersedes)
    return [
        _bounded_metadata(
            event.id,
            event.type,
            event.outcome,
            event.timestamp,
            event.issue_id,
            event.id in retired_ids,
            event.location,
            event.summary,
            json.dumps(list(event.files or []), ensure_ascii=False),
            relevance=None,
            source="raw",
        )
        for event in reversed(matches)
    ]


def search_events(
    query: str, limit: int = 10, root: Path | str | None = None
) -> list[dict[str, Any]]:
    """Search indexed event metadata, with a bounded literal source fallback.

    ``notes`` participates in matching but is deliberately never returned.
    Results are metadata dictionaries ordered by FTS relevance and recency;
    literal fallback results are newest first.
    """
    value = _validate_search_args(query, limit)
    project_root = _project_root(root)
    ensure_index_current(project_root)
    fts_query = _fts_query(value)
    try:
        matches = _fts_search(project_root, fts_query, limit)
    except SearchIndexUnavailableError:
        raise
    except (OSError, sqlite3.DatabaseError):
        # A database can be damaged after the health check (for example, an
        # interrupted external copy).  Rebuild once, then let a second error
        # remain visible to the caller.
        rebuild_index(project_root)
        matches = _fts_search(project_root, fts_query, limit)
    if matches:
        return matches[:limit]
    return _raw_literal_search(project_root, value.casefold(), limit)


# Descriptive aliases make the small public API convenient for callers that
# name the artifact explicitly while retaining the short names used by the
# CLI/MCP integration.
rebuild_search_index = rebuild_index
check_search_index = check_index
ensure_search_index = ensure_index_current
index_health = check_index
search_index_status = check_index
is_search_index_current = is_index_current


__all__ = [
    "INDEX_FILE",
    "MAX_SEARCH_LIMIT",
    "SearchIndexError",
    "SearchIndexUnavailableError",
    "check_index",
    "check_search_index",
    "ensure_index_current",
    "ensure_search_index",
    "index_health",
    "is_index_current",
    "is_search_index_current",
    "rebuild_index",
    "rebuild_search_index",
    "search_events",
    "search_index_path",
    "search_index_status",
]
