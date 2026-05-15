"""Smart Context Injection — wrap AI agent sessions with project memory.

Intercepts AI agent startup and auto-prepends a token-budgeted context
block. The right memory at the right time, injected before the agent
sees your first message.

Usage:
    pjm wrap claude                     # wrap Claude Code
    pjm wrap cursor                     # wrap Cursor
    pjm wrap claude --tokens 3000       # custom budget
    pjm wrap claude --focus src/auth/   # file-specific context
    pjm wrap claude --preview           # show what would be injected
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from projectmem.commands.context import generate_context
from projectmem.storage import read_events, require_mem_dir


# ── Agent configurations ──
AGENTS = {
    "claude": {
        "description": "Claude Code (CLI)",
        "injection": "claude_md",       # inject via CLAUDE.md
        "command": "claude",
        "fallback_commands": ["claude"],
    },
    "cursor": {
        "description": "Cursor IDE",
        "injection": "cursorrules",     # inject via .cursorrules
        "command": None,                # no CLI launch
    },
    "aider": {
        "description": "Aider",
        "injection": "message_prefix",  # inject via --message flag
        "command": "aider",
        "fallback_commands": ["aider"],
    },
    "generic": {
        "description": "Generic (clipboard copy)",
        "injection": "clipboard",
        "command": None,
    },
}

# Marker for injected context in files
CONTEXT_MARKER_START = "<!-- projectmem:context:start -->"
CONTEXT_MARKER_END = "<!-- projectmem:context:end -->"


def run(
    agent: str = "claude",
    tokens: int = 2000,
    focus: str | None = None,
    recent: str | None = None,
    preview: bool = False,
    no_warnings: bool = False,
    root: Path | None = None,
) -> None:
    """Wrap an AI agent session with project memory context."""
    root_path = root or Path.cwd()
    require_mem_dir(root_path)

    # Normalize agent name
    agent = agent.lower().strip()
    if agent not in AGENTS:
        typer.echo(
            f"Unknown agent: {agent}\n"
            f"Supported: {', '.join(AGENTS.keys())}\n"
            f"Using 'generic' (clipboard) mode.",
            err=True,
        )
        agent = "generic"

    config = AGENTS[agent]
    events = read_events(root_path)

    if not events:
        typer.echo("No events found. Run some pjm commands first to build memory.")
        return

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

    # Generate context
    result = generate_context(
        events,
        token_budget=tokens,
        focus=focus,
        recent_days=recent_days,
        root=root_path,
    )

    context_md = result["markdown"]
    tokens_used = result["tokens_used"]
    level = result["compression_level"]

    if preview:
        _show_preview(context_md, tokens_used, tokens, level, config)
        return

    # Inject context based on agent type
    injection = config["injection"]

    if injection == "claude_md":
        _inject_claude_md(root_path, context_md)
    elif injection == "cursorrules":
        _inject_cursorrules(root_path, context_md)
    elif injection == "message_prefix":
        _inject_message_prefix(root_path, context_md, config, tokens_used)
    elif injection == "clipboard":
        _inject_clipboard(context_md, tokens_used)

    # Launch the agent if it has a CLI command
    cmd = config.get("command")
    if cmd:
        _launch_agent(cmd, config.get("fallback_commands", []), root_path)


def _show_preview(
    context_md: str,
    tokens_used: int,
    budget: int,
    level: str,
    config: dict,
) -> None:
    """Show what would be injected without actually doing it."""
    dim = "\033[2m"
    bold = "\033[1m"
    cyan = "\033[36m"
    reset = "\033[0m"

    typer.echo(f"\n{bold}Context Preview{reset}")
    typer.echo(f"{dim}{'─' * 50}{reset}")
    typer.echo(f"  Agent:       {cyan}{config['description']}{reset}")
    typer.echo(f"  Injection:   {config['injection']}")
    typer.echo(f"  Tokens:      {tokens_used}/{budget} ({level})")
    typer.echo(f"{dim}{'─' * 50}{reset}\n")
    typer.echo(context_md)
    typer.echo(f"\n{dim}{'─' * 50}{reset}")
    typer.echo(f"{dim}Run without --preview to inject and launch.{reset}\n")


def _inject_claude_md(root: Path, context_md: str) -> None:
    """Inject context into CLAUDE.md for Claude Code."""
    claude_md = root / "CLAUDE.md"
    wrapped = f"\n{CONTEXT_MARKER_START}\n{context_md}\n{CONTEXT_MARKER_END}\n"

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        # Remove old injection if present
        if CONTEXT_MARKER_START in content:
            start = content.index(CONTEXT_MARKER_START)
            end = content.index(CONTEXT_MARKER_END) + len(CONTEXT_MARKER_END)
            content = content[:start] + content[end:]
        content = content.rstrip("\n") + "\n" + wrapped
    else:
        content = (
            "# CLAUDE.md\n\n"
            "This file provides context for Claude Code sessions.\n"
            + wrapped
        )

    claude_md.write_text(content, encoding="utf-8")
    typer.echo(
        "\033[32m[projectmem]\033[0m Context injected into CLAUDE.md"
    )


def _inject_cursorrules(root: Path, context_md: str) -> None:
    """Inject context into .cursorrules for Cursor IDE."""
    cursorrules = root / ".cursorrules"
    wrapped = f"\n{CONTEXT_MARKER_START}\n{context_md}\n{CONTEXT_MARKER_END}\n"

    if cursorrules.exists():
        content = cursorrules.read_text(encoding="utf-8")
        if CONTEXT_MARKER_START in content:
            start = content.index(CONTEXT_MARKER_START)
            end = content.index(CONTEXT_MARKER_END) + len(CONTEXT_MARKER_END)
            content = content[:start] + content[end:]
        content = content.rstrip("\n") + "\n" + wrapped
    else:
        content = (
            "# Cursor Rules\n\n"
            "Read `.projectmem/AI_INSTRUCTIONS.md` before working.\n"
            + wrapped
        )

    cursorrules.write_text(content, encoding="utf-8")
    typer.echo(
        "\033[32m[projectmem]\033[0m Context injected into .cursorrules"
    )


def _inject_message_prefix(
    root: Path, context_md: str, config: dict, tokens_used: int
) -> None:
    """Write context to a temp file for agents that accept --message."""
    context_file = root / ".projectmem" / "context_inject.md"
    context_file.write_text(context_md, encoding="utf-8")
    typer.echo(
        f"\033[32m[projectmem]\033[0m Context written to {context_file} "
        f"({tokens_used} tokens)"
    )
    typer.echo(
        f"  Tip: Use with --message flag or paste at the start of your session."
    )


def _inject_clipboard(context_md: str, tokens_used: int) -> None:
    """Copy context to clipboard."""
    try:
        if sys.platform == "darwin":
            proc = subprocess.Popen(
                ["pbcopy"], stdin=subprocess.PIPE, timeout=5
            )
            proc.communicate(context_md.encode("utf-8"))
        elif sys.platform.startswith("linux"):
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE,
                timeout=5,
            )
            proc.communicate(context_md.encode("utf-8"))
        else:
            # Fallback: write to file
            _inject_message_prefix(
                Path.cwd(), context_md, {}, tokens_used
            )
            return

        typer.echo(
            f"\033[32m[projectmem]\033[0m Context copied to clipboard "
            f"({tokens_used} tokens)\n"
            f"  Paste at the start of your AI session."
        )
    except (OSError, subprocess.TimeoutExpired):
        typer.echo(
            f"\033[33m[projectmem]\033[0m Clipboard not available. "
            f"Context saved to .projectmem/context_inject.md"
        )
        context_file = Path.cwd() / ".projectmem" / "context_inject.md"
        context_file.write_text(context_md, encoding="utf-8")


def _launch_agent(cmd: str, fallbacks: list[str], root: Path) -> None:
    """Launch the AI agent CLI."""
    # Find the command
    exe = shutil.which(cmd)
    if not exe:
        for fb in fallbacks:
            exe = shutil.which(fb)
            if exe:
                break

    if not exe:
        typer.echo(
            f"\033[33m[projectmem]\033[0m {cmd} not found in PATH. "
            f"Context has been injected — launch {cmd} manually."
        )
        return

    typer.echo(f"\033[36m[projectmem]\033[0m Launching {cmd}...\n")

    # Replace current process with the agent
    os.execvp(exe, [exe])
