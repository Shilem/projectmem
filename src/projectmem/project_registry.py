"""Project-scoped registry for the single-process global MCP.

The registry is intentionally small and boring: ``projects.json`` is the
machine-global allow-list, while each project keeps its own ``.projectmem``
data.  A project id is minted once and persisted in the project's config;
callers cannot choose an id or use a root path as an implicit resolver
fallback.

Older releases wrote a JSON list of absolute paths.  That form is still read
and is migrated to the versioned record form on the next registration:

    {"version": 1, "projects": [{"project_id": "...", "root": "..."}]}

The module does not import :mod:`projectmem.storage` on purpose.  Storage
delegates its legacy registry helpers here, which keeps root validation and
the global lock usable by the MCP without an import cycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised only on POSIX
    msvcrt = None  # type: ignore[assignment]


MEM_DIR = ".projectmem"
CONFIG_FILE = "config.toml"
REGISTRY_FILE = "projects.json"
REGISTRY_LOCK_FILE = "projects.lock"
REGISTRY_VERSION = 1
PROJECT_ID_PREFIX = "proj_"
# New ids contain a UUID4 hex payload.  Keep accepting the historical
# path-derived 64-hex payload while a registry is being migrated; records are
# rewritten with the persisted UUID on the next registration.
_PROJECT_ID_RE = re.compile(rf"^{re.escape(PROJECT_ID_PREFIX)}[0-9a-f]{{32,64}}$")
PROJECT_ID_CONFIG_KEY = "project_id"


class ProjectRegistryError(RuntimeError):
    """Base class for observable project-registry failures."""


class RegistryCorruptError(ProjectRegistryError):
    """The registry exists but is not a supported, valid JSON document."""


class RegistryIOError(ProjectRegistryError):
    """The registry or its lock could not be read or written."""


class UnknownProjectError(ProjectRegistryError):
    """No registered project has the requested id."""


class AmbiguousProjectError(ProjectRegistryError):
    """A project id maps to more than one registry record."""


class ProjectDeletedError(ProjectRegistryError):
    """A registered project root no longer exists."""


class ProjectNotInitializedError(ProjectRegistryError):
    """A registered root is missing a valid ``.projectmem/config.toml``."""


class ProjectPathChangedError(ProjectRegistryError):
    """A registry path now resolves to a different canonical root."""


@dataclass(frozen=True)
class ProjectRecord:
    """One registered, stable project identity and its canonical root."""

    project_id: str
    root: Path

    @property
    def id(self) -> str:
        """Short alias useful to callers that model records as ``id``."""
        return self.project_id

    @property
    def path(self) -> Path:
        """Compatibility alias for callers that call the root a path."""
        return self.root


@dataclass
class _LockState:
    thread_lock: threading.RLock
    depth: int = 0
    handle: object | None = None


_LOCK_STATES: dict[Path, _LockState] = {}
_LOCK_STATES_GUARD = threading.Lock()


def _projectmem_home() -> Path:
    configured = os.environ.get("PROJECTMEM_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".projectmem"
    return base.resolve()


def registry_path() -> Path:
    """Return the machine-global project registry path."""
    return _projectmem_home() / REGISTRY_FILE


def registry_lock_path() -> Path:
    """Return the independent lock used for every global registry mutation."""
    return registry_path().with_name(REGISTRY_LOCK_FILE)


def _state_for(path: Path) -> _LockState:
    with _LOCK_STATES_GUARD:
        state = _LOCK_STATES.get(path)
        if state is None:
            state = _LockState(thread_lock=threading.RLock())
            _LOCK_STATES[path] = state
        return state


def _lock_file(handle: object) -> None:
    # ``handle`` is kept as object so importing the module does not need to
    # expose a platform-specific IO type in its public API.
    file_handle = handle  # type: ignore[assignment]
    if fcntl is not None:
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - defensive for unusual runtimes
        raise RegistryIOError("Project registry requires a file-locking API.")
    file_handle.seek(0, os.SEEK_END)
    if file_handle.tell() == 0:
        file_handle.write(b"\0")
        file_handle.flush()
    file_handle.seek(0)
    msvcrt.locking(file_handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(handle: object) -> None:
    file_handle = handle  # type: ignore[assignment]
    if fcntl is not None:
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - exercised only on Windows
        file_handle.seek(0)
        msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def global_file_lock(path: Path) -> Iterator[Path]:
    """Acquire a re-entrant, thread- and process-safe lock for ``path``.

    This generic helper is also exported for the global-memory writer.  The
    registry uses :func:`registry_lock`; callers protecting other files should
    pass a stable lock path under the same ``~/.projectmem`` home.
    """
    lock_path = Path(path).expanduser().resolve()
    state = _state_for(lock_path)
    state.thread_lock.acquire()
    entered = False
    try:
        if state.depth == 0:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+b")
                _lock_file(handle)
            except (OSError, RegistryIOError) as exc:
                if "handle" in locals():
                    try:
                        handle.close()  # type: ignore[union-attr]
                    except OSError:
                        pass
                raise RegistryIOError(
                    f"Could not lock ProjectMem global file {lock_path}: {exc}"
                ) from exc
            state.handle = handle
        state.depth += 1
        entered = True
        yield lock_path
    finally:
        try:
            if entered:
                state.depth -= 1
                if state.depth == 0:
                    handle = state.handle
                    state.handle = None
                    if handle is not None:
                        try:
                            _unlock_file(handle)
                        finally:
                            handle.close()  # type: ignore[union-attr]
        finally:
            state.thread_lock.release()


@contextmanager
def registry_lock() -> Iterator[Path]:
    """Serialize reads and writes of ``projects.json``."""
    with global_file_lock(registry_lock_path()) as path:
        yield path


# Explicit alias for the MCP/global-memory owner.  It intentionally points to
# the same lock file as registry_lock, so registry and machine-global lesson
# writes cannot interleave when a caller wraps both operations in this helper.
global_registry_lock = registry_lock
global_projectmem_lock = registry_lock


def _canonical_path(root: Path | str, *, strict: bool) -> Path:
    try:
        candidate = Path(root).expanduser()
    except (TypeError, ValueError) as exc:
        raise ProjectRegistryError(f"Invalid project root: {root!r}") from exc
    if not candidate.is_absolute():
        # A root supplied to registration may be relative to the caller's
        # current directory, but registry records themselves must never be
        # relative (otherwise resolution would accidentally use MCP CWD).
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=strict)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        if strict:
            raise ProjectDeletedError(
                f"Registered project root no longer exists: {candidate}"
            ) from exc
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as nested:
            raise ProjectRegistryError(
                f"Could not canonicalize project root {candidate}: {nested}"
            ) from nested


def _read_config_data(root: Path) -> dict[str, object]:
    if not root.exists() or not root.is_dir():
        raise ProjectDeletedError(f"Registered project root no longer exists: {root}")
    mem_dir = root / MEM_DIR
    if not mem_dir.is_dir():
        raise ProjectNotInitializedError(
            f"Project is not initialized (missing {mem_dir}): {root}"
        )
    config = mem_dir / CONFIG_FILE
    if not config.is_file():
        raise ProjectNotInitializedError(
            f"Project is not initialized (missing {config}): {root}"
        )
    try:
        content = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectNotInitializedError(
            f"Project configuration is unreadable: {config}"
        ) from exc
    # Validate syntax where the stdlib TOML parser is available.  Python 3.10
    # remains supported: there, existence/readability is the same contract as
    # initialize()'s historical tiny config writer.
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            parsed = tomllib.loads(content)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise ProjectNotInitializedError(
                f"Project configuration is invalid TOML: {config}"
            ) from exc
        return parsed

    # Python 3.10 has no stdlib TOML parser.  The project registry only needs
    # one root-level key there; retain the historical existence/readability
    # contract for the rest of the config file.
    match = re.search(
        rf"^\s*{re.escape(PROJECT_ID_CONFIG_KEY)}\s*=\s*['\"]([^'\"]+)['\"]",
        content,
        re.MULTILINE,
    )
    return {PROJECT_ID_CONFIG_KEY: match.group(1)} if match else {}


def _validate_initialized_root(root: Path) -> Path:
    _read_config_data(root)
    return root


def _configured_project_id(root: Path) -> str | None:
    raw = _read_config_data(root).get(PROJECT_ID_CONFIG_KEY)
    if raw is None:
        return None
    if not isinstance(raw, str) or not _PROJECT_ID_RE.fullmatch(raw):
        config = root / MEM_DIR / CONFIG_FILE
        raise ProjectNotInitializedError(
            f"Project configuration has an invalid project_id: {config}"
        )
    return raw


def _new_project_id() -> str:
    return f"{PROJECT_ID_PREFIX}{uuid4().hex}"


def _persist_project_id(root: Path, project_id: str) -> None:
    config = root / MEM_DIR / CONFIG_FILE
    try:
        content = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectNotInitializedError(
            f"Project configuration is unreadable: {config}"
        ) from exc
    line = f'{PROJECT_ID_CONFIG_KEY} = "{project_id}"\n'
    if re.search(rf"^\s*{re.escape(PROJECT_ID_CONFIG_KEY)}\s*=", content, re.MULTILINE):
        updated = re.sub(
            rf"^\s*{re.escape(PROJECT_ID_CONFIG_KEY)}\s*=.*$",
            line.rstrip("\n"),
            content,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        suffix = "" if content.endswith("\n") else "\n"
        updated = f"{content}{suffix}{line}"
    if updated != content:
        _atomic_write_text(config, updated)


def _ensure_project_id(root: Path) -> str:
    _validate_initialized_root(root)
    existing = _configured_project_id(root)
    if existing is not None:
        return existing
    project_id = _new_project_id()
    _persist_project_id(root, project_id)
    return project_id


def _legacy_project_id(root: Path) -> str:
    """Derive a temporary id for stale/uninitialized legacy entries."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return f"{PROJECT_ID_PREFIX}{digest}"


