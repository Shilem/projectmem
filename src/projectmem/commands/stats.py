"""ROI Dashboard — thin presentation layer over `pjm score`.

`pjm stats` and `pjm score` used to report different token-saved totals
because each had its own home-grown weighting (L-025d). They now share a
single source of truth: `score.calculate_score`. The viral-stat brag line
(L-025c) is gated behind a minimum-savings threshold so the very first
`pjm stats` call doesn't print "Lord of the Rings ... 0.0 times."
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from projectmem.commands.score import calculate_score
from projectmem.storage import events_path, require_mem_dir

# Roughly the total token count of the LOTR trilogy. Used only for the
# brag line — and only when we've actually crossed enough savings to make
# the number meaningful (>= ~0.5 trilogies). Below the threshold we say
# nothing rather than say "0.0 times."
LOTR_TOKENS = 500_000
LOTR_MIN_TOKENS_FOR_BRAG = 250_000  # ~0.5 trilogies


def _load_events(root: Path | None = None) -> list[dict[str, Any]]:
    path = events_path(root)
    raw: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw.append(json.loads(line))
    return raw


def compute_stats(root: Path | None = None) -> dict[str, Any]:
    """Return ROI numbers derived from `pjm score`'s scoring model."""
    require_mem_dir(root)
    raw = _load_events(root)
    score = calculate_score(raw)
    tokens_saved = score["value"]["tokens_saved"]
    return {
        "tokens_saved": tokens_saved,
        "usd_saved": score["value"]["usd_saved"],
        "debugging_hours_saved": score["value"]["debugging_hours_saved"],
        "score": score["score"],
        "grade": score["grade"],
        "components": score["components"],
        "capture": score["capture"],
    }


def calculate_savings(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Back-compat shim for `pjm visualize` and any external callers.

    Forwards to `calculate_score` so all surfaces share a single ROI model.
    The shape mirrors the legacy keys (`total_tokens`, `usd_saved`,
    `breakdown`) so the dashboard's stats card keeps rendering, but
    `total_tokens` now matches `pjm score`'s `tokens_saved` exactly.
    """
    score = calculate_score(events)
    tokens = score["value"]["tokens_saved"]
    components = score["components"]
    return {
        "total_tokens": tokens,
        "usd_saved": score["value"]["usd_saved"],
        "breakdown": {
            "failed_approaches": components["failed_approaches"],
            "decisions": components["decisions_documented"],
            "fixes": components["fixes_with_context"],
            "notes": components["notes_count"],
            "files_with_gotchas": components["files_with_gotchas"],
        },
    }


def run(fmt: str = "text", root: Path | None = None) -> None:
    """Display the ROI dashboard."""
    data = compute_stats(root)

    if fmt == "json":
        typer.echo(json.dumps(data, indent=2))
        return

    tokens = data["tokens_saved"]
    usd = data["usd_saved"]

    typer.echo("")
    typer.echo("━" * 40)
    typer.echo("  PROJECTMEM TOKEN ROI DASHBOARD")
    typer.echo("━" * 40)
    typer.echo(f"  Score:                 {data['score']}/100 ({data['grade']})")
    typer.echo(f"  Total Tokens Saved:    {tokens:,}")
    typer.echo(f"  Estimated USD Saved:   ${usd:.2f}")
    typer.echo(f"  Debug Hours Saved:     ~{data['debugging_hours_saved']:.1f}h")
    typer.echo("-" * 40)
    typer.echo(
        f"  Failed approaches:   {data['components']['failed_approaches']}\n"
        f"  Decisions:           {data['components']['decisions_documented']}\n"
        f"  Fixes:               {data['components']['fixes_with_context']}\n"
        f"  Auto-captured:       {data['capture']['auto_captured']}"
    )
    typer.echo("-" * 40)
    if tokens >= LOTR_MIN_TOKENS_FOR_BRAG:
        typer.echo(
            f"  Viral: projectmem has saved enough tokens to read the\n"
            f"  entire 'Lord of the Rings' trilogy "
            f"{tokens / LOTR_TOKENS:.1f} times."
        )
    else:
        typer.echo("  Keep going — every issue and fix you log compounds.")
    typer.echo("━" * 40)
    typer.echo("")
