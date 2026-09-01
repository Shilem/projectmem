from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    events_metadata,
    events_state_matches,
    issues_dir,
    project_map_path,
    project_transaction,
    read_events,
    read_events_from_offset,
    read_events_state,
    summary_index_path,
    summary_path,
    write_events_state,
)

# Phrases that mean "this is still placeholder content, treat as not-yet-set"
# (L-037). Used by both `extract_project_purpose` and
# `extract_project_purpose_from_map` so the regenerator doesn't keep echoing
# the init placeholder back into summary.md forever.
_PLACEHOLDER_PHRASES = (
    "Not described yet.",
    "Short description of what the project does.",
    "Replace this placeholder",
    "Status: not created yet",
    "This file should be created by the first AI assistant",
)


_UNSET = object()
_SUMMARY_INDEX_VERSION = 1

# ``summary.index.json`` is a session-start projection, not the audit log.
# Keep the two decision-related collections bounded so a long-lived project
# cannot make every regeneration and session read grow without limit.  The
# caps are deliberately constants rather than project settings: changing a
# local config must not change the shape or safety of the derived projection.
MAX_DECISIONS = 20
MAX_SUPERSEDED = MAX_DECISIONS
MAX_SUMMARY_ISSUES = 12
MAX_RENDERED_EVENT_CHARS = 200
MAX_RENDERED_LOCATION_CHARS = 160
MAX_RENDERED_PURPOSE_CHARS = 800


def _new_projection(metadata: dict[str, int]) -> dict[str, object]:
    """Create the compact, JSON-serializable summary projection."""
    return {
        "version": _SUMMARY_INDEX_VERSION,
        "events": {
            **metadata,
            "offset": metadata["size"],
            "count": 0,
            "last_id": None,
        },
        # Issue markdown remains the durable per-issue projection.  The index
        # keeps only the bounded summary fields needed to render summary.md;
        # retaining every historical Event here would make every append
        # serialize the whole issue history again.
        "issues": {},
        "decisions": [],
        "notes": [],
        "superseded": [],
        "files": [],
        "event_chain": "",
        # These counters are compact metadata used only for an honest hint in
        # summary.md when older decisions are no longer in the projection.
        # The event log remains the source of truth for the complete count.
        "decision_count": 0,
        "superseded_count": 0,
    }


def _summary_record(event: Event) -> dict[str, object]:
    """Retain only fields used by the compact summary sections."""
    record: dict[str, object] = {
        "id": event.id,
        "type": event.type,
        "summary": event.summary,
    }
    if event.location:
        record["location"] = event.location
    return record


def _issue_projection(event: Event) -> dict[str, object]:
    record: dict[str, object] = {
        "summary": "",
        "location": None,
        "lessons": [],
        "fix": None,
        "event_count": 0,
        "file": None,
    }
    if event.type == "issue":
        record["summary"] = event.summary
        record["location"] = event.location
    return record