def canonicalize_project_root(
    root: Path | str, *, require_initialized: bool = True
) -> Path:
    """Canonicalize a root and optionally require a real ProjectMem config."""
    canonical = _canonical_path(root, strict=True)
    if not canonical.is_dir():
        raise ProjectDeletedError(f"Registered project root is not a directory: {root}")
    if require_initialized:
        _validate_initialized_root(canonical)
    return canonical


# Concise alias for integrations that already call the operation simply
# ``canonicalize_root``.
canonicalize_root = canonicalize_project_root


def project_id_for_root(root: Path | str) -> str:
    """Return the persisted id for an initialized canonical root.

    Projects initialized before the registry existed are assigned a UUID on
    first registration (or first call to this helper).  The write is guarded
    by the machine-global lock so two processes cannot mint different ids.
    """
    canonical = canonicalize_project_root(root, require_initialized=True)
    with registry_lock():
        return _ensure_project_id(canonical)


def _validate_record_fields(raw: object, *, source: Path, index: int) -> ProjectRecord:
    if not isinstance(raw, dict):
        raise RegistryCorruptError(
            f"Invalid project record at {source} index {index}: expected object"
        )
    project_id = raw.get("project_id")
    root_value = raw.get("root")
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise RegistryCorruptError(
            f"Invalid project_id at {source} index {index}: expected generated id"
        )
    if not isinstance(root_value, str) or not root_value:
        raise RegistryCorruptError(
            f"Invalid root at {source} index {index}: expected absolute path"
        )
    try:
        root = Path(root_value).expanduser()
    except (TypeError, ValueError) as exc:
        raise RegistryCorruptError(f"Invalid root at {source} index {index}") from exc
    if not root.is_absolute():
        raise RegistryCorruptError(
            f"Invalid root at {source} index {index}: relative paths are not allowed"
        )
    canonical = _canonical_path(root, strict=False)
    return ProjectRecord(project_id=project_id, root=canonical)


