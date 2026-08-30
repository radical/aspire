from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Mapping

from .quarantine import select_quarantine_session_request
from .timeutils import parse_aware_iso8601


MAX_GRANT_LIFETIME = timedelta(hours=1)
PRODUCTION_REPOSITORY = "microsoft/aspire"
_GRANT_KEYS = frozenset(
    {
        "schemaVersion",
        "grantType",
        "grantId",
        "repository",
        "stateDirectory",
        "snapshotId",
        "quarantinePlanDigest",
        "allowedBatchId",
        "allowedTestNames",
        "issuedAt",
        "expiresAt",
    }
)


@dataclass(frozen=True, slots=True)
class AuthorizedQuarantineStart:
    request: dict[str, object]
    grant_id: str


def create_quarantine_grant(
    *,
    request_path: Path,
    state_dir: Path,
    batch_id: str | None,
    issued_at: datetime,
    lifetime: timedelta = timedelta(minutes=15),
    test_name: str | None = None,
) -> dict[str, object]:
    request_bytes, document = _read_request(request_path)
    request = _select_request(document)
    if test_name is not None:
        request = select_quarantine_session_request(request, test_name)
    if batch_id is None:
        batch_id = _require_string(request, "batchId")
    repository, snapshot_id, test_names = _validate_request(request, batch_id)
    _deny_production(repository)
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ValueError("issuedAt must include a UTC offset.")
    if lifetime <= timedelta(0) or lifetime > MAX_GRANT_LIFETIME:
        raise ValueError("Quarantine grant lifetime must be between zero and one hour.")
    issued_at = issued_at.astimezone(timezone.utc)
    return {
        "schemaVersion": 1,
        "grantType": "quarantine-start",
        "grantId": f"quarantine-grant:{secrets.token_hex(16)}",
        "repository": repository,
        "stateDirectory": _canonical_state_directory(state_dir),
        "snapshotId": snapshot_id,
        "quarantinePlanDigest": hashlib.sha256(request_bytes).hexdigest(),
        "allowedBatchId": batch_id,
        "allowedTestNames": test_names,
        "issuedAt": issued_at.isoformat().replace("+00:00", "Z"),
        "expiresAt": (issued_at + lifetime).isoformat().replace("+00:00", "Z"),
    }


def write_quarantine_grant(path: Path, grant: Mapping[str, object]) -> None:
    payload = (json.dumps(grant, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.is_symlink():
        raise ValueError("Quarantine authorization output must not be a symlink.")
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def authorize_quarantine_start(
    *,
    request_path: Path,
    authorization_path: Path,
    state_dir: Path,
    batch_id: str,
    now: datetime,
    test_name: str | None = None,
) -> AuthorizedQuarantineStart:
    request_bytes, document = _read_request(request_path)
    request = _select_request(document)
    if test_name is not None:
        request = select_quarantine_session_request(request, test_name)
    repository, snapshot_id, test_names = _validate_request(request, batch_id)
    _deny_production(repository)
    grant = _read_json_object(authorization_path, "quarantine authorization")
    if frozenset(grant) != _GRANT_KEYS:
        raise ValueError("Quarantine authorization has unexpected or missing fields.")
    if grant.get("schemaVersion") != 1 or grant.get("grantType") != "quarantine-start":
        raise ValueError("Unsupported quarantine authorization.")
    grant_id = _require_string(grant, "grantId")
    checks = {
        "repository": repository,
        "stateDirectory": _canonical_state_directory(state_dir),
        "snapshotId": snapshot_id,
        "quarantinePlanDigest": hashlib.sha256(request_bytes).hexdigest(),
        "allowedBatchId": batch_id,
        "allowedTestNames": test_names,
    }
    for field, expected in checks.items():
        if grant.get(field) != expected:
            description = (
                "plan digest" if field == "quarantinePlanDigest" else field
            )
            raise ValueError(
                f"Quarantine authorization {description} does not match."
            )
    issued_at = parse_aware_iso8601(grant.get("issuedAt"), "issuedAt")
    expires_at = parse_aware_iso8601(grant.get("expiresAt"), "expiresAt")
    if expires_at <= issued_at or expires_at - issued_at > MAX_GRANT_LIFETIME:
        raise ValueError("Quarantine authorization has an invalid lifetime.")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must include a UTC offset.")
    if now < issued_at or now >= expires_at:
        raise ValueError("Quarantine authorization is not currently valid.")
    return AuthorizedQuarantineStart(request=request, grant_id=grant_id)


def _read_request(path: Path) -> tuple[bytes, dict[str, object]]:
    payload = _read_regular_file(path, "quarantine request")
    return payload, _load_json_object(payload, "quarantine request")


def _read_json_object(path: Path, description: str) -> dict[str, object]:
    return _load_json_object(_read_regular_file(path, description), description)


def _read_regular_file(path: Path, description: str) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{description} must not be a symlink.")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise ValueError(f"{description} does not exist: {path}") from error
    if not stat.S_ISREG(mode):
        raise ValueError(f"{description} must be a regular file.")
    return path.read_bytes()


def _load_json_object(payload: bytes, description: str) -> dict[str, object]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{description} contains duplicate key {key!r}.")
            result[key] = value
        return result

    try:
        document = json.loads(payload, object_pairs_hook=reject_duplicate)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} must contain valid UTF-8 JSON.") from error
    if not isinstance(document, dict):
        raise ValueError(f"{description} must contain a JSON object.")
    return document


def _validate_request(
    request: Mapping[str, object],
    batch_id: str,
) -> tuple[str, str, list[str]]:
    if request.get("schemaVersion") != 1:
        raise ValueError("Unsupported quarantine request.")
    repository = _require_string(request, "repository")
    snapshot_id = _require_string(request, "snapshotId")
    if request.get("batchId") != batch_id:
        raise ValueError("Quarantine request batchId does not match.")
    tests = request.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("Quarantine request tests must be nonempty.")
    test_names: list[str] = []
    for item in tests:
        if not isinstance(item, dict):
            raise ValueError("Quarantine request tests must contain objects.")
        test_names.append(_require_string(item, "testName"))
    if len(test_names) != len(set(test_names)):
        raise ValueError("Quarantine request test names must be unique.")
    return repository, snapshot_id, sorted(test_names)


def _select_request(
    document: dict[str, object],
) -> dict[str, object]:
    proposal = document.get("proposal")
    if isinstance(proposal, dict):
        return proposal
    return document


def _require_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be nonempty.")
    return value


def _canonical_state_directory(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("State directory must not be a symlink.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(path.resolve(strict=True))


def _deny_production(repository: str) -> None:
    if repository.casefold() == PRODUCTION_REPOSITORY:
        raise ValueError(
            "Quarantine execution is forbidden for microsoft/aspire; "
            "use an explicitly authorized fork."
        )
