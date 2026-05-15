from __future__ import annotations

import typer

from projectmem.storage import summary_path


def run() -> None:
    typer.echo(summary_path().read_text(encoding="utf-8"))