def _parse_registry_payload(
    payload: object, source: Path
) -> tuple[list[ProjectRecord], bool]:
    """Parse supported payloads; return records and whether the form is legacy."""
    if isinstance(payload, list):
        records: list[ProjectRecord] = []
        seen: set[tuple[str, Path]] = set()
        for index, item in enumerate(payload):
            if not isinstance(item, str) or not item:
                raise RegistryCorruptError(
                    f"Invalid legacy project path at {source} index {index}"
                )
            path = Path(item).expanduser()
            if not path.is_absolute():
                raise RegistryCorruptError(
                    f"Invalid legacy project path at {source} index {index}: "
                    "relative paths are not allowed"
                )
            canonical = _canonical_path(path, strict=False)
            try:
                project_id = _configured_project_id(canonical)
            except ProjectRegistryError:
                project_id = None
            project_id = project_id or _legacy_project_id(canonical)
            key = (project_id, canonical)
            if key in seen:
                continue
            seen.add(key)
            records.append(ProjectRecord(project_id=key[0], root=canonical))
        return records, True

    if not isinstance(payload, dict):
        raise RegistryCorruptError(
            f"Invalid ProjectMem registry {source}: expected object or legacy list"
        )
    if payload.get("version") != REGISTRY_VERSION:
        raise RegistryCorruptError(
            f"Unsupported ProjectMem registry version in {source}: "
            f"{payload.get('version')!r}"
        )
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise RegistryCorruptError(
            f"Invalid ProjectMem registry {source}: projects must be a list"
        )
    records = [
        _validate_record_fields(item, source=source, index=index)
        for index, item in enumerate(raw_projects)
    ]
    return records, False


def _read_registry_unlocked() -> tuple[list[ProjectRecord], bool]:
    path = registry_path()
    if not path.exists():
        return [], False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryCorruptError(
            f"Invalid JSON in ProjectMem registry {path}"
        ) from exc
    except OSError as exc:
        raise RegistryIOError(
            f"Could not read ProjectMem registry {path}: {exc}"
        ) from exc
    return _parse_registry_payload(payload, path)


