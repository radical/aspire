"""Coordinator-owned Git worktrees; workers never write this durable registry."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows
from .ownership import normalize_github_repository_url
from .timeutils import parse_aware_iso8601


_TERMINAL = frozenset({"completed", "failed", "abandoned"})
_STATES = frozenset({
    "provisioning", "ready", "bound", "terminal", "cleanup-pending", "cleaned",
    "provisioning-failed", "blocked",
})
_IMMUTABLE = (
    "schemaVersion", "ownershipId", "repository", "investigationId", "attempt",
    "request", "requestFingerprint", "sourceRevision", "commonGitDirectory",
    "commonGitIdentity", "managedRoot", "checkoutPath", "stateDirectory",
)
_FIELDS = frozenset((*_IMMUTABLE,
    "recordedAt", "state", "sessionId", "terminalStatus", "workerStopped", "error",
    "gitDirectory", "gitDirectoryIdentity", "checkoutIdentity",
))


def default_worktree_root() -> Path:
    return Path.home() / ".copilot" / "ci-shepherd" / "worktrees"


def _fingerprint(value: object) -> str:
    # This is a compact inventory label, not an authorization digest. Every
    # operation also compares the complete frozen request and repository identity.
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    result = 0xCBF29CE484222325
    for byte in encoded:
        result = ((result ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{result:016x}"


def _safe_component(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")[:60] or "item"
    return f"{slug}-{_fingerprint(value).split(':')[1]}"


def _safe_path(path: Path, *, exists: bool = False) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise ValueError(f"Managed path must not contain parent traversal: {path}")
    absolute = expanded.absolute()
    for part in (absolute, *absolute.parents):
        if part.is_symlink():
            raise ValueError(f"Managed path must not contain a symlink: {part}")
    try:
        canonical = absolute.resolve(strict=exists)
    except FileNotFoundError as error:
        raise ValueError(f"Managed path does not exist: {path}") from error
    if canonical != absolute:
        raise ValueError(f"Managed path must be canonical: {path}")
    return canonical


def _identity(path: Path) -> dict[str, int]:
    status = path.stat()
    return {"device": status.st_dev, "inode": status.st_ino}


def _private_directory(path: Path) -> None:
    _safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"Managed directory is not a directory: {path}")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ValueError(f"Managed directory belongs to another user: {path}")
    path.chmod(0o700)


def _ledger(state_directory: Path) -> Path:
    return _safe_path(state_directory) / "ledgers" / "investigation-worktrees.jsonl"


def _git(directory: Path, *arguments: str, common: bool = False) -> str:
    location = ["--git-dir", str(directory)] if common else ["-C", str(directory)]
    # A caller's GIT_DIR/WORK_TREE/INDEX_FILE must not redirect identity checks
    # or worktree mutations. Worktree checkout must not execute repository hooks.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git", "--no-pager", *location, "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.fsmonitor=false",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False,
            timeout=60, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Git {arguments[0]} could not complete: {error}") from error
    if result.returncode:
        raise ValueError(f"Git {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _request(request: Mapping[str, Any]) -> dict[str, Any]:
    frozen = copy.deepcopy(dict(request))
    repository = frozen.get("repository")
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or any(part in {".", ".."} for part in repository.split("/"))
    ):
        raise ValueError("Investigation request requires an owner/repository identity.")
    investigation_id = frozen.get("investigationId")
    if not isinstance(investigation_id, str) or not investigation_id.strip():
        raise ValueError("Investigation request requires an investigationId.")
    revision = frozen.get("sourceRevision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Investigation request requires an exact frozen source revision.")
    scope = frozen.get("investigationScope")
    if scope is not None and (
        not isinstance(scope, Mapping) or scope.get("sourceRevision") != revision
    ):
        raise ValueError("Investigation request source revision disagrees with its scope.")
    _fingerprint(frozen)
    return frozen


def _registered(common_directory: Path) -> list[dict[str, str]]:
    text = _git(common_directory, "worktree", "list", "--porcelain", "-z", common=True)
    # Git's stable -z porcelain format is:
    #   worktree /path\0HEAD <sha>\0detached\0locked <reason>\0\0
    # Paths/reasons can contain newlines; only NUL separates attributes/records.
    # https://git-scm.com/docs/git-worktree#_porcelain_format
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in text.split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        name, _, value = field.partition(" ")
        if name in current:
            raise ValueError("Git worktree registration has duplicate attributes.")
        current[name] = value
    if current:
        raise ValueError("Git worktree registration is incomplete.")
    return records


def _repository(source: Path, repository: str) -> Path:
    if _git(source, "rev-parse", "--show-toplevel").strip() != str(source):
        raise ValueError("Source checkout must be the repository worktree root.")
    matches = []
    for remote in _git(source, "remote").splitlines():
        urls = _git(source, "remote", "get-url", "--all", remote).splitlines()
        matches.extend(
            url for url in urls
            if (normalize_github_repository_url(url) or "").casefold() == repository.casefold()
        )
    if not matches:
        raise ValueError("Source checkout does not match the requested GitHub repository.")
    common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return _safe_path(common, exists=True)


def _expected_path(record: Mapping[str, Any]) -> Path:
    return (
        Path(record["managedRoot"]) / _safe_component(record["repository"].casefold())
        / _safe_component(record["investigationId"]) / str(record["attempt"])
    )


def _read_registry(path: Path) -> list[dict[str, Any]]:
    _safe_path(path)
    latest: dict[str, dict[str, Any]] = {}
    allocations: dict[tuple[str, str, int], str] = {}
    paths: dict[str, str] = {}
    for row in read_jsonl_rows(path):
        if (
            set(row) != _FIELDS or type(row["schemaVersion"]) is not int or row["schemaVersion"] != 1
            or not isinstance(row["state"], str) or row["state"] not in _STATES
            or not isinstance(row["ownershipId"], str)
            or re.fullmatch(r"[0-9a-f]{32}", row["ownershipId"]) is None
            or type(row["attempt"]) is not int or row["attempt"] < 1
            or not isinstance(row["request"], dict)
            or any(
                not isinstance(row[key], str) or not row[key]
                for key in ("managedRoot", "checkoutPath", "commonGitDirectory", "stateDirectory")
            )
        ):
            raise ValueError("Malformed investigation worktree registry row.")
        _validate_lifecycle_metadata(row)
        if row["stateDirectory"] != str(path.parent.parent):
            raise ValueError("Investigation worktree belongs to another state directory.")
        frozen = _request(row["request"])
        if (
            row["requestFingerprint"] != _fingerprint(frozen)
            or any(row[key] != frozen[key] for key in ("repository", "investigationId", "sourceRevision"))
            or str(_expected_path(row)) != row["checkoutPath"]
        ):
            raise ValueError("Investigation worktree registry identity/request mismatch.")
        parse_aware_iso8601(row.get("recordedAt"), "recordedAt")
        owner = row["ownershipId"]
        previous = latest.get(owner)
        if previous is None and row["state"] != "provisioning":
            raise ValueError("Investigation worktree registry lacks a provisioning intent.")
        if previous is not None and any(previous[key] != row[key] for key in _IMMUTABLE):
            raise ValueError("Investigation worktree registry changed immutable ownership.")
        if previous is not None and (
            (previous["sessionId"] is not None and previous["sessionId"] != row["sessionId"])
            or (previous["terminalStatus"] is not None and previous["terminalStatus"] != row["terminalStatus"])
            or (previous["workerStopped"] and not row["workerStopped"])
            or (previous["state"] == "cleaned" and row["state"] != "cleaned")
            or (previous["gitDirectory"] is not None and any(
                previous[key] != row[key]
                for key in ("gitDirectory", "gitDirectoryIdentity", "checkoutIdentity")
            ))
        ):
            raise ValueError("Investigation worktree registry changed established lifecycle identity.")
        key = (row["repository"].casefold(), row["investigationId"], row["attempt"])
        if allocations.get(key, owner) != owner or paths.get(row["checkoutPath"], owner) != owner:
            raise ValueError("Investigation worktree registry has conflicting allocations.")
        allocations[key] = owner
        paths[row["checkoutPath"]] = owner
        latest[owner] = row
    return list(latest.values())


def _validate_lifecycle_metadata(row: Mapping[str, Any]) -> None:
    if (
        type(row["workerStopped"]) is not bool
        or (row["terminalStatus"] is not None and (
            not isinstance(row["terminalStatus"], str) or row["terminalStatus"] not in _TERMINAL
        ))
        or (row["sessionId"] is not None and (
            not isinstance(row["sessionId"], str) or not row["sessionId"].strip()
        ))
        or (row["error"] is not None and not isinstance(row["error"], str))
        or (row["state"] == "bound" and row["sessionId"] is None)
        or (row["state"] in {"terminal", "cleanup-pending", "cleaned"} and row["terminalStatus"] is None)
        or (row["state"] in {"cleanup-pending", "cleaned"} and not row["workerStopped"])
        or (row["state"] in {"provisioning", "ready", "bound"} and row["terminalStatus"] is not None)
        or (row["workerStopped"] and row["terminalStatus"] is None)
    ):
        raise ValueError("Malformed investigation worktree registry lifecycle metadata.")
    for key in ("commonGitIdentity", "gitDirectoryIdentity", "checkoutIdentity"):
        identity = row[key]
        if identity is None and key != "commonGitIdentity" and row["gitDirectory"] is None:
            continue
        if (
            not isinstance(identity, dict) or set(identity) != {"device", "inode"}
            or any(type(value) is not int or value < 0 for value in identity.values())
        ):
            raise ValueError("Malformed investigation worktree registry filesystem identity.")
    if row["gitDirectory"] is not None and not isinstance(row["gitDirectory"], str):
        raise ValueError("Malformed investigation worktree registry Git directory.")
    if row["state"] in {"ready", "bound", "cleanup-pending", "cleaned"} and row["gitDirectory"] is None:
        raise ValueError("Investigation worktree registry lacks verified filesystem identity.")


def list_investigation_worktrees(state_directory: Path) -> list[dict[str, Any]]:
    """Read durable inventory, including failed, terminal and removed attempts."""
    return copy.deepcopy(_read_registry(_ledger(state_directory)))


def _append(path: Path, record: dict[str, Any], **updates: Any) -> dict[str, Any]:
    updated = {**record, **updates}
    parse_aware_iso8601(updated["recordedAt"], "recordedAt")
    append_jsonl_rows(path, [updated])
    return updated


def _check_layout(record: Mapping[str, Any], state_directory: Path) -> tuple[Path, Path]:
    root = _safe_path(Path(record["managedRoot"]))
    checkout = _safe_path(Path(record["checkoutPath"]))
    common = _safe_path(Path(record["commonGitDirectory"]), exists=True)
    state = _safe_path(state_directory)
    if (
        root == Path(root.anchor) or checkout != _expected_path(record)
        or not checkout.is_relative_to(root)
        or state.is_relative_to(root) or root.is_relative_to(state)
        or common.is_relative_to(root) or root.is_relative_to(common)
    ):
        raise ValueError("Registry, common Git directory and managed worktree root must be separate.")
    if _identity(common) != record["commonGitIdentity"]:
        raise ValueError("Investigation common Git identity changed.")
    return checkout, common


def _verify(record: Mapping[str, Any], state_directory: Path, *, clean: bool = True) -> None:
    checkout, common = _check_layout(record, state_directory)
    matches = [row for row in _registered(common) if row.get("worktree") == str(checkout)]
    if len(matches) != 1:
        raise ValueError("Owned checkout has no exact Git worktree registration.")
    registration = matches[0]
    if (
        registration.get("HEAD") != record["sourceRevision"]
        or "detached" not in registration or "prunable" in registration
    ):
        raise ValueError("Owned worktree HEAD/revision or detached registration changed.")
    reason = f"ci-shepherd:{record['ownershipId']}"
    if registration.get("locked") != reason and not (
        record["state"] == "cleanup-pending" and "locked" not in registration
    ):
        raise ValueError("Git worktree ownership lock does not match the registry.")
    _safe_path(checkout, exists=True)
    if not checkout.is_dir() or (checkout / ".git").is_symlink() or not (checkout / ".git").is_file():
        raise ValueError("Owned worktree must have a regular .git link file.")
    actual_common = _safe_path(Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()), exists=True)
    git_directory = _safe_path(Path(_git(checkout, "rev-parse", "--absolute-git-dir").strip()), exists=True)
    if actual_common != common or git_directory.parent != common / "worktrees":
        raise ValueError("Owned worktree common Git identity does not match.")
    if record.get("gitDirectory") is not None and (
        record["gitDirectory"] != str(git_directory)
        or record["gitDirectoryIdentity"] != _identity(git_directory)
        or record["checkoutIdentity"] != _identity(checkout)
    ):
        raise ValueError("Owned worktree filesystem/Git identity changed.")
    if _git(checkout, "rev-parse", "HEAD").strip() != record["sourceRevision"]:
        raise ValueError("Owned worktree HEAD does not match the frozen revision.")
    if clean:
        # Normal status deliberately trusts assume-unchanged/skip-worktree bits.
        # Reject those bits rather than clearing a worker's index to inspect it.
        # ls-files -v emits "H path\0" for ordinary entries, "S" for skipped
        # entries, and lower-case tags for assume-unchanged entries.
        # https://git-scm.com/docs/git-ls-files#Documentation/git-ls-files.txt--v
        entries = _git(checkout, "ls-files", "-v", "-z").split("\0")
        if any(entry and not entry.startswith("H ") for entry in entries):
            raise ValueError("Owned investigation worktree index can hide source changes.")
        if _git(
            checkout, "status", "--porcelain", "-z", "--untracked-files=all",
            "--ignored=matching", "--ignore-submodules=none",
        ):
            raise ValueError("Owned investigation worktree is not clean (including ignored files).")


def provision_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    source_checkout: Path,
    attempt: int,
    recorded_at: str,
    managed_root: Path | None = None,
) -> dict[str, Any]:
    """Allocate exactly one detached source checkout for this frozen attempt."""
    frozen = _request(request)
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Investigation attempt must be a positive integer.")
    parse_aware_iso8601(recorded_at, "recordedAt")
    source = _safe_path(source_checkout, exists=True)
    common = _repository(source, frozen["repository"])
    revision = _git(common, "rev-parse", "--verify", f"{frozen['sourceRevision']}^{{commit}}", common=True).strip()
    if revision != frozen["sourceRevision"]:
        raise ValueError("Requested source revision is not an exact commit.")
    root = _safe_path(managed_root if managed_root is not None else default_worktree_root())
    state = _safe_path(state_directory)
    for registration in _registered(common):
        existing = Path(registration["worktree"])
        if root.is_relative_to(existing) or existing.is_relative_to(root) or state.is_relative_to(existing):
            # Existing owned workers below the same root are checked individually
            # by the registry below; the coordinator must remain outside that root.
            if existing != source and existing.is_relative_to(root) and not state.is_relative_to(existing):
                continue
            raise ValueError("Managed root and durable registry must be outside source worktrees.")
    record = {
        "schemaVersion": 1, "ownershipId": secrets.token_hex(16),
        "repository": frozen["repository"], "investigationId": frozen["investigationId"],
        "attempt": attempt, "request": frozen, "requestFingerprint": _fingerprint(frozen),
        "sourceRevision": revision, "commonGitDirectory": str(common),
        "commonGitIdentity": _identity(common), "managedRoot": str(root),
        "stateDirectory": str(state),
        "recordedAt": recorded_at, "state": "provisioning", "sessionId": None,
        "terminalStatus": None, "workerStopped": False, "error": None,
        "gitDirectory": None, "gitDirectoryIdentity": None, "checkoutIdentity": None,
    }
    record["checkoutPath"] = str(_expected_path(record))
    checkout, _ = _check_layout(record, state)
    path = _ledger(state)
    _safe_path(path)
    with exclusive_jsonl_lock(path):
        records = _read_registry(path)
        same = [
            row for row in records
            if row["repository"].casefold() == frozen["repository"].casefold()
            and row["investigationId"] == frozen["investigationId"]
        ]
        previous = next((row for row in same if row["attempt"] == attempt), None)
        if previous is not None:
            if previous["request"] != frozen:
                raise ValueError("This investigation attempt belongs to a different frozen request.")
            if any(previous[key] != record[key] for key in ("commonGitDirectory", "commonGitIdentity", "managedRoot")):
                raise ValueError("This investigation attempt belongs to another Git repository/root.")
            if previous["state"] not in {"ready", "bound"}:
                raise ValueError(f"Attempt is {previous['state']}; reconcile it explicitly rather than reprovisioning.")
            _verify(previous, state)
            return previous
        if same and (
            attempt != max(row["attempt"] for row in same) + 1
            or any(row["state"] not in {"terminal", "cleaned"} or not row["workerStopped"] for row in same)
        ):
            raise ValueError("A replacement requires the preceding terminal, explicitly stopped attempt.")
        if not same and attempt != 1:
            raise ValueError("The first investigation attempt must be 1.")
        if checkout.exists() or any(row.get("worktree") == str(checkout) for row in _registered(common)):
            raise ValueError("An unowned path or Git registration already occupies this allocation.")
        _private_directory(root)
        _private_directory(checkout.parent.parent)
        _private_directory(checkout.parent)
        record = _append(path, record)
        try:
            # --lock attaches our random ownership reason during creation, so a
            # surviving intent can prove ownership after a crash before "ready".
            # https://git-scm.com/docs/git-worktree#Documentation/git-worktree.txt---lock
            _git(common, "worktree", "add", "--detach", "--lock", "--reason",
                 f"ci-shepherd:{record['ownershipId']}", str(checkout), revision, common=True)
            _verify(record, state)
        except ValueError as error:
            _append(path, record, state="provisioning-failed", error=str(error))
            raise
        checkout.chmod(0o700)
        git_directory = Path(_git(checkout, "rev-parse", "--absolute-git-dir").strip())
        return _append(
            path, record, state="ready", gitDirectory=str(git_directory),
            gitDirectoryIdentity=_identity(git_directory), checkoutIdentity=_identity(checkout),
        )


def _find(
    state_directory: Path, request: Mapping[str, Any], checkout: Path,
) -> dict[str, Any]:
    frozen = _request(request)
    # Looking up a durable binding must not touch the disposable source tree:
    # failure recording and result replay also work after deletion or corruption.
    # Live verification and every Git mutation separately call _check_layout.
    if ".." in checkout.parts:
        raise ValueError("Owned checkout path must not contain parent traversal.")
    canonical = checkout.expanduser().absolute()
    matches = [
        record for record in _read_registry(_ledger(state_directory))
        if record["checkoutPath"] == str(canonical)
    ]
    if len(matches) != 1:
        raise ValueError("Checkout is not an owned investigation worktree in this registry.")
    record = matches[0]
    if record["request"] != frozen or record["requestFingerprint"] != _fingerprint(frozen):
        raise ValueError("Owned worktree does not match the exact frozen request.")
    return record


def get_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Read a durable binding without verifying disposable source or cleanup safety."""
    record = _find(state_directory, request, checkout)
    if session_id is not None:
        _session(record, session_id)
    return record


