from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import typer

from projectmem.storage import MEM_DIR


# Marker used to identify projectmem's section in git hooks
HOOK_MARKER_START = "# >>> projectmem auto-capture >>>"
HOOK_MARKER_END = "# <<< projectmem auto-capture <<<"


# ── L-047: bake the absolute path to `pjm` into the hook ────────────────
#
# Git invokes hooks via a non-interactive bash (no `.zshrc`/`.bashrc` runs),
# so conda / pyenv / venv PATH modifications are absent. The old snippet
# relied on `command -v pjm`, which silently returns false in that context
# and made the hook a no-op for every conda user. We now resolve the
# absolute path at install time and write it into the hook, with a runtime
# fallback to `command -v` for the rare case where the install-time binary
# was moved.

def _resolve_pjm_binary() -> str:
    """Find an absolute path to a working pjm-equivalent CLI.

    Preference order:
      1. ``pjm`` on PATH (most common — pip-installed entry point)
      2. ``projectmem`` on PATH (alias entry point)
      3. ``<sys.prefix>/bin/pjm`` (conda / venv layout where the entry
         point lives next to the python that imported this module)
      4. Bare ``"pjm"`` as a last resort — preserves prior behaviour and
         the runtime fallback in the snippet can still find it.
    """
    found = shutil.which("pjm") or shutil.which("projectmem")
    if found:
        return found
    venv_guess = Path(sys.prefix) / "bin" / "pjm"
    if venv_guess.exists():
        return str(venv_guess)
    return "pjm"


def _auto_capture_snippet(pjm_path: str, capture_arg: str) -> str:
    """Auto-capture hook snippet with a baked absolute path + runtime fallback.

    Redirects BOTH stdout and stderr to /dev/null so the backgrounded
    capture process never prints over the user's shell prompt after
    `git commit` returns (L-050). Users can verify capture with `pjm show`.
    """
    return (
        f"{HOOK_MARKER_START}\n"
        "# Automatically captures development events into projectmem.\n"
        "# Installed by: pjm hooks install (or pjm init)\n"
        "# Remove with:  pjm hooks uninstall\n"
        f'PJM_BIN="{pjm_path}"\n'
        'if [ ! -x "$PJM_BIN" ]; then\n'
        '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
        'fi\n'
        'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
        f'    "$PJM_BIN" _auto-capture "{capture_arg}" >/dev/null 2>&1 &\n'
        'fi\n'
        f"{HOOK_MARKER_END}\n"
    )


def _precheck_snippet(pjm_path: str) -> str:
    """Pre-commit precheck snippet with a baked absolute path + runtime fallback."""
    return (
        f"{HOOK_MARKER_START}\n"
        "# Pre-commit warning check against project memory.\n"
        "# Installed by: pjm hooks install (or pjm init)\n"
        "# Remove with:  pjm hooks uninstall\n"
        "# Bypass once:  git commit --no-verify\n"
        f'PJM_BIN="{pjm_path}"\n'
        'if [ ! -x "$PJM_BIN" ]; then\n'
        '    PJM_BIN="$(command -v pjm 2>/dev/null || command -v projectmem 2>/dev/null)"\n'
        'fi\n'
        'if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]; then\n'
        '    "$PJM_BIN" precheck --level warn || true\n'
        'fi\n'
        f"{HOOK_MARKER_END}\n"
    )


# Back-compat aliases for code/tests that imported the old constants. These
# resolve the binary at import time, which matches the old semantics for
# anyone who imported the snippet directly.
HOOK_SNIPPET = _auto_capture_snippet(_resolve_pjm_binary(), '$1')
PRECHECK_SNIPPET = _precheck_snippet(_resolve_pjm_binary())

# Hook types we install, with the argument passed to _auto-capture
HOOK_CONFIGS = {
    "post-commit": "commit",
    "post-merge": "merge",
}


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
    than overwriting the file. The pjm binary path is resolved at install
    time and baked into the hook (L-047), so the hook works under conda /
    pyenv / venv where the interactive-shell PATH isn't inherited by git.
    """
    installed: list[str] = []
    pjm_path = _resolve_pjm_binary()

    # Auto-capture hooks (post-commit, post-merge)
    for hook_name, capture_arg in HOOK_CONFIGS.items():
        hook_path = hooks_dir / hook_name
        snippet = _auto_capture_snippet(pjm_path, capture_arg)

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
    precheck_snippet = _precheck_snippet(pjm_path)
    if precommit_path.exists():
        content = precommit_path.read_text(encoding="utf-8")
        if HOOK_MARKER_START not in content:
            content = content.rstrip("\n") + "\n\n" + precheck_snippet
            precommit_path.write_text(content, encoding="utf-8")
            _make_executable(precommit_path)
            installed.append("pre-commit")
    else:
        precommit_path.write_text(
            "#!/usr/bin/env bash\n" + precheck_snippet, encoding="utf-8"
        )
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
