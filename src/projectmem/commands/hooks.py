from __future__ import annotations

import os
import stat
from pathlib import Path

import typer

from projectmem.storage import MEM_DIR


# Marker used to identify projectmem's section in git hooks
HOOK_MARKER_START = "# >>> projectmem auto-capture >>>"
HOOK_MARKER_END = "# <<< projectmem auto-capture <<<"

# The bash snippet injected into git hooks.
# Uses `pjm _auto-capture` internal command instead of shell-based classification.
HOOK_SNIPPET = f"""{HOOK_MARKER_START}
# Automatically captures development events into projectmem.
# Installed by: pjm hooks install (or pjm init)
# Remove with:  pjm hooks uninstall
if [ -d ".projectmem" ]; then
    if command -v pjm &> /dev/null; then
        PJM_CMD="pjm"
    elif command -v projectmem &> /dev/null; then
        PJM_CMD="projectmem"
    else
        PJM_CMD=""
    fi
    if [ -n "$PJM_CMD" ]; then
        $PJM_CMD _auto-capture "$1" 2>/dev/null &
    fi
fi
{HOOK_MARKER_END}
"""

# Hook types we install, with the argument passed to _auto-capture
HOOK_CONFIGS = {
    "post-commit": "commit",
    "post-merge": "merge",
}

# Pre-commit snippet — different from auto-capture since it must block
PRECHECK_SNIPPET = f"""{HOOK_MARKER_START}
# Pre-commit warning check against project memory.
# Installed by: pjm hooks install (or pjm init)
# Remove with:  pjm hooks uninstall
# Bypass once:  git commit --no-verify
if [ -d ".projectmem" ]; then
    if command -v pjm &> /dev/null; then
        pjm precheck --level warn || true
    elif command -v projectmem &> /dev/null; then
        projectmem precheck --level warn || true
    fi
fi
{HOOK_MARKER_END}
"""


def run(action: str = "install", root: Path | None = None) -> None:
    root_path = root or Path.cwd()
    hooks_dir = root_path / ".git" / "hooks"

    if not hooks_dir.exists():
        typer.echo(
            "Error: .git/hooks directory not found. Is this a git repository?",
            err=True,
        )
        return

    if action == "install":
        install_hooks(hooks_dir)
    elif action == "uninstall":
        uninstall_hooks(hooks_dir)
    else:
        typer.echo(f"Unknown action: {action}")


def install_hooks(hooks_dir: Path) -> None:
    """Install projectmem auto-capture into git hooks.

    Safe for existing hooks — appends a clearly-marked snippet rather
    than overwriting the file.
    """
    installed: list[str] = []

    # Auto-capture hooks (post-commit, post-merge)
    for hook_name, capture_arg in HOOK_CONFIGS.items():
        hook_path = hooks_dir / hook_name
        snippet = HOOK_SNIPPET.replace('"$1"', f'"{capture_arg}"')

        if hook_path.exists():
            content = hook_path.read_text(encoding="utf-8")
            # Already installed — skip
            if HOOK_MARKER_START in content:
                continue
            # Append to existing hook
            content = content.rstrip("\n") + "\n\n" + snippet
        else:
            content = "#!/usr/bin/env bash\n" + snippet

        hook_path.write_text(content, encoding="utf-8")
        _make_executable(hook_path)
        installed.append(hook_name)

    # Pre-commit hook (for precheck warnings)
    precommit_path = hooks_dir / "pre-commit"
    if precommit_path.exists():
        content = precommit_path.read_text(encoding="utf-8")
        if HOOK_MARKER_START not in content:
            content = content.rstrip("\n") + "\n\n" + PRECHECK_SNIPPET
            precommit_path.write_text(content, encoding="utf-8")
            _make_executable(precommit_path)
            installed.append("pre-commit")
    else:
        precommit_path.write_text("#!/usr/bin/env bash\n" + PRECHECK_SNIPPET, encoding="utf-8")
        _make_executable(precommit_path)
        installed.append("pre-commit")

    if installed:
        typer.echo(
            f"projectmem git hooks installed: {', '.join(installed)}\n"
            "  Auto-captures: commits, reverts, merges, fixes, features, breaking changes.\n"
            "  Pre-commit: warns about repeating failed approaches and high-churn files."
        )
    else:
        typer.echo("projectmem git hooks already installed.")


def uninstall_hooks(hooks_dir: Path) -> None:
    """Remove projectmem's snippet from git hooks without touching other content."""
    removed: list[str] = []

    # All hook names we may have touched
    all_hooks = list(HOOK_CONFIGS) + ["pre-commit"]

    for hook_name in all_hooks:
        hook_path = hooks_dir / hook_name
        if not hook_path.exists():
            continue
        content = hook_path.read_text(encoding="utf-8")
        if HOOK_MARKER_START not in content:
            continue

        # Remove the projectmem snippet
        lines = content.split("\n")
        new_lines: list[str] = []
        skip = False
        for line in lines:
            if HOOK_MARKER_START in line:
                skip = True
                continue
            if HOOK_MARKER_END in line:
                skip = False
                continue
            if not skip:
                new_lines.append(line)

        remaining = "\n".join(new_lines).strip()
        if remaining in ("", "#!/usr/bin/env bash", "#!/bin/sh"):
            # Hook file is now empty — remove it
            hook_path.unlink()
        else:
            hook_path.write_text(remaining + "\n", encoding="utf-8")
        removed.append(hook_name)

    if removed:
        typer.echo(f"projectmem hooks removed from: {', '.join(removed)}")
    else:
        typer.echo("No projectmem hooks found to remove.")


def _make_executable(path: Path) -> None:
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