def _session(record: Mapping[str, Any], session_id: str | None) -> None:
    if record["sessionId"] != session_id:
        raise ValueError("Owned worktree belongs to another session.")


def validate_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Verify registry ownership, Git identity, frozen HEAD and full cleanliness."""
    record = _find(state_directory, request, checkout)
    if session_id is not None:
        _session(record, session_id)
    if record["state"] not in {"ready", "bound", "terminal"}:
        raise ValueError(f"Worktree is {record['state']}, not a validated allocation.")
    _verify(record, state_directory)
    return record


def bind_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    session_id: str,
    recorded_at: str,
) -> dict[str, Any]:
    """Bind one idle worker before dispatch; never share an allocation/session."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("A nonempty worker sessionId is required.")
    parse_aware_iso8601(recorded_at, "recordedAt")
    path = _ledger(state_directory)
    _safe_path(path)
    with exclusive_jsonl_lock(path):
        record = _find(state_directory, request, checkout)
        if record["sessionId"] is not None:
            _session(record, session_id)
        if record["state"] not in {"ready", "bound"}:
            raise ValueError("Only a ready allocation can bind a worker session.")
        if any(
            other["sessionId"] == session_id and other["ownershipId"] != record["ownershipId"]
            for other in _read_registry(path)
        ):
            raise ValueError("Worker session already belongs to another investigation worktree.")
        _verify(record, state_directory)
        if record["state"] == "bound":
            return record
        return _append(path, record, state="bound", sessionId=session_id, recordedAt=recorded_at)