def _event_chain_value(previous: object, event: Event) -> str:
    payload = json.dumps(
        event.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    previous_bytes = str(previous or "").encode("ascii")
    return hashlib.sha256(previous_bytes + payload).hexdigest()


def _bound_decision_state(projection: dict[str, object]) -> None:
    """Keep decision/supersede projection state bounded and live-only.

    Supersede pointers are retained only as recent projection metadata.  A
    decision that is already retired is removed from the retained decision
    records as well, so it cannot become visible again if its old pointer is
    later evicted from the bounded ``superseded`` list.  The append-only event
    log still contains both records for audit/search.
    """
    decisions = projection.get("decisions")
    superseded = projection.get("superseded")
    if not isinstance(decisions, list) or not isinstance(superseded, list):
        return

    del decisions[:-MAX_DECISIONS]
    del superseded[:-MAX_SUPERSEDED]

    retired = {
        value
        for value in superseded
        if isinstance(value, str) and value
    }
    if retired:
        decisions[:] = [
            record
            for record in decisions
            if not isinstance(record, dict) or record.get("id") not in retired
        ]


def _increment_projection_counter(
    projection: dict[str, object], key: str
) -> None:
    """Increment a new-schema counter without inventing legacy totals."""
    value = projection.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        projection[key] = value + 1


def _append_file_mentions(projection: dict[str, object], record: dict[str, object]) -> None:
    files = projection["files"]
    if not isinstance(files, list):  # pragma: no cover - guarded by validation
        return
    seen = set(files)
    mentions = [str(path) for path in (record.get("files") or [])]
    mentions.extend(infer_file_mentions(str(record.get("summary") or "")))
    for mention in mentions:
        if mention in seen:
            continue
        seen.add(mention)
        files.append(mention)
        if len(files) >= 20:
            break


def _apply_records(
    projection: dict[str, object], events: list[Event]
) -> set[str]:
    """Apply only newly parsed events and return affected issue IDs."""
    issues = projection["issues"]
    decisions = projection["decisions"]
    notes = projection["notes"]
    superseded = projection["superseded"]
    event_chain = projection.get("event_chain", "")
    metadata = projection["events"]
    if not all(
        isinstance(value, list) for value in (decisions, notes, superseded)
    ) or not isinstance(issues, dict) or not isinstance(metadata, dict):
        raise ValueError("Invalid summary projection containers")
    if not isinstance(event_chain, str):
        raise TypeError("Invalid summary projection event chain")

    affected: set[str] = set()
    for event in events:
        record = event.to_dict()
        event_id = event.id
        event_chain = _event_chain_value(event_chain, event)
        metadata["count"] = int(metadata.get("count", 0)) + 1
        metadata["last_id"] = event_id

        issue_id = event.issue_id
        if isinstance(issue_id, str) and issue_id:
            issue = issues.setdefault(issue_id, _issue_projection(event))
            if not isinstance(issue, dict):
                raise ValueError("Invalid issue summary projection")
            issue["event_count"] = int(issue.get("event_count", 0)) + 1
            if event.type == "issue":
                issue["summary"] = event.summary
                issue["location"] = event.location
            elif event.type == "fix":
                issue["fix"] = _summary_record(event)
            elif event.type == "attempt" and event.outcome in ("failed", "partial"):
                lessons = issue.setdefault("lessons", [])
                if not isinstance(lessons, list):
                    raise ValueError("Invalid issue lessons projection")
                lessons.append(_summary_record(event) | {"outcome": event.outcome})
                del lessons[:-3]
            affected.add(issue_id)

        if event.type == "decision":
            decisions.append(_summary_record(event))
            _increment_projection_counter(projection, "decision_count")
        if event.type == "note":
            notes.append(_summary_record(event))
            del notes[:-10]
        if event.supersedes:
            superseded.append(event.supersedes)
            _increment_projection_counter(projection, "superseded_count")
        _append_file_mentions(projection, record)
    _bound_decision_state(projection)
    projection["event_chain"] = event_chain
    return affected


def _projection_from_events(
    events: list[Event], metadata: dict[str, int]
) -> dict[str, object]:
    projection = _new_projection(metadata)
    _apply_records(projection, events)
    events_state = projection["events"]
    if isinstance(events_state, dict):
        events_state["offset"] = metadata["size"]
    return projection


def _valid_event_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and isinstance(record.get("type"), str)
        and isinstance(record.get("summary"), str)
    )