def _migrate_legacy_records(records: list[ProjectRecord]) -> list[ProjectRecord]:
    """Persist UUIDs for initialized projects known by a legacy registry."""
    migrated: list[ProjectRecord] = []
    for record in records:
        try:
            project_id = _ensure_project_id(record.root)
        except ProjectRegistryError:
            # Keep deleted/uninitialized entries visible to strict resolution;
            # it will raise the precise failure when that id is requested.
            project_id = record.project_id
        migrated.append(ProjectRecord(project_id=project_id, root=record.root))
    return migrated


def load_registry() -> list[ProjectRecord]:
    """Read strict records, migrating known legacy paths to persisted UUIDs."""
    with registry_lock():
        records, legacy = _read_registry_unlocked()
        if legacy:
            records = _migrate_legacy_records(records)
            _atomic_write_registry(records)
    return records


def registered_project_records() -> list[ProjectRecord]:
    """Public strict alias used by global MCP integrations."""
    return load_registry()


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a file atomically and fsync its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        raise RegistryIOError(f"Could not atomically write {path}: {exc}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _atomic_write_registry(records: list[ProjectRecord]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": REGISTRY_VERSION,
        "projects": [
            {"project_id": record.project_id, "root": str(record.root)}
            for record in records
        ],
    }
    _atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
    )


def _record_is_live(record: ProjectRecord) -> bool:
    """Return whether a record still names the same initialized identity."""
    try:
        canonical = canonicalize_project_root(record.root, require_initialized=True)
        return (
            canonical == record.root
            and _configured_project_id(canonical) == record.project_id
        )
    except ProjectRegistryError:
        return False


def register_project_record(root: Path | str) -> ProjectRecord:
    """Validate and atomically register one initialized project."""
    canonical = canonicalize_project_root(root, require_initialized=True)
    with registry_lock():
        record = ProjectRecord(project_id=_ensure_project_id(canonical), root=canonical)
        records, legacy = _read_registry_unlocked()
        if legacy:
            records = _migrate_legacy_records(records)
        matches = [item for item in records if item.project_id == record.project_id]
        conflicts = [
            item
            for item in matches
            if item.root != record.root and _record_is_live(item)
        ]
        if conflicts:
            raise AmbiguousProjectError(
                f"Project id {record.project_id} maps to multiple roots"
            )
        if matches:
            # A moved project carries its persisted UUID to the new root.  Its
            # old registry path is stale, so replace it rather than creating
            # an ambiguous identity.  A live conflicting path was rejected
            # above.
            records = [
                record if item.project_id == record.project_id else item
                for item in records
            ]
        else:
            records.append(record)
        # A registration is also the explicit migration point for the legacy
        # list, even when its target was already present.
        if legacy or not matches or any(item.root != record.root for item in matches):
            _atomic_write_registry(records)
    return record


def register_project(root: Path | str) -> ProjectRecord:
    """Compatibility/public registration entry point returning its record."""
    return register_project_record(root)


def resolve_project_record(project_id: str) -> ProjectRecord:
    """Resolve only a registered id and validate its root at request time."""
    if not isinstance(project_id, str) or not project_id:
        raise UnknownProjectError(f"Unknown ProjectMem project_id: {project_id!r}")
    with registry_lock():
        records, legacy = _read_registry_unlocked()
        if legacy:
            records = _migrate_legacy_records(records)
            _atomic_write_registry(records)
        matches = [item for item in records if item.project_id == project_id]
    if not matches:
        raise UnknownProjectError(f"Unknown ProjectMem project_id: {project_id}")
    if len(matches) != 1:
        raise AmbiguousProjectError(
            f"Project id {project_id} resolves ambiguously ({len(matches)} records)"
        )
    record = matches[0]
    canonical = canonicalize_project_root(record.root, require_initialized=True)
    if canonical != record.root:
        raise ProjectPathChangedError(
            f"Registered project {project_id} no longer resolves to {record.root}"
        )
    configured_id = _configured_project_id(canonical)
    if configured_id is None:
        raise ProjectNotInitializedError(
            f"Project configuration is missing generated project_id: "
            f"{canonical / MEM_DIR / CONFIG_FILE}"
        )
    if configured_id != record.project_id:
        raise ProjectPathChangedError(
            f"Registered project {project_id} does not match project identity at "
            f"{canonical}"
        )
    return record


def resolve_project_root(project_id: str) -> Path:
    """Resolve a registered id to a validated canonical project root."""
    return resolve_project_record(project_id).root


def resolve_project(project_id: str) -> Path:
    """Concise alias for :func:`resolve_project_root`."""
    return resolve_project_root(project_id)


def projectmem_global_lock() -> Iterator[Path]:
    """Return the shared lock context for registry/global-memory writes.

    Kept as a function (rather than another alias) for discoverability in MCP
    integrations: ``with projectmem_global_lock(): ...``.
    """
    return registry_lock()