def finish_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    session_id: str | None,
    status: str,
    recorded_at: str,
    confirm_worker_stopped: bool = False,
) -> dict[str, Any]:
    """Mirror a durable terminal lifecycle event; this never removes source."""
    if status not in _TERMINAL:
        raise ValueError("Worktree terminal status must be completed, failed or abandoned.")
    if type(confirm_worker_stopped) is not bool:
        raise ValueError("Stopped-worker confirmation must be an explicit boolean.")
    parse_aware_iso8601(recorded_at, "recordedAt")
    path = _ledger(state_directory)
    _safe_path(path)
    with exclusive_jsonl_lock(path):
        record = _find(state_directory, request, checkout)
        _session(record, session_id)
        if record["terminalStatus"] is not None:
            if record["terminalStatus"] != status:
                raise ValueError("Worktree already has another terminal outcome.")
            if record["workerStopped"] or not confirm_worker_stopped:
                return record
        elif record["state"] not in {"ready", "bound", "provisioning-failed", "blocked"}:
            raise ValueError("Worktree is not ready for a terminal lifecycle event.")
        if status == "completed":
            if record["sessionId"] is None:
                raise ValueError("Completion requires a bound worker session.")
            _verify(record, state_directory)
        # Failed workers can leave dirty or changed source. Preserve their
        # terminal outcome without implying that their checkout is removable.
        return _append(
            path, record, state="terminal", terminalStatus=status,
            workerStopped=confirm_worker_stopped or record["workerStopped"],
            recordedAt=recorded_at,
        )


