from __future__ import annotations

import json
from pathlib import Path

import tiktoken
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.global_memory import (
    INHERITED_GOTCHA_PREVIEW_LIMIT,
    INHERITED_MEMORY_TOKEN_BUDGET,
    INHERITED_PATTERN_PREVIEW_LIMIT,
    build_inherited_instructions,
    detect_stack,
    get_relevant_entries,
    read_gotchas,
)

runner = CliRunner()
from projectmem.models import Event
from projectmem.storage import (
    append_event,
    global_memory_enabled,
    initialize,
    set_global_memory_enabled,
)


def _python_project(root: Path, *, global_enabled: bool | None = None) -> None:
    root.mkdir()
    initialize(root, global_enabled=global_enabled)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["pytest"]\n',
        encoding="utf-8",
    )


def test_global_memory_is_enabled_by_default(tmp_path: Path) -> None:
    initialize(tmp_path)

    assert global_memory_enabled(tmp_path)
    assert "global_memory_enabled" not in (
        tmp_path / ".projectmem" / "config.toml"
    ).read_text(encoding="utf-8")


def test_init_no_global_persists_the_write_opt_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "init",
            "--no-global",
            "--no-hooks",
            "--no-watch",
            "--no-backfill",
            "--no-claude-md",
            "--no-mcp-config",
            "--no-structure",
        ],
    )

    assert result.exit_code == 0
    assert not global_memory_enabled(tmp_path)


def test_opted_out_project_does_not_auto_promote_or_inherit(
    tmp_path: Path, monkeypatch
) -> None:
    global_root = tmp_path / "global"
    import projectmem.global_memory as global_memory

    monkeypatch.setattr(global_memory, "GLOBAL_DIR", global_root)
    project = tmp_path / "project"
    _python_project(project, global_enabled=False)

    assert not global_memory_enabled(project)
    config = (project / ".projectmem" / "config.toml").read_text(encoding="utf-8")
    assert "global_memory_enabled = false" in config

    append_event(
        Event(type="note", summary="gotcha: pytest fixtures need explicit cleanup"),
        project,
    )
    assert not global_root.exists()

    # A persisted opt-out also suppresses inheritance on a later init/detect
    # path, while the stack shape itself remains available to callers.
    global_root.mkdir(parents=True)
    (global_root / "library_gotchas.jsonl").write_text(
        '{"library":"pytest","gotcha":"old lesson"}\n',
        encoding="utf-8",
    )
    stack = detect_stack(project)
    assert "pytest" in stack["libraries"]
    assert get_relevant_entries(stack) == {"patterns": [], "gotchas": []}


def test_default_project_still_auto_promotes_lessons(
    tmp_path: Path, monkeypatch
) -> None:
    global_root = tmp_path / "global"
    import projectmem.global_memory as global_memory

    monkeypatch.setattr(global_memory, "GLOBAL_DIR", global_root)
    project = tmp_path / "project"
    _python_project(project)

    append_event(
        Event(type="note", summary="gotcha: pytest fixtures need explicit cleanup"),
        project,
    )

    gotchas = read_gotchas()
    assert len(gotchas) == 1
    assert gotchas[0]["library"] == "pytest"


def test_global_memory_preference_can_be_toggled_without_losing_other_config(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)
    set_global_memory_enabled(tmp_path, False)
    set_global_memory_enabled(tmp_path, True)

    config = (tmp_path / ".projectmem" / "config.toml").read_text(encoding="utf-8")
    assert "summary_size_limit_kb = 20" in config
    assert "recent_days = 30" in config
    assert "global_memory_enabled = true" in config
    assert global_memory_enabled(tmp_path)


def test_inherited_instructions_keep_a_token_bounded_preview() -> None:
    gotchas = [
        {
            "library": "fastapi",
            "gotcha": f"fastapi lesson {index}: use explicit lifecycle handling",
            "discovered": f"2026-01-{(index % 28) + 1:02d}T00:00:00Z",
        }
        for index in range(118)
    ]
    patterns = [
        {
            "pattern": f"pattern {index}: keep project state local",
            "created": f"2026-02-{(index % 28) + 1:02d}T00:00:00Z",
        }
        for index in range(10)
    ]

    rendered = build_inherited_instructions({"gotchas": gotchas, "patterns": patterns})
    tokens = len(tiktoken.get_encoding("cl100k_base").encode(rendered))

    assert tokens <= INHERITED_MEMORY_TOKEN_BUDGET
    assert rendered.count("- **fastapi**") <= INHERITED_GOTCHA_PREVIEW_LIMIT
    assert rendered.count("- pattern ") <= INHERITED_PATTERN_PREVIEW_LIMIT
    assert "Relevant now: 118 library gotchas and 10 patterns." in rendered
    assert "get_global_gotchas(project_id, library)" in rendered


def test_init_replaces_old_bulk_inheritance_with_bounded_preview(
    tmp_path: Path, monkeypatch
) -> None:
    from projectmem import global_memory
    from projectmem.commands.init import _inherit_global_memory

    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    monkeypatch.setattr(global_memory, "GLOBAL_DIR", tmp_path / "global")
    project = tmp_path / "project"
    _python_project(project)

    entries = [
        {
            "id": f"got_{index}",
            "library": "pytest",
            "gotcha": f"pytest global lesson {index}: keep teardown explicit",
            "discovered": f"2026-03-{(index % 28) + 1:02d}T00:00:00Z",
        }
        for index in range(118)
    ]
    global_memory.gotchas_path().write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )

    _inherit_global_memory(project)
    _inherit_global_memory(project)
    content = (project / ".projectmem" / "AI_INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )
    section = content.split("## Global Memory — Inherited Knowledge", 1)[1].split(
        "\n## Rules", 1
    )[0]

    assert content.count("## Global Memory — Inherited Knowledge") == 1
    assert "Relevant now: 118 library gotchas and 0 patterns." in section
    assert len(tiktoken.get_encoding("cl100k_base").encode(section)) <= (
        INHERITED_MEMORY_TOKEN_BUDGET
    )