def _projection_is_valid(
    projection: object,
    metadata: dict[str, int],
    state: dict[str, object] | None,
    root: Path,
) -> bool:
    """Validate enough structure to safely choose incremental parsing."""
    if not isinstance(projection, dict) or projection.get("version") != _SUMMARY_INDEX_VERSION:
        return False
    if not isinstance(state, dict) or state.get("rebuild_required", False):
        return False
    if not events_state_matches(state, metadata):
        return False

    event_meta = projection.get("events")
    if not isinstance(event_meta, dict):
        return False
    integer_fields = ("device", "inode", "size", "mtime_ns", "offset", "count")
    if not all(
        isinstance(event_meta.get(field), int)
        and not isinstance(event_meta.get(field), bool)
        and event_meta.get(field) >= 0
        for field in integer_fields
    ):
        return False
    if event_meta["device"] != metadata["device"] or event_meta["inode"] != metadata["inode"]:
        return False
    if event_meta["offset"] != event_meta["size"] or event_meta["offset"] > metadata["size"]:
        return False
    if event_meta["count"] < 0:
        return False
    if not isinstance(event_meta["last_id"], (str, type(None))):
        return False
    event_chain = projection.get("event_chain")
    if not isinstance(event_chain, str) or len(event_chain) not in (0, 64):
        return False

    issues = projection.get("issues")
    if not isinstance(issues, dict):
        return False
    for issue_id, issue in issues.items():
        if not isinstance(issue_id, str) or not isinstance(issue, dict):
            return False
        if not isinstance(issue.get("summary"), str):
            return False
        if not isinstance(issue.get("location"), (str, type(None))):
            return False
        if not isinstance(issue.get("event_count"), int) or issue["event_count"] < 1:
            return False
        lessons = issue.get("lessons")
        if not isinstance(lessons, list) or len(lessons) > 3:
            return False
        if not all(_valid_event_record(record) for record in lessons):
            return False
        fix = issue.get("fix")
        if fix is not None and not _valid_event_record(fix):
            return False
        file_metadata = issue.get("file")
        if not isinstance(file_metadata, dict):
            return False
        if not all(
            isinstance(file_metadata.get(field), int)
            and not isinstance(file_metadata.get(field), bool)
            and file_metadata.get(field) >= 0
            for field in ("device", "inode", "size", "mtime_ns")
        ):
            return False
        try:
            issue_path = issues_dir(root) / f"{issue_id}-{slugify(issue['summary'])}.md"
            actual_file = issue_path.stat()
        except (OSError, KeyError):
            return False
        if any(
            file_metadata[field] != getattr(actual_file, attr)
            for field, attr in (
                ("device", "st_dev"),
                ("inode", "st_ino"),
                ("size", "st_size"),
                ("mtime_ns", "st_mtime_ns"),
            )
        ):
            return False
    for key in ("decisions", "notes"):
        records = projection.get(key)
        if not isinstance(records, list) or not all(
            _valid_event_record(record) for record in records
        ):
            return False
    for key in ("superseded", "files"):
        values = projection.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return False
    for key in ("decision_count", "superseded_count"):
        if key in projection and (
            not isinstance(projection[key], int)
            or isinstance(projection[key], bool)
            or projection[key] < 0
        ):
            return False
    return True


def _load_projection(
    root: Path,
) -> tuple[dict[str, object], set[str], bool, list[Event]]:
    """Load a projection, parsing only the event suffix when it is usable.

    The writer-owned events state receipt is important here: inode/size alone
    cannot distinguish a same-file truncate-and-rewrite from a normal append.
    A stale/missing receipt therefore forces a safe full rebuild.
    """
    metadata = events_metadata(root)
    index_path = summary_index_path(root)
    state = read_events_state(root)
    try:
        projection = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        projection = None

    if _projection_is_valid(projection, metadata, state, root):
        assert isinstance(projection, dict)
        # Normalize indexes produced before the decision cap was introduced
        # without reparsing the historical JSONL prefix.  New appends are
        # bounded by _apply_records; this handles the no-new-events case too.
        _bound_decision_state(projection)
        event_meta = projection["events"]
        assert isinstance(event_meta, dict)
        old_offset = event_meta["offset"]
        if old_offset < metadata["size"]:
            try:
                appended, end_offset = read_events_from_offset(root, old_offset)
                affected = _apply_records(projection, appended)
            except (ProjectMemError, ValueError, TypeError):
                # A damaged suffix is not silently dropped.  Re-read the
                # authoritative log so malformed data still raises normally.
                events = read_events(root)
                rebuilt = _projection_from_events(events, metadata)
                return rebuilt, set(rebuilt["issues"]), True, events
            event_meta.update(metadata)
            event_meta["offset"] = end_offset
            return projection, affected, False, appended
        return projection, set(), False, []

    events = read_events(root)
    rebuilt = _projection_from_events(events, metadata)
    return rebuilt, set(rebuilt["issues"]), True, events


def _event_record_location(record: dict[str, object]) -> str:
    location = record.get("location")
    if not isinstance(location, str) or not location:
        return ""
    return f" [{_truncate_rendered_text(location, MAX_RENDERED_LOCATION_CHARS)}]"