def _is_removed(record: Mapping[str, Any], state_directory: Path) -> bool:
    checkout, common = _check_layout(record, state_directory)
    return not checkout.exists() and not any(
        row.get("worktree") == str(checkout) for row in _registered(common)
    )


def cleanup_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    session_id: str | None = None,
    recorded_at: str,
    confirm_worker_stopped: bool = False,
) -> dict[str, Any]:
    """Remove only an owned, stopped, terminal and clean registered checkout."""
    parse_aware_iso8601(recorded_at, "recordedAt")
    path = _ledger(state_directory)
    _safe_path(path)
    with exclusive_jsonl_lock(path):
        record = _find(state_directory, request, checkout)
        _session(record, session_id)
        if record["state"] not in {"terminal", "cleanup-pending", "cleaned"}:
            raise ValueError("Cleanup requires a terminal investigation worktree.")
        if confirm_worker_stopped is not True:
            raise ValueError("Cleanup requires explicit confirmation that the worker is stopped.")
        if record["state"] == "cleaned":
            if not _is_removed(record, state_directory):
                raise ValueError("A cleaned allocation path/registration has reappeared; refusing removal.")
            return record
        if record["state"] == "cleanup-pending" and _is_removed(record, state_directory):
            return _append(path, record, state="cleaned", error=None, recordedAt=recorded_at)
        _verify(record, state_directory)
        if record["state"] != "cleanup-pending":
            record = _append(
                path, record, state="cleanup-pending", workerStopped=True, recordedAt=recorded_at,
            )
        common = Path(record["commonGitDirectory"])
        target = Path(record["checkoutPath"])
        try:
            registration = next(row for row in _registered(common) if row.get("worktree") == str(target))
            if "locked" in registration:
                _git(common, "worktree", "unlock", str(target), common=True)
            # Recheck after unlocking; a stopped worker is an operator assertion,
            # not permission to force through new writes or identity drift.
            _verify(record, state_directory)
            _git(common, "worktree", "remove", str(target), common=True)
            if not _is_removed(record, state_directory):
                raise ValueError("Git did not fully remove the exact owned worktree.")
        except ValueError as error:
            _append(path, record, error=str(error))
            raise
        return _append(path, record, state="cleaned", error=None, recordedAt=recorded_at)


