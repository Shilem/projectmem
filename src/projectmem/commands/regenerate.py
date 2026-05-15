from __future__ import annotations

import typer

from projectmem.summary import regenerate_summary


def run() -> None:
    path = regenerate_summary()
    typer.echo(f"Regenerated {path}")
