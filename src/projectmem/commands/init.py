from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from projectmem.storage import initialize


def run(
    no_hooks: bool = False,
    no_global: bool = False,
    no_watch: bool = False,
    no_backfill: bool = False,
    no_claude_md: bool = False,
    global_tags: str | None = None,
    root: Path | None = None,
) -> None:
    path = initialize(root)
    typer.echo(f"Initialized {path}")

    root_path = root or Path.cwd()

    # Drop a CLAUDE.md bridge so AI clients (Claude Code / Antigravity /
    # Cursor) call our MCP tools instead of re-scanning source. AI clients
    # honor root-level rule files even before the MCP server's own
    # `instructions=` field takes effect (L-004f).
    if not no_claude_md:
        _ensure_claude_md(root_path)

    # Auto-install git hooks unless opted out
    if not no_hooks:
        hooks_dir = root_path / ".git" / "hooks"
        if hooks_dir.exists():
            from projectmem.commands.hooks import install_hooks

            install_hooks(hooks_dir)
            typer.echo("  Auto-capture active — events will be logged automatically.")
        else:
            typer.echo(
                "  Note: No .git directory found. Run `pjm hooks install` after `git init`."
            )

    # Backfill from recent git history so the dashboard is meaningful immediately
    if not no_backfill:
        _try_auto_backfill(root_path)

    # Inherit global memory unless opted out
    if not no_global:
        _inherit_global_memory(root_path, global_tags)

    # Auto-start the file watcher (guarded — skips in CI / non-interactive / no-watchdog)
    if not no_watch:
        _try_auto_start_watch(root_path)

    typer.echo(
        "\n  Next: Start an AI session to write PROJECT_SUMMARY.md and PROJECT_MAP.md"
    )


_CLAUDE_MD_BRIDGE_START = "<!-- >>> projectmem bridge >>> -->"
_CLAUDE_MD_BRIDGE_END = "<!-- <<< projectmem bridge <<< -->"


def _claude_md_bridge() -> str:
    """The bridge block written into CLAUDE.md.

    Mirrors the MCP server's `instructions=` field (`mcp_server.py`):
    same session-start trio, same write-side rule. Keep the two in sync
    — divergence between them was the L-031 finding: CLAUDE.md said
    "call get_summary first" while the MCP field said "call
    get_instructions first," confusing AI clients that read both.

    Markers (`>>> projectmem bridge >>>` / `<<< projectmem bridge <<<`) let
    us safely re-emit / replace the block on later `pjm init` runs without
    clobbering the rest of the file.
    """
    return (
        f"{_CLAUDE_MD_BRIDGE_START}\n"
        "## projectmem (MANDATORY)\n\n"
        "This project uses projectmem for persistent memory + workflow rules.\n\n"
        "SESSION START — call these three MCP tools, in this order, BEFORE\n"
        "answering ANY question about this project:\n\n"
        "  1. `get_instructions()` — loads the project's mandatory workflow\n"
        "     rules. Without this you will not know how to log work\n"
        "     correctly, when to use `add_note` vs `add_decision`, or how\n"
        "     the event log is structured.\n"
        "  2. `get_summary()` — loads project content. Do NOT answer from\n"
        "     conversation history or by re-reading package.json / README /\n"
        "     source files.\n"
        "  3. `get_project_map()` — loads structural layout when relevant.\n\n"
        "BEFORE modifying ANY file:\n"
        "  - Call `precheck_file(path)` — check failure history first.\n\n"
        "DURING work — use MCP write tools, NEVER edit `.projectmem/`\n"
        "files directly via filesystem write:\n"
        "  - On a bug discovery → `log_issue(summary, location)`.\n"
        "  - After each fix attempt → `record_attempt(summary, outcome)`.\n"
        "  - After confirmation → `record_fix(summary)`.\n"
        "  - On a design choice → `add_decision(summary)`.\n"
        "  - On a gotcha / setup detail → `add_note(summary)`.\n\n"
        "Editing `.projectmem/summary.md` or `.projectmem/PROJECT_MAP.md`\n"
        "directly bypasses event logging and breaks audit replay. The\n"
        "summary file regenerates from `events.jsonl` automatically — write\n"
        "via the MCP tools and the summary will follow.\n\n"
        "Do not re-scan source files when MCP tools can give you the same\n"
        "answer in ~500 tokens instead of ~5000. This is not optional.\n"
        f"{_CLAUDE_MD_BRIDGE_END}\n"
    )


def _ensure_claude_md(root: Path) -> None:
    """Create or safely-update CLAUDE.md with the projectmem bridge block."""
    claude_md = root / "CLAUDE.md"
    bridge = _claude_md_bridge()
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        if _CLAUDE_MD_BRIDGE_START in content and _CLAUDE_MD_BRIDGE_END in content:
            # Replace existing bridge in-place.
            start = content.index(_CLAUDE_MD_BRIDGE_START)
            end = content.index(_CLAUDE_MD_BRIDGE_END) + len(_CLAUDE_MD_BRIDGE_END)
            new_content = content[:start] + bridge.rstrip() + content[end:]
            if new_content == content:
                return
            claude_md.write_text(new_content, encoding="utf-8")
            typer.echo("  CLAUDE.md: projectmem bridge refreshed.")
            return
        # Append, preserving the user's existing content.
        new_content = content.rstrip("\n") + "\n\n" + bridge
        claude_md.write_text(new_content, encoding="utf-8")
        typer.echo("  CLAUDE.md: projectmem bridge appended.")
        return
    claude_md.write_text("# CLAUDE.md\n\n" + bridge, encoding="utf-8")
    typer.echo("  CLAUDE.md: created with projectmem bridge.")


