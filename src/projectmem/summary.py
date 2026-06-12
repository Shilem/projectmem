from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from projectmem.models import Event, superseded_ids
from projectmem.storage import issues_dir, project_map_path, read_events, summary_path


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


def _looks_like_placeholder(text: str) -> bool:
    """True if `text` is empty or contains a known placeholder phrase."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return any(phrase in stripped for phrase in _PLACEHOLDER_PHRASES)


def regenerate_summary(root: Path | None = None) -> Path:
    events = read_events(root)
    path = summary_path(root)
    existing_summary = path.read_text(encoding="utf-8") if path.exists() else ""

    # L-037: Project purpose is structural, not event-derived. Pull it from
    # PROJECT_MAP.md (the user-/AI-authored project description) so any
    # update to PROJECT_MAP.md flows through to summary.md on the next
    # regen. Falls back to whatever summary.md had before (legacy repos),
    # then to the default placeholder.
    try:
        map_purpose = extract_project_purpose_from_map(project_map_path(root))
    except Exception:
        map_purpose = None
    project_purpose = map_purpose or extract_project_purpose(existing_summary)

    content = build_summary(events, root or Path.cwd(), project_purpose=project_purpose)
    path.write_text(content, encoding="utf-8")
    write_issue_files(events, root)
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
    project_name = root.name
    now = datetime.now(timezone.utc).date().isoformat()
    issues = group_issue_events(events)
    # Superseded decisions stay in the log (append-only audit trail) but
    # drop out of the live summary — only the current decision should steer
    # an AI session. Retired ones remain reachable via `pjm search`.
    retired = superseded_ids(events)
    decisions = [
        event
        for event in events
        if event.type == "decision" and event.id not in retired
    ]
    notes = [event for event in events if event.type == "note"]

    lines = [
        f"# projectmem - {project_name}",
        "",
        f"_Last updated: {now}_",
        "",
        "## Project purpose",
        project_purpose or (
            "Replace this placeholder with a concise description of what this "
            "project does, who it serves, and the main technologies or runtime "
            "assumptions."
        ),
        "",
        "## Recent issues",
    ]

    if not issues:
        lines.append("- No issues logged yet.")
    else:
        for issue_id, issue_events in sorted(issues.items(), reverse=True):
            issue = next(event for event in issue_events if event.type == "issue")
            fix = next((event for event in reversed(issue_events) if event.type == "fix"), None)
            status = "fixed" if fix else "open"
            marker = "DONE" if fix else "OPEN"
            
            issue_loc = f" [{issue.location}]" if issue.location else ""
            if fix:
                fix_loc = f" [{fix.location}]" if fix.location else ""
                outcome = f" -> {fix.summary}{fix_loc}"
            else:
                outcome = ""
            
            lines.append(f"- [{marker}] #{issue_id} {issue.summary}{issue_loc}{outcome} ({status})")
            # Surface non-worked attempts. Both `failed` and `partial` outcomes
            # encode lessons the next session needs — dropping `partial` (L-027b)
            # would let an AI repeat work that already got 80% of the way there.
            lessons = [
                event
                for event in issue_events
                if event.type == "attempt" and event.outcome in ("failed", "partial")
            ]
            label = {"failed": "Failed attempt", "partial": "Partial attempt"}
            for lesson_event in lessons[-3:]:
                loc = f" [{lesson_event.location}]" if lesson_event.location else ""
                tag = label.get(lesson_event.outcome or "failed", "Attempt")
                lines.append(f"  - {tag}: {lesson_event.summary}{loc}")

    lines.extend(["", "## Decisions"])
    if decisions:
        for event in decisions:
            loc = f" [{event.location}]" if event.location else ""
            lines.append(f"- {event.summary}{loc}")
    else:
        lines.append("- No decisions logged yet.")

    lines.extend(["", "## Notes"])
    if notes:
        for event in notes[-10:]:
            loc = f" [{event.location}]" if event.location else ""
            lines.append(f"- {event.summary}{loc}")
    else:
        lines.append("- No notes logged yet.")

    lines.extend(["", "## Key files"])
    key_files = collect_files(events)
    if key_files:
        for file_path in key_files[:20]:
            lines.append(f"- `{file_path}`")
    else:
        lines.append("- No key files logged yet.")

    lines.extend(["", "## Open questions"])
    lines.append("- None logged yet.")
    lines.append("")
    return "\n".join(lines)


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


def write_issue_files(events: list[Event], root: Path | None = None) -> None:
    for issue_id, issue_events in group_issue_events(events).items():
        issue = next((event for event in issue_events if event.type == "issue"), None)
        if issue is None:
            continue
        slug = slugify(issue.summary)
        path = issues_dir(root) / f"{issue_id}-{slug}.md"
        lines = [f"# #{issue_id} {issue.summary}", ""]
        for event in issue_events:
            loc = f" [{event.location}]" if event.location else ""
            detail = f"- {event.timestamp} `{event.type}`: {event.summary}{loc}"
            if event.outcome:
                detail += f" ({event.outcome})"
            lines.append(detail)
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "issue")[:48].strip("-") or "issue"
