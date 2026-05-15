from __future__ import annotations

import typer

from projectmem.storage import project_map_path


def run() -> None:
    typer.echo(project_map_path().read_text(encoding="utf-8"))
