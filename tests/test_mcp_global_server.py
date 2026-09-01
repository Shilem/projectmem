from __future__ import annotations

import json
from pathlib import Path

from projectmem import mcp_global_server
from projectmem.global_memory import add_gotcha
from projectmem.project_registry import project_id_for_root
from projectmem.storage import initialize


def _project(
    root: Path,
    *,
    package: str | None = None,
    global_enabled: bool = True,
) -> tuple[Path, str]:
    root.mkdir()
    if package:
        (root / "pyproject.toml").write_text(
            f"[project]\nname = '{root.name}'\ndependencies = ['{package}']\n",
            encoding="utf-8",
        )
    initialize(root, global_enabled=global_enabled)
    return root, project_id_for_root(root)


def test_list_projects_is_root_free_and_reports_ready_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    first, first_id = _project(tmp_path / "first")
    second, second_id = _project(tmp_path / "second")

    payload = json.loads(mcp_global_server.list_projects())

    assert {item["project_id"] for item in payload["projects"]} == {
        first_id,
        second_id,
    }
    assert {item["name"] for item in payload["projects"]} == {"first", "second"}
    assert all(item["status"] == "ready" for item in payload["projects"])
    assert str(first) not in mcp_global_server.list_projects()
    assert str(second) not in mcp_global_server.list_projects()


def test_explicit_project_id_works_from_unrelated_cwd_and_unknown_does_not_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    _project_root, project_id = _project(tmp_path / "project")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    assert "# projectmem - project" in mcp_global_server.get_summary(project_id)
    unknown = mcp_global_server.get_summary("not-registered")
    assert "Unknown ProjectMem project_id" in unknown
    assert "# projectmem - project" not in unknown


def test_events_and_fts_are_isolated_by_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    first, first_id = _project(tmp_path / "first")
    second, second_id = _project(tmp_path / "second")

    mcp_global_server.add_note(first_id, "first-only-event-marker")
    mcp_global_server.add_note(second_id, "second-only-event-marker")

    first_results = mcp_global_server.search_events(first_id, "first-only-event-marker")
    second_results = mcp_global_server.search_events(second_id, "first-only-event-marker")

    assert "first-only-event-marker" in first_results
    assert "No events match" in second_results
    assert "first-only-event-marker" not in (
        second / ".projectmem" / "search.sqlite3"
    ).read_bytes().decode("utf-8", errors="ignore")
    assert "first-only-event-marker" in (first / ".projectmem" / "events.jsonl").read_text(
        encoding="utf-8"
    )


def test_global_gotchas_honor_opt_out_and_stack_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    from projectmem import global_memory

    monkeypatch.setattr(global_memory, "GLOBAL_DIR", tmp_path / "global")
    python_project, python_id = _project(tmp_path / "python", package="requests")
    opted_out, opted_out_id = _project(
        tmp_path / "opted-out", package="requests", global_enabled=False
    )
    # Stack detection's dependency parser intentionally handles one package
    # per line in requirements.txt; keep the fixture aligned with that
    # established input contract.
    (python_project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (opted_out / "requirements.txt").write_text("requests\n", encoding="utf-8")
    add_gotcha("requests", "requests-only-global-lesson")
    add_gotcha("react", "react-only-global-lesson")

    python_results = mcp_global_server.get_global_gotchas(python_id)
    opted_out_results = mcp_global_server.get_global_gotchas(opted_out_id)

    assert "requests-only-global-lesson" in python_results
    assert "react-only-global-lesson" not in python_results
    assert "Global memory is disabled" in opted_out_results
    assert str(python_project) not in python_results
    assert str(opted_out) not in opted_out_results


def test_list_projects_surfaces_stale_identity_without_exposing_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry"))
    project, project_id = _project(tmp_path / "stale")
    # Preserve the registry record, then make its registered root disappear.
    import shutil

    shutil.rmtree(project)
    payload = json.loads(mcp_global_server.list_projects())
    item = next(item for item in payload["projects"] if item["project_id"] == project_id)
    assert item["status"] == "deleted"
    assert "root" not in item
    assert str(project) not in mcp_global_server.list_projects()


def test_global_tool_schemas_require_project_id_and_legacy_entry_remains_available():
    tools = mcp_global_server.mcp._tool_manager._tools
    project_tools = set(tools) - {"list_projects"}
    assert project_tools
    assert all("project_id" in tools[name].parameters["required"] for name in project_tools)

    from projectmem import mcp_server

    assert mcp_server.mcp.name == "projectmem"
    assert "pjm-mcp-global" in Path("pyproject.toml").read_text(encoding="utf-8")