def _truncate_rendered_text(value: object, limit: int) -> str:
    """Keep the session-start projection bounded without changing raw history."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _render_summary_projection(
    projection: dict[str, object], root: Path, project_purpose: str | None = None
) -> str:
    """Render the public summary format from the derived projection."""
    project_name = root.name
    now = datetime.now(timezone.utc).date().isoformat()
    raw_issues = projection.get("issues")
    issues = raw_issues if isinstance(raw_issues, dict) else {}
    retired_values = projection.get("superseded")
    retired = set(retired_values) if isinstance(retired_values, list) else set()
    decisions = [
        event
        for event in projection.get("decisions", [])
        if isinstance(event, dict) and event.get("id") not in retired
    ]
    notes = [event for event in projection.get("notes", []) if isinstance(event, dict)]

    lines = [
        f"# projectmem - {project_name}",
        "",
        f"_Last updated: {now}_",
        "",
        "## Project purpose",
        _truncate_rendered_text(
            project_purpose
            or (
                "Replace this placeholder with a concise description of what this "
                "project does, who it serves, and the main technologies or runtime "
                "assumptions."
            ),
            MAX_RENDERED_PURPOSE_CHARS,
        ),
        "",
        "## Recent issues",
    ]

    if not issues:
        lines.append("- No issues logged yet.")
    else:
        shown_issues = 0
        for issue_id, issue in sorted(issues.items(), reverse=True):
            if shown_issues >= MAX_SUMMARY_ISSUES:
                break
            if not isinstance(issue, dict):
                continue
            shown_issues += 1
            fix = issue.get("fix")
            status = "fixed" if fix else "open"
            marker = "DONE" if fix else "OPEN"
            issue_summary = _truncate_rendered_text(
                issue.get("summary"), MAX_RENDERED_EVENT_CHARS
            )
            outcome = ""
            if fix:
                outcome = (
                    " -> "
                    f"{_truncate_rendered_text(fix.get('summary'), MAX_RENDERED_EVENT_CHARS)}"
                    f"{_event_record_location(fix)}"
                )
            lines.append(
                f"- [{marker}] #{issue_id} {issue_summary}"
                f"{_event_record_location(issue)}{outcome} ({status})"
            )
            lessons = issue.get("lessons", [])
            labels = {"failed": "Failed attempt", "partial": "Partial attempt"}
            for lesson_event in lessons[-3:]:
                outcome_name = lesson_event.get("outcome")
                tag = labels.get(outcome_name, "Attempt")
                lines.append(
                    "  - "
                    f"{tag}: {_truncate_rendered_text(lesson_event.get('summary'), MAX_RENDERED_EVENT_CHARS)}"
                    f"{_event_record_location(lesson_event)}"
                )
        omitted_issues = max(0, len(issues) - shown_issues)
        if omitted_issues:
            lines.append(
                f"- {omitted_issues} older issues remain searchable in events.jsonl."
            )

    lines.extend(["", "## Decisions"])
    if decisions:
        for event in decisions:
            lines.append(
                "- "
                f"{_truncate_rendered_text(event.get('summary'), MAX_RENDERED_EVENT_CHARS)}"
                f"{_event_record_location(event)}"
            )
    else:
        lines.append("- No decisions logged yet.")

    # Keep the public projection transparent when the fixed decision window
    # omits older records.  The count is metadata only; complete history stays
    # in events.jsonl and remains discoverable with `pjm search`.
    decision_count = projection.get("decision_count")
    if (
        isinstance(decision_count, int)
        and not isinstance(decision_count, bool)
        and decision_count > len(decisions)
    ):
        omitted = decision_count - len(decisions)
        noun = "decision" if omitted == 1 else "decisions"
        lines.append(
            f"- {omitted} older or superseded {noun} remain in events.jsonl."
        )

    lines.extend(["", "## Notes"])
    if notes:
        for event in notes[-10:]:
            lines.append(
                "- "
                f"{_truncate_rendered_text(event.get('summary'), MAX_RENDERED_EVENT_CHARS)}"
                f"{_event_record_location(event)}"
            )
    else:
        lines.append("- No notes logged yet.")

    lines.extend(["", "## Key files"])
    files = projection.get("files")
    if isinstance(files, list) and files:
        for file_path in files[:20]:
            lines.append(f"- `{file_path}`")
    else:
        lines.append("- No key files logged yet.")

    lines.extend(["", "## Open questions"])
    lines.append("- None logged yet.")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` with complete ``content`` in one filesystem operation."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None:
                os.chmod(handle.name, stat.S_IMODE(mode))
            handle.write(content)
            handle.flush()
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_if_changed(
    path: Path, content: str, *, existing: str | None | object = _UNSET
) -> bool:
    """Write a derived file only when its rendered content has changed."""
    if existing is _UNSET:
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = None
    if existing == content:
        return False
    _atomic_write(path, content)
    return True


def _looks_like_placeholder(text: str) -> bool:
    """True if `text` is empty or contains a known placeholder phrase."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return any(phrase in stripped for phrase in _PLACEHOLDER_PHRASES)