def _try_auto_backfill(root: Path) -> None:
    """Backfill recent git history into events.jsonl.

    Safe in all cases:
      - Fresh project (no commits) → silent no-op
      - Existing project → ingests last 20 commits, dedup'd against existing events
      - Not a git repo → silent skip
    """
    # Only run if we're in a git repo
    if not (root / ".git").exists():
        return

    try:
        from projectmem.commands.backfill import run as backfill_run
        # Capture stdout + stderr; we'll print our own one-liner.
        import io, contextlib
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            backfill_run(limit=20, root=root)
        # Extract success line if present; ignore "no git history" style errors.
        out = out_buf.getvalue().strip().splitlines()
        ingested = next((ln for ln in out if "Backfilled" in ln or "Added" in ln), None)
        if ingested:
            typer.echo(f"  History: {ingested.strip()}")
        # else: fresh repo or already up-to-date — stay quiet
    except Exception:
        # Never let backfill break init
        pass


def _try_auto_start_watch(root: Path) -> None:
    """Start pjm watch --daemon automatically if the environment supports it."""
    # Skip in CI / pipelines
    ci_markers = ("CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "GITLAB_CI",
                  "JENKINS_HOME", "TRAVIS", "CIRCLECI", "BUILDKITE")
    if any(os.environ.get(m) for m in ci_markers):
        return  # silently skip — don't spawn daemons in pipelines

    # Skip if stdout isn't a TTY (scripted / piped)
    if not sys.stdout.isatty():
        return

    # watchdog is a required dependency — start the daemon (handles its own fork-and-detach)
    try:
        from projectmem.commands.watch import _running_pid, _run_as_daemon

        if _running_pid(root) is not None:
            return  # Already running
        _run_as_daemon(root)
    except Exception:
        # Silent fallback — never block init on watcher failure
        pass


def _inherit_global_memory(root: Path, filter_tags: str | None = None) -> None:
    """Detect stack and inject relevant global memory into AI_INSTRUCTIONS.md."""
    from projectmem.global_memory import (
        detect_stack,
        get_relevant_entries,
        build_inherited_instructions,
        global_dir,
    )

    # Check if global memory exists at all
    gdir = global_dir()
    patterns_file = gdir / "patterns.jsonl"
    gotchas_file = gdir / "library_gotchas.jsonl"

    if not patterns_file.exists() and not gotchas_file.exists():
        return  # No global memory yet — skip silently

    # Detect stack
    stack = detect_stack(root)
    if not stack["tags"] and not stack["libraries"]:
        return  # Can't detect stack — skip

    # Parse filter tags
    tag_list = None
    if filter_tags:
        tag_list = [t.strip() for t in filter_tags.split(",")]

    # Get relevant entries
    relevant = get_relevant_entries(stack, filter_tags=tag_list)
    r_patterns = relevant["patterns"]
    r_gotchas = relevant["gotchas"]

    if not r_patterns and not r_gotchas:
        return  # Nothing relevant

    # Build and inject the instructions section
    instructions_section = build_inherited_instructions(relevant)
    if not instructions_section:
        return

    ai_path = root / ".projectmem" / "AI_INSTRUCTIONS.md"
    if ai_path.exists():
        content = ai_path.read_text(encoding="utf-8")

        # Remove old inherited section if present
        marker_start = "## Global Memory — Inherited Knowledge"
        if marker_start in content:
            # Find start and end of section
            start_idx = content.index(marker_start)
            # Find next ## heading or end of file
            rest = content[start_idx + len(marker_start):]
            next_heading = rest.find("\n## ")
            if next_heading >= 0:
                end_idx = start_idx + len(marker_start) + next_heading
            else:
                end_idx = len(content)
            content = content[:start_idx].rstrip("\n") + "\n\n" + content[end_idx:].lstrip("\n")

        # Append the new section before the Rules section if it exists
        rules_marker = "## Rules"
        if rules_marker in content:
            idx = content.index(rules_marker)
            content = content[:idx] + instructions_section + "\n" + content[idx:]
        else:
            content = content.rstrip("\n") + "\n\n" + instructions_section

        ai_path.write_text(content, encoding="utf-8")

    # Report
    tags_str = ", ".join(stack["tags"][:5])
    typer.echo(f"\n  Global memory: Detected stack [{tags_str}]")
    if r_gotchas:
        typer.echo(f"    → {len(r_gotchas)} library gotchas injected into AI_INSTRUCTIONS.md")
    if r_patterns:
        typer.echo(f"    → {len(r_patterns)} patterns injected into AI_INSTRUCTIONS.md")
