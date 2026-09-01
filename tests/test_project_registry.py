"""Focused coverage for the global ProjectMem project registry."""

from __future__ import annotations

import json
import multiprocessing
import shutil
from pathlib import Path
from queue import Empty
from uuid import uuid4

import pytest

from projectmem.project_registry import (
    PROJECT_ID_PREFIX,
    AmbiguousProjectError,
    ProjectDeletedError,
    ProjectNotInitializedError,
    ProjectPathChangedError,
    RegistryCorruptError,
    UnknownProjectError,
    load_registry,
    project_id_for_root,
    register_project_record,
    registry_path,
    resolve_project_root,
)
from projectmem.storage import initialize, registered_projects


def _initialized(root: Path) -> Path:
    root.mkdir()
    initialize(root, global_enabled=False)
    return root


def _write_records(records: list[dict[str, str]]) -> None:
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry_path().write_text(
        json.dumps({"version": 1, "projects": records}), encoding="utf-8"
    )


def test_legacy_path_list_is_read_and_next_registration_migrates(tmp_path):
    first = _initialized(tmp_path / "first")
    second = _initialized(tmp_path / "second")
    registry_path().write_text(
        json.dumps([str(first), str(second), str(first)]), encoding="utf-8"
    )

    records = load_registry()

    assert [record.root for record in records] == [first.resolve(), second.resolve()]
    assert [record.project_id for record in records] == [
        project_id_for_root(first),
        project_id_for_root(second),
    ]

    register_project_record(first)
    migrated = json.loads(registry_path().read_text(encoding="utf-8"))
    assert migrated["version"] == 1
    assert all("project_id" in item and "root" in item for item in migrated["projects"])


def test_legacy_initialized_project_gets_persisted_uuid_on_read(tmp_path):
    root = tmp_path / "old"
    (root / ".projectmem").mkdir(parents=True)
    (root / ".projectmem" / "config.toml").write_text(
        "summary_size_limit_kb = 20\n", encoding="utf-8"
    )
    registry_path().write_text(json.dumps([str(root.resolve())]), encoding="utf-8")

    records = load_registry()

    assert len(records) == 1
    persisted = (root / ".projectmem" / "config.toml").read_text(encoding="utf-8")
    assert f'project_id = "{records[0].project_id}"' in persisted
    assert json.loads(registry_path().read_text(encoding="utf-8"))["version"] == 1


def test_project_id_is_stable_for_canonical_alias_and_not_a_root(tmp_path):
    root = _initialized(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    assert project_id_for_root(root) == project_id_for_root(alias)
    record = register_project_record(alias)
    assert record.project_id != str(root)
    assert record.root == root.resolve()
    assert resolve_project_root(record.project_id) == root.resolve()


def test_recreated_path_does_not_reuse_deleted_project_identity(tmp_path):
    root = _initialized(tmp_path / "reused")
    old_record = register_project_record(root)
    shutil.rmtree(root / ".projectmem")
    initialize(root, global_enabled=False)
    new_record = register_project_record(root)

    assert new_record.project_id != old_record.project_id
    with pytest.raises(ProjectPathChangedError):
        resolve_project_root(old_record.project_id)
    assert resolve_project_root(new_record.project_id) == root.resolve()


def test_moved_project_can_re_register_with_persisted_identity(tmp_path):
    original = _initialized(tmp_path / "original")
    old_record = register_project_record(original)
    moved = tmp_path / "moved"
    original.rename(moved)

    new_record = register_project_record(moved)

    assert new_record.project_id == old_record.project_id
    assert resolve_project_root(old_record.project_id) == moved.resolve()


def test_resolver_rejects_unknown_id_without_root_or_cwd_fallback(
    tmp_path, monkeypatch
):
    root = _initialized(tmp_path / "project")
    monkeypatch.chdir(root)

    with pytest.raises(UnknownProjectError):
        resolve_project_root(str(root))
    with pytest.raises(UnknownProjectError):
        resolve_project_root("not-registered")


def test_resolver_reports_ambiguous_duplicate_records(tmp_path):
    root = _initialized(tmp_path / "project")
    record = register_project_record(root)
    _write_records(
        [
            {"project_id": record.project_id, "root": str(root.resolve())},
            {"project_id": record.project_id, "root": str(root.resolve())},
        ]
    )

    with pytest.raises(AmbiguousProjectError):
        resolve_project_root(record.project_id)


def test_resolver_reports_deleted_and_uninitialized_projects(tmp_path):
    deleted = _initialized(tmp_path / "deleted")
    deleted_id = register_project_record(deleted).project_id
    shutil.rmtree(deleted)
    with pytest.raises(ProjectDeletedError):
        resolve_project_root(deleted_id)

    uninitialized = tmp_path / "uninitialized"
    uninitialized.mkdir()
    uninitialized_id = f"{PROJECT_ID_PREFIX}{uuid4().hex}"
    _write_records(
        [{"project_id": uninitialized_id, "root": str(uninitialized.resolve())}]
    )
    with pytest.raises(ProjectNotInitializedError):
        resolve_project_root(uninitialized_id)


def test_resolver_rejects_path_replacement_after_registration(tmp_path):
    original = _initialized(tmp_path / "original")
    replacement = _initialized(tmp_path / "replacement")
    record = register_project_record(original)
    moved = tmp_path / "original-moved"
    original.rename(moved)
    (tmp_path / "original").symlink_to(replacement, target_is_directory=True)

    # The record's generated id no longer matches the canonical target.  It is
    # rejected while parsing the registry (before any path can be used).
    with pytest.raises((ProjectPathChangedError, RegistryCorruptError)):
        resolve_project_root(record.project_id)


@pytest.mark.parametrize(
    "payload",
    ["{", {"version": 99, "projects": []}, {"version": 1, "projects": "bad"}],
)
def test_corrupt_registry_fails_observably(payload):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryCorruptError):
        load_registry()


def _register_worker(root: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=30)
        record = register_project_record(Path(root))
        results.put((record.project_id, None))
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted by parent
        results.put((None, f"{type(exc).__name__}: {exc}"))


def test_concurrent_registration_preserves_every_record(tmp_path):
    roots = [_initialized(tmp_path / f"project-{index}") for index in range(12)]
    # initialize() registers each project. Reset to an empty valid registry so
    # every worker must append under the independent global lock.
    registry_path().write_text("[]", encoding="utf-8")

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(roots))
    results = context.Queue()
    processes = [
        context.Process(target=_register_worker, args=(str(root), barrier, results))
        for root in roots
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            pytest.fail("registry worker did not finish")
        assert process.exitcode == 0

    outcomes = []
    for _ in processes:
        try:
            outcomes.append(results.get(timeout=5))
        except Empty:
            pytest.fail("registry worker returned no result")
    errors = [error for _project_id, error in outcomes if error]
    assert not errors, "; ".join(errors)

    payload = json.loads(registry_path().read_text(encoding="utf-8"))
    assert payload["version"] == 1
    records = payload["projects"]
    assert len(records) == len(roots)
    assert {item["project_id"] for item in records} == {
        project_id_for_root(root) for root in roots
    }


def test_legacy_registered_projects_keeps_stale_filtering(tmp_path):
    live = _initialized(tmp_path / "live")
    stale = _initialized(tmp_path / "stale")
    registry_path().write_text(
        json.dumps([str(live.resolve()), str(stale.resolve())]), encoding="utf-8"
    )
    shutil.rmtree(stale / ".projectmem")

    assert registered_projects() == [live.resolve()]
