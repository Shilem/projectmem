"""Context Budget Optimizer — generate token-budgeted project memory.

Produces optimally compressed context tailored to a token budget,
file focus area, and time window. Output is markdown suitable for
injection into AI agent prompts.

Usage:
    pjm context                          # 2000 tokens, all files, 30d
    pjm context --tokens 500             # compressed
    pjm context --focus src/auth/        # file-specific
    pjm context --recent 3d             # time window
    pjm context --format json            # structured output
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import typer

try:
    import tiktoken
except ImportError:  # pragma: no cover - exercised in minimal installations
    tiktoken = None  # type: ignore[assignment]

from projectmem.models import Event, superseded_ids
from projectmem.storage import (
    project_map_path,
    read_events,
    require_mem_dir,
)

# ``cl100k_base`` is a stable, model-independent OpenAI encoding.  It is a
# declared dependency and is used for budgeting only; the returned metadata
# makes clear that this is not a claim about every provider's tokenizer.
TOKENIZER_ENCODING = "cl100k_base"
TOKENIZER_NAME = f"tiktoken/{TOKENIZER_ENCODING}"
# Section builders use this only as a cheap sizing heuristic.  It is never
# reported as a token count and the final assembly is checked by tiktoken.
_SECTION_CHAR_HEURISTIC = 4
DEFAULT_TOKEN_BUDGET = 2000
MIN_TOKEN_BUDGET = 100
MAX_TOKEN_BUDGET = 20000

# Canonical project setting for the default context budget.  Keep parsing
# dependency-free because projectmem supports Python 3.10, whose stdlib does
# not include ``tomllib``.
_TOKEN_CONFIG_KEY = "context_token_budget"


class ContextTokenizerUnavailable(RuntimeError):
    """Raised when the configured tokenizer cannot be loaded or used."""


@lru_cache(maxsize=1)
def _get_context_encoding() -> Any:
    """Load the one supported context encoding, without an estimate fallback."""
    if tiktoken is None:
        raise ContextTokenizerUnavailable(
            "Context tokenization unavailable: install the `tiktoken` dependency "
            f"to load {TOKENIZER_ENCODING}."
        )
    try:
        return tiktoken.get_encoding(TOKENIZER_ENCODING)
    except Exception as exc:  # encoding data may be unavailable offline
        raise ContextTokenizerUnavailable(
            f"Context tokenization unavailable: could not load "
            f"tiktoken encoding {TOKENIZER_ENCODING}: {exc}"
        ) from exc


def _encode_context(text: str, encoding: Any | None = None) -> list[int]:
    """Encode context text and convert dependency failures into clear errors."""
    encoder = encoding if encoding is not None else _get_context_encoding()
    try:
        # Context is generated text, so special-looking markers are ordinary
        # content rather than control tokens.
        return encoder.encode(text, disallowed_special=())
    except Exception as exc:
        raise ContextTokenizerUnavailable(
            f"Context tokenization unavailable: failed to encode text with "
            f"tiktoken/{TOKENIZER_ENCODING}: {exc}"
        ) from exc


def count_context_tokens(text: str) -> int:
    """Return the exact token count under the supported context encoding."""
    return len(_encode_context(text))


def _truncate_to_token_budget(
    text: str, token_budget: int, encoding: Any | None = None
) -> tuple[str, int]:
    """Truncate text and return ``(text, exact_token_count)`` within a budget."""
    encoder = encoding if encoding is not None else _get_context_encoding()
    token_ids = _encode_context(text, encoder)
    if len(token_ids) <= token_budget:
        return text, len(token_ids)

    # Decode a token prefix instead of slicing characters.  This keeps the
    # budget in the tokenizer's units and handles CJK, combining characters,
    # and emoji consistently with the declared encoding.
    try:
        prefix = encoder.decode(token_ids[:token_budget])
    except Exception as exc:
        raise ContextTokenizerUnavailable(
            f"Context tokenization unavailable: failed to decode the "
            f"{TOKENIZER_ENCODING} token prefix: {exc}"
        ) from exc
    used = len(_encode_context(prefix, encoder))

    # Most BPE prefixes round-trip exactly.  Keep a character-boundary safety
    # path for encodings that decode a partial UTF-8 token into replacement
    # characters whose re-encoding could consume more tokens.
    if used > token_budget:
        low, high = 0, len(prefix)
        best = ""
        best_used = 0
        while low <= high:
            mid = (low + high) // 2
            candidate = prefix[:mid]
            candidate_used = len(_encode_context(candidate, encoder))
            if candidate_used <= token_budget:
                best, best_used = candidate, candidate_used
                low = mid + 1
            else:
                high = mid - 1
        prefix, used = best, best_used

    # Make truncation observable where the marker itself fits.  Re-check the
    # complete string: the marker is content and therefore part of the cap.
    marker = "…"
    if prefix and prefix != text:
        marker_tokens = len(_encode_context(marker, encoder))
        if marker_tokens < token_budget:
            available = token_budget - marker_tokens
            try:
                marked_prefix = (
                    encoder.decode(token_ids[:available]).rstrip() + marker
                )
            except Exception as exc:
                raise ContextTokenizerUnavailable(
                    f"Context tokenization unavailable: failed to decode the "
                    f"{TOKENIZER_ENCODING} truncation marker: {exc}"
                ) from exc
            marked_used = len(_encode_context(marked_prefix, encoder))
            if marked_used <= token_budget:
                prefix, used = marked_prefix, marked_used
    return prefix, used

# ── Scoring weights ──
TYPE_BASE_SCORES = {
    "attempt": 8,    # failed attempts are highest value
    "fix": 6,
    "decision": 5,
    "issue": 4,
    "note": 2,
    "hypothesis": 3,
}

OUTCOME_MULTIPLIERS = {
    "failed": 2.0,   # failed approaches are most valuable
    "worked": 0.8,
    "partial": 1.2,
    None: 1.0,
}


def _smart_truncate(text: str, limit: int) -> str:
    """Trim `text` to <= `limit` chars without slicing mid-word (L-022b).

    Prefers the last sentence boundary that fits, then word boundary, then
    falls back to a hard slice with an ellipsis to make truncation visible.
    """
    if text is None or len(text) <= limit:
        return text
    # Try sentence boundary first.
    head = text[:limit]
    for sep in (". ", "! ", "? "):
        idx = head.rfind(sep)
        if idx >= max(40, limit // 2):
            return head[: idx + 1] + " …"
    # Fall back to word boundary.
    idx = head.rfind(" ")
    if idx >= max(20, limit // 3):
        return head[:idx].rstrip(",;:—-") + " …"
    return head.rstrip() + "…"


def _is_backfill_event(event: Event) -> bool:
    """True for synthetic events created by `pjm init --backfill` (L-022a).

    These events tag every tracked file with the same commit message and
    pollute the File Gotchas section of `pjm wrap` with one identical
    entry per file. Filter them out so File Gotchas reflect real, logged
    per-file signal (or stay empty, which is honest).
    """
    notes = (event.notes or "").lower()
    if notes.startswith("auto-backfilled") or "auto-backfilled" in notes:
        return True
    return bool(event.files and len(event.files) > 5 and event.type == "note")


def _read_context_config(root: Path | None = None) -> dict[str, Any]:
    """Read optional context settings from the project's config file.

    Configuration is a convenience default, not a second source of events.
    A missing or malformed config therefore leaves the normal defaults in
    place; callers still validate any value that is actually selected.
    """
    root_path = root or Path.cwd()
    config_path = root_path / ".projectmem" / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    # The project config written by ``pjm init`` is deliberately flat.  Parse
    # only the scalar settings this command owns instead of adding a TOML
    # dependency merely to read one integer.
    data: dict[str, Any] = {}
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        if key != _TOKEN_CONFIG_KEY:
            continue
        data[key] = value.strip().strip('"').strip("'")
    return data


def configured_token_budget(root: Path | None = None) -> int | None:
    """Return a valid configured context budget, if one is present."""
    config = _read_context_config(root)
    value = config.get(_TOKEN_CONFIG_KEY)
    if value is None or isinstance(value, bool):
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    if MIN_TOKEN_BUDGET <= budget <= MAX_TOKEN_BUDGET:
        return budget
    return None


def resolve_token_budget(
    requested: int | None = None,
    root: Path | None = None,
    *,
    use_config: bool = True,
) -> int:
    """Resolve and validate a context budget for CLI/MCP entry points."""
    budget = requested
    if budget is None and use_config:
        budget = configured_token_budget(root)
    if budget is None:
        budget = DEFAULT_TOKEN_BUDGET
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise TypeError("Context token budget must be an integer.")
    if not MIN_TOKEN_BUDGET <= budget <= MAX_TOKEN_BUDGET:
        raise ValueError(
            f"Context token budget must be between {MIN_TOKEN_BUDGET} and "
            f"{MAX_TOKEN_BUDGET} tokens."
        )
    return budget


def generate_context(
    events: list[Event],
    token_budget: int | None = None,
    focus: str | None = None,
    recent_days: int = 30,
    root: Path | None = None,
    *,
    use_config: bool = True,
) -> dict[str, Any]:
    """Generate compressed context within a token budget.

    Returns dict with markdown, exact token accounting, and selection metadata.
    """
    root_path = root or Path.cwd()
    # Resolve here as well as at the CLI/wrap/MCP boundaries so direct Python
    # callers share the same config/override contract.  Passing an explicit
    # integer always wins over the project default.
    token_budget = resolve_token_budget(
        token_budget, root_path, use_config=use_config
    )
    encoding = _get_context_encoding()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=recent_days)

    # ── Gather context signals ──
    git_files = _get_git_status_files(root_path)

    # Superseded decisions remain in the append-only log for auditability but
    # must not be injected into a live agent context.  This mirrors summary
    # and export, which already expose only current decisions.
    retired = superseded_ids(events)

    # ── Score and filter events ──
    scored: list[tuple[float, Event]] = []
    for event in events:
        if event.id in retired:
            continue
        score = _score_event(event, now, cutoff, focus, git_files)
        if score > 0:
            scored.append((score, event))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── Determine compression level ──
    if token_budget >= 4000:
        level = "full"
    elif token_budget >= 1000:
        level = "compressed"
    elif token_budget >= 200:
        level = "ultra"
    else:
        level = "emergency"

    # ── Build sections ──
    # Character sizing is only used to avoid asking section builders to do
    # excessive work.  It is deliberately not used for accounting.
    char_budget = token_budget * _SECTION_CHAR_HEURISTIC
    sections: list[str] = []
    chars_used = 0

    # Header
    header = (
        f"## projectmem context (budget: {token_budget} tokens; "
        f"encoding: {TOKENIZER_NAME})\n"
    )
    chars_used += len(header)
    sections.append(header)

    # 1. WARNINGS — always included regardless of budget
    warnings = _build_warnings(scored, level)
    if warnings:
        chars_used += len(warnings)
        sections.append(warnings)

    # 2. Recent Decisions (if budget allows)
    if chars_used < char_budget * 0.6:
        decisions = _build_decisions(scored, level, char_budget - chars_used)
        if decisions:
            chars_used += len(decisions)
            sections.append(decisions)

    # 3. Relevant Fixes / Lessons (if budget allows)
    if chars_used < char_budget * 0.8:
        fixes = _build_fixes(scored, level, char_budget - chars_used)
        if fixes:
            chars_used += len(fixes)
            sections.append(fixes)

    # 4. File Gotchas (if budget allows and focus specified)
    if chars_used < char_budget * 0.9:
        gotchas = _build_file_gotchas(scored, focus, level, char_budget - chars_used)
        if gotchas:
            chars_used += len(gotchas)
            sections.append(gotchas)

    # 5. Architecture context (ultra-compressed from PROJECT_MAP)
    if chars_used < char_budget * 0.95 and level in ("full", "compressed"):
        arch = _build_arch_context(root_path, focus, char_budget - chars_used)
        if arch:
            chars_used += len(arch)
            sections.append(arch)

    # Section builders intentionally optimize relevance and may produce a
    # warning block larger than the remaining budget.  Enforce the contract
    # at the final assembly boundary so every caller receives a hard cap.
    markdown, tokens_used = _truncate_to_token_budget(
        "\n".join(sections), token_budget, encoding
    )
    return {
        "markdown": markdown,
        "tokens_used": tokens_used,
        "token_budget": token_budget,
        "tokenizer": TOKENIZER_NAME,
        "token_count_method": "exact",
        "compression_level": level,
        "events_scored": len(scored),
        "events_total": len(events),
        "focus": focus,
        "recent_days": recent_days,
    }


def _score_event(
    event: Event,
    now: datetime,
    cutoff: datetime,
    focus: str | None,
    git_files: list[str],
) -> float:
    """Score an event for context relevance."""
    # Parse timestamp
    try:
        ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        ts = now - timedelta(days=15)  # middle-of-range fallback

    # Skip events before cutoff
    # Still include unresolved failures even if old.
    if ts < cutoff and not (event.type == "attempt" and event.outcome == "failed"):
        return 0.0

    # Base score by type
    base = TYPE_BASE_SCORES.get(event.type, 1)

    # Outcome multiplier
    outcome_mult = OUTCOME_MULTIPLIERS.get(event.outcome, 1.0)

    # Recency decay
    age_days = (now - ts).total_seconds() / 86400
    if age_days <= 1:
        recency = 1.0
    elif age_days <= 7:
        recency = 0.8
    elif age_days <= 30:
        recency = 0.5
    else:
        recency = 0.2

    # File relevance
    event_files = set(event.files or [])
    if event.location and ":" in event.location:
        event_files.add(event.location.split(":")[0])

    file_relevance = 0.3  # base relevance
    if focus:
        for f in event_files:
            if f.startswith(focus) or focus in f:
                file_relevance = 1.0
                break
            # Same directory
            if "/" in f and "/" in focus and f.rsplit("/", 1)[0] == focus.rstrip("/"):
                file_relevance = 0.7
                break

    # Git status boost (files currently being worked on)
    if git_files:
        for f in event_files:
            if f in git_files:
                file_relevance = max(file_relevance, 0.9)
                break

    # Unresolved boost
    resolution_boost = 1.5 if (event.type == "attempt" and event.outcome == "failed") else 1.0

    return base * outcome_mult * recency * file_relevance * resolution_boost


def _build_warnings(
    scored: list[tuple[float, Event]], level: str
) -> str:
    """Build WARNINGS section — failed approaches and unresolved issues."""
    warnings: list[str] = []
    for _, event in scored:
        if event.type == "attempt" and event.outcome == "failed":
            summary = event.summary
            if level == "emergency":
                summary = _smart_truncate(summary, 60)
            elif level == "ultra":
                summary = _smart_truncate(summary, 100)
            loc = f" [{event.location}]" if event.location and level != "emergency" else ""
            age = _age_label(event.timestamp)
            warnings.append(f"- FAILED: {summary}{loc} ({age})")
            if len(warnings) >= (3 if level in ("ultra", "emergency") else 8):
                break

    if not warnings:
        return ""
    return "### WARNINGS — Do Not Repeat\n" + "\n".join(warnings) + "\n"


def _build_decisions(
    scored: list[tuple[float, Event]], level: str, char_remaining: int
) -> str:
    """Build recent decisions section."""
    decisions: list[str] = []
    chars = 0
    limit = 3 if level in ("ultra", "emergency") else 6

    for _, event in scored:
        if event.type == "decision":
            summary = event.summary
            if level == "ultra":
                summary = _smart_truncate(summary, 80)
            line = f"- {summary} ({_age_label(event.timestamp)})\n"
            if chars + len(line) > char_remaining:
                break
            decisions.append(line)
            chars += len(line)
            if len(decisions) >= limit:
                break

    if not decisions:
        return ""
    return "### Recent Decisions\n" + "".join(decisions)


def _build_fixes(
    scored: list[tuple[float, Event]], level: str, char_remaining: int
) -> str:
    """Build fixes/lessons section."""
    fixes: list[str] = []
    chars = 0
    limit = 3 if level in ("ultra", "emergency") else 6

    for _, event in scored:
        if event.type == "fix":
            summary = event.summary
            if level == "ultra":
                summary = _smart_truncate(summary, 80)
            files_str = ""
            if event.files and level in ("full", "compressed"):
                files_str = "\n  Files: " + ", ".join(event.files[:3])
            line = f"- Fixed: {summary} ({_age_label(event.timestamp)}){files_str}\n"
            if chars + len(line) > char_remaining:
                break
            fixes.append(line)
            chars += len(line)
            if len(fixes) >= limit:
                break

    if not fixes:
        return ""
    return "### Relevant Fixes\n" + "".join(fixes)


def _build_file_gotchas(
    scored: list[tuple[float, Event]],
    focus: str | None,
    level: str,
    char_remaining: int,
) -> str:
    """Build file-specific gotchas section."""
    if level == "emergency":
        return ""

    file_events: dict[str, list[str]] = defaultdict(list)
    for _, event in scored:
        if _is_backfill_event(event):
            continue  # L-022a: don't pollute File Gotchas with backfill noise
        event_files = list(event.files or [])
        if event.location and ":" in event.location:
            event_files.append(event.location.split(":")[0])
        for f in event_files:
            if focus and not (f.startswith(focus) or focus in f):
                continue
            label = event.type.upper()
            if event.outcome:
                label += f" ({event.outcome})"
            file_events[f].append(f"{label}: {_smart_truncate(event.summary, 60)}")

    if not file_events:
        return ""

    lines: list[str] = ["### File Gotchas\n"]
    chars = 0
    for f, notes in sorted(file_events.items(), key=lambda x: -len(x[1]))[:5]:
        line = f"- `{f}`: {notes[0]}\n"
        if chars + len(line) > char_remaining:
            break
        lines.append(line)
        chars += len(line)

    return "".join(lines) if len(lines) > 1 else ""


def _build_arch_context(
    root: Path, focus: str | None, char_remaining: int
) -> str:
    """Extract a brief architecture context from PROJECT_MAP.md."""
    map_path = project_map_path(root)
    if not map_path.exists():
        return ""

    content = map_path.read_text(encoding="utf-8")
    if "not created yet" in content.lower():
        return ""

    # Extract relevant lines (those mentioning the focus path, or first 5 lines of structure)
    lines = content.strip().split("\n")
    relevant: list[str] = []

    for line in lines:
        if focus and focus.rstrip("/") in line or line.startswith("- ") and ("`" in line or "/" in line):
            relevant.append(line.strip())

    if not relevant:
        return ""

    result = "### Architecture Context\n"
    chars = len(result)
    for line in relevant[:8]:
        if chars + len(line) + 1 > char_remaining:
            break
        result += line + "\n"
        chars += len(line) + 1

    return result


def _get_git_status_files(root: Path) -> list[str]:
    """Get files from git status (currently modified/staged)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--no-renames"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        files = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                # porcelain format: XY filename
                files.append(line[3:].strip())
        return files
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _age_label(timestamp: str) -> str:
    """Convert timestamp to human-readable age."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                return "just now"
            return f"{hours}h ago"
        elif days == 1:
            return "yesterday"
        elif days < 7:
            return f"{days}d ago"
        elif days < 30:
            weeks = days // 7
            return f"{weeks}w ago"
        else:
            months = days // 30
            return f"{months}mo ago"
    except (ValueError, AttributeError):
        return "unknown"


def run(
    tokens: int | None = None,
    focus: str | None = None,
    recent: str | None = None,
    fmt: str = "md",
    root: Path | None = None,
) -> None:
    """Generate and print context within token budget."""
    require_mem_dir(root)
    events = read_events(root)

    effective_tokens = resolve_token_budget(
        tokens, root, use_config=tokens is None
    )

    # Parse --recent
    recent_days = 30
    if recent:
        recent = recent.strip().lower()
        if recent.endswith("d"):
            recent_days = int(recent[:-1])
        elif recent.endswith("w"):
            recent_days = int(recent[:-1]) * 7
        elif recent.endswith("m"):
            recent_days = int(recent[:-1]) * 30
        else:
            recent_days = int(recent)

    result = generate_context(
        events,
        token_budget=effective_tokens,
        focus=focus,
        recent_days=recent_days,
        root=root,
        use_config=False,
    )

    if fmt == "json":
        # Include markdown as a field in JSON
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(result["markdown"])