def regenerate_summary(root: Path | None = None) -> Path:
    # A standalone `pjm regenerate` must not read an older event snapshot and
    # replace the summary after another process records a newer event. Command
    # paths may already own this re-entrant transaction.
    with project_transaction(root) as project_root:
        (
            projection,
            affected_issue_ids,
            full_rebuild,
            parsed_events,
        ) = _load_projection(project_root)
        path = summary_path(project_root)
        try:
            existing_summary = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing_summary = None

        # L-037: Project purpose is structural, not event-derived. Pull it from
        # PROJECT_MAP.md (the user-/AI-authored project description) so any
        # update to PROJECT_MAP.md flows through to summary.md on the next
        # regen. Falls back to whatever summary.md had before (legacy repos),
        # then to the default placeholder.
        try:
            map_purpose = extract_project_purpose_from_map(
                project_map_path(project_root)
            )
        except Exception:
            map_purpose = None
        project_purpose = map_purpose or extract_project_purpose(
            existing_summary or ""
        )

        content = _render_summary_projection(
            projection, project_root, project_purpose=project_purpose
        )
        _write_if_changed(path, content, existing=existing_summary)
        _write_projection_issue_files(
            projection,
            project_root,
            affected_issue_ids=affected_issue_ids,
            remove_stale=full_rebuild,
            event_records=parsed_events,
        )
        # Update the projection only after all derived public files have been
        # rendered.  A crash before this point leaves the previous index and
        # causes a safe replay of the event suffix on the next run.
        index_content = json.dumps(
            projection, sort_keys=True, separators=(",", ":")
        ) + "\n"
        _write_if_changed(summary_index_path(project_root), index_content)
        try:
            current_state = read_events_state(project_root)
            current_metadata = events_metadata(project_root)
            if (
                not events_state_matches(current_state, current_metadata)
                or bool(current_state and current_state.get("rebuild_required"))
            ):
                write_events_state(project_root, rebuild_required=False)
        except OSError:
            # A missing receipt is conservative: the next run rebuilds from
            # events.jsonl instead of trusting a potentially stale projection.
            pass
        return path


