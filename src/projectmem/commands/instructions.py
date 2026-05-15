from __future__ import annotations

import typer

from projectmem.storage import ai_instructions_path


def run() -> None:
    typer.echo(ai_instructions_path().read_text(encoding="utf-8"))