def reconcile_investigation_worktree(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    checkout: Path,
    recorded_at: str,
) -> dict[str, Any]:
    """Reconcile interrupted intents using Git ownership, never a path name."""
    parse_aware_iso8601(recorded_at, "recordedAt")
    path = _ledger(state_directory)
    _safe_path(path)
    with exclusive_jsonl_lock(path):
        record = _find(state_directory, request, checkout)
        try:
            if record["state"] in {"cleanup-pending", "cleaned"} and _is_removed(record, state_directory):
                if record["state"] == "cleaned":
                    return record
                return _append(path, record, state="cleaned", error=None, recordedAt=recorded_at)
            if record["state"] == "cleaned":
                raise ValueError("A cleaned allocation has reappeared; manual inspection is required.")
            _verify(record, state_directory)
        except ValueError as error:
            # Retain cleanup intent across an interrupted unlock/remove. Its
            # captured filesystem identities can still validate the unlocked
            # registration; no other state is allowed that exception.
            state = record["state"] if record["state"] in {"cleanup-pending", "cleaned"} else "blocked"
            if record["state"] == state and record["error"] == str(error):
                return record
            return _append(path, record, state=state, error=str(error), recordedAt=recorded_at)
        if record["state"] in {"ready", "bound", "terminal", "cleanup-pending"}:
            if record["error"] is None:
                return record
            return _append(path, record, error=None, recordedAt=recorded_at)
        target = Path(record["checkoutPath"])
        git_directory = Path(_git(target, "rev-parse", "--absolute-git-dir").strip())
        target.chmod(0o700)
        state = "terminal" if record["terminalStatus"] else "bound" if record["sessionId"] else "ready"
        return _append(
            path, record, state=state, error=None, recordedAt=recorded_at,
            gitDirectory=str(git_directory), gitDirectoryIdentity=_identity(git_directory),
            checkoutIdentity=_identity(target),
        )