def extract_project_purpose_from_map(map_path: Path) -> str | None:
    """Read the `## Project purpose` section from PROJECT_MAP.md.

    Returns the body as a string if it's been populated with real content,
    or None if PROJECT_MAP.md is missing, the section is missing, or the
    body still looks like one of the known placeholder phrases.
    """
    if not map_path.exists():
        return None
    content = map_path.read_text(encoding="utf-8")
    match = re.search(
        r"^## Project purpose\s*\n(?P<body>.*?)(?=\n## |\Z)",
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return None
    body = match.group("body").strip()
    if _looks_like_placeholder(body):
        return None
    return body


def build_summary(
    events: list[Event], root: Path, project_purpose: str | None = None
) -> str:
    metadata = {"device": 0, "inode": 0, "size": 0, "mtime_ns": 0}
    projection = _projection_from_events(events, metadata)
    return _render_summary_projection(
        projection, root, project_purpose=project_purpose
    )


def extract_project_purpose(summary: str) -> str | None:
    """Pull the Project purpose section out of an existing summary.md.

    Returns None when missing or still placeholder, so the regenerator
    knows to fall back to PROJECT_MAP.md or the default template. L-037
    broadened the placeholder detection beyond the historical
    "Not described yet." check — without that, the `pjm init` placeholder
    ("Replace this placeholder...") was treated as real content and
    silently round-tripped forever, hiding the bug that motivated L-037.
    """
    match = re.search(
        r"^## (?:Project purpose|What this project is)\n(?P<body>.*?)(?=\n## |\Z)",
        summary,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return None
    body = match.group("body").strip()
    if _looks_like_placeholder(body):
        return None
    return body


def group_issue_events(events: list[Event]) -> dict[str, list[Event]]:
    issues: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        if event.issue_id:
            issues[event.issue_id].append(event)
    return dict(issues)


def collect_files(events: list[Event]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for event in events:
        for explicit in event.files:
            if explicit not in seen:
                seen.add(explicit)
                files.append(explicit)
        for inferred in infer_file_mentions(event.summary):
            if inferred not in seen:
                seen.add(inferred)
                files.append(inferred)
    return files


def infer_file_mentions(text: str) -> list[str]:
    pattern = r"(?<![\w/.-])[\w./-]+\.[A-Za-z0-9]+(?::\d+)?"
    return re.findall(pattern, text)


def _format_issue_event(event: Event) -> str:
    detail = (
        f"- {event.timestamp} `{event.type}`: {event.summary}"
        f"{_event_record_location(event.to_dict())}"
    )
    if event.outcome:
        detail += f" ({event.outcome})"
    return detail


def _write_projection_issue_files(
    projection: dict[str, object],
    root: Path,
    *,
    affected_issue_ids: set[str],
    remove_stale: bool,
    event_records: list[Event] | None = None,
) -> None:
    """Write only changed issue documents, pruning them after a full rebuild."""
    raw_issues = projection.get("issues")
    issues = raw_issues if isinstance(raw_issues, dict) else {}
    directory = issues_dir(root)
    expected_paths: set[Path] = set()
    grouped_new_events = group_issue_events(event_records or [])

    if remove_stale:
        issue_ids_to_write = set(issues)
    else:
        issue_ids_to_write = set(affected_issue_ids)

    for issue_id in issue_ids_to_write:
        issue = issues.get(issue_id)
        if not isinstance(issue_id, str) or not isinstance(issue, dict):
            continue
        issue_summary = issue.get("summary")
        if not isinstance(issue_summary, str) or not issue_summary:
            continue
        path = directory / f"{issue_id}-{slugify(issue_summary)}.md"
        expected_paths.add(path)
        new_events = grouped_new_events.get(issue_id, [])
        if remove_stale or not path.exists():
            lines = [f"# #{issue_id} {issue_summary}", ""]
            for event in new_events:
                lines.append(_format_issue_event(event))
            lines.append("")
            _write_if_changed(path, "\n".join(lines))
        elif new_events:
            # The existing issue markdown is itself a per-issue projection.  A
            # suffix update reads and rewrites only this affected document;
            # historical Event objects are not reconstructed or serialized in
            # the global summary index.
            existing = path.read_text(encoding="utf-8")
            prefix = existing.rstrip("\n")
            suffix = "\n".join(_format_issue_event(event) for event in new_events)
            updated = f"{prefix}\n{suffix}\n\n"
            _write_if_changed(path, updated, existing=existing)
        try:
            file_stat = path.stat()
        except OSError as exc:
            raise ProjectMemError(f"Could not stat issue projection {path}: {exc}") from exc
        issue["file"] = {
            "device": int(file_stat.st_dev),
            "inode": int(file_stat.st_ino),
            "size": int(file_stat.st_size),
            "mtime_ns": int(file_stat.st_mtime_ns),
        }

    if remove_stale:
        for path in directory.glob("*.md"):
            if path not in expected_paths:
                path.unlink(missing_ok=True)


def write_issue_files(events: list[Event], root: Path | None = None) -> None:
    metadata = {"device": 0, "inode": 0, "size": 0, "mtime_ns": 0}
    projection = _projection_from_events(events, metadata)
    project_root = root
    if project_root is None:
        project_root = issues_dir(None).parent
    _write_projection_issue_files(
        projection,
        project_root,
        affected_issue_ids=set(projection["issues"]),
        remove_stale=False,
        event_records=events,
    )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "issue")[:48].strip("-") or "issue"
