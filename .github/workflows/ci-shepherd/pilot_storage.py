"""Full-ledger branch authority and explicitly bounded host-local artifacts."""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import branch_memory as memory
import pilot_state as state
import round as contracts

MAX_POINTER = 4096
MAX_ENVELOPE = 524288
HEADER = "[automated] CI Shepherd repository authority. Do not edit.\n" + state.MARKER + "\n"
AUTHORITY_KEYS = {"controllerRepository", "controllerRepositoryId", "trackerId", "trackerNode", "commentId",
                  "actorId", "actorLogin", "binding", "targetRepository", "targetRepositoryId"}
DOCUMENT_KEYS = {"storageVersion", "authority", "migrationDigest", "generation", "ledgerDigest", "ledger"}
POINTER_KEYS = {"storageVersion", "authority", "branch", "path", "activationCommit", "activationBlob",
                "migrationDigest", "initialLedgerDigest"}


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid memory integrity digest")


def identity(api):
    return {"controllerRepository": api.controller_repository, "controllerRepositoryId": api.controller_repository_id,
            "trackerId": api.tracker, "trackerNode": api.tracker_node, "commentId": api.authority_id,
            "actorId": api.actor["id"], "actorLogin": api.actor["login"], "binding": api.binding.name,
            "targetRepository": api.repository, "targetRepositoryId": api.repository_id}


def path_for(authority):
    return (f"authorities/{authority['controllerRepositoryId']}/"
            f"{authority['trackerId']}-{authority['commentId']}-{authority['binding']}.json")


def validate_identity(value):
    contracts.exact(value, AUTHORITY_KEYS, "memory authority identity")
    for key in ("controllerRepositoryId", "targetRepositoryId", "trackerId", "commentId", "actorId"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError("invalid memory authority numeric identity")
    for key in ("controllerRepository", "targetRepository"):
        if not isinstance(value[key], str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value[key]) is None:
            raise ValueError("invalid memory repository identity")
    for key in ("actorLogin", "trackerNode", "binding"):
        if not isinstance(value[key], str) or not value[key] or len(value[key].encode()) > 200:
            raise ValueError("invalid memory authority identity")


def document(authority, ledger, migration, generation):
    validate_identity(authority)
    validate_digest(migration)
    if type(generation) is not int or generation < 0:
        raise ValueError("invalid memory generation")
    state.validate(ledger)
    if type(ledger["schemaVersion"]) is not int:
        raise ValueError("invalid memory ledger version")
    value = {"storageVersion": 1, "authority": authority, "migrationDigest": migration,
             "generation": generation, "ledgerDigest": digest(canonical(ledger)), "ledger": ledger}
    raw = canonical(value)
    if len(raw) > memory.MAX_BYTES:
        raise ValueError("memory authority application bound exhausted")
    return raw


def parse_document(raw, authority, migration):
    if not isinstance(raw, bytes):
        raise ValueError("missing memory authority")
    value = contracts.loads(raw.decode("utf-8"), max_bytes=memory.MAX_BYTES)
    contracts.exact(value, DOCUMENT_KEYS, "memory document")
    validate_identity(value["authority"])
    if (type(value["storageVersion"]) is not int or value["storageVersion"] != 1
            or value["authority"] != authority or value["migrationDigest"] != migration
            or type(value["generation"]) is not int or value["generation"] < 0):
        raise ValueError("memory identity/epoch/version mismatch")
    validate_digest(value["migrationDigest"])
    validate_digest(value["ledgerDigest"])
    state.validate(value["ledger"])
    if type(value["ledger"]["schemaVersion"]) is not int:
        raise ValueError("invalid memory ledger version")
    if value["ledger"]["repository"] != authority["targetRepository"]:
        raise ValueError("memory target mismatch")
    if value["ledgerDigest"] != digest(canonical(value["ledger"])) or canonical(value) != raw:
        raise ValueError("noncanonical or corrupt memory document")
    return value


def pointer(authority, snapshot, migration, ledger):
    value = {"storageVersion": 1, "authority": authority, "branch": memory.REF, "path": path_for(authority),
             "activationCommit": snapshot.commit, "activationBlob": snapshot.blob,
             "migrationDigest": migration, "initialLedgerDigest": digest(canonical(ledger))}
    body = HEADER + canonical(value).decode()
    if len(body.encode()) > MAX_POINTER:
        raise ValueError("memory pointer bound exhausted")
    return body


def parse_pointer(body, authority):
    if not isinstance(body, str) or not body.startswith(HEADER) or len(body.encode()) > MAX_POINTER:
        raise ValueError("invalid memory pointer")
    value = contracts.loads(body[len(HEADER):], max_bytes=MAX_POINTER)
    contracts.exact(value, POINTER_KEYS, "memory pointer")
    validate_identity(value["authority"])
    if (type(value["storageVersion"]) is not int or value["storageVersion"] != 1
            or value["authority"] != authority or value["branch"] != memory.REF
            or value["path"] != path_for(authority) or HEADER + canonical(value).decode() != body):
        raise ValueError("memory pointer identity/path/version mismatch")
    memory.oid(value["activationCommit"])
    memory.oid(value["activationBlob"])
    validate_digest(value["migrationDigest"])
    validate_digest(value["initialLedgerDigest"])
    return value


@dataclass(frozen=True)
class Revision:
    pointer: str
    snapshot: memory.MemorySnapshot
    generation: int


def is_pointer(body):
    if not isinstance(body, str) or not body.startswith(HEADER):
        return False
    value = contracts.loads(body[len(HEADER):], max_bytes=state.MAX_BODY)
    return isinstance(value, dict) and "storageVersion" in value


def store(api, *, write=False):
    authority = identity(api)
    return memory.BranchMemory(api.transport, api.controller_repository, api.controller_repository_id,
                               path_for(authority), write=write)


def read(api, body):
    authority = identity(api)
    value = parse_pointer(body, authority)
    branch = store(api)
    initial = branch.at(value["activationCommit"])
    if initial.blob != value["activationBlob"]:
        raise ValueError("memory activation blob changed")
    original = parse_document(initial.document, authority, value["migrationDigest"])
    if original["generation"] != 0 or original["ledgerDigest"] != value["initialLedgerDigest"]:
        raise ValueError("invalid memory activation epoch")
    snapshot = branch.read()
    current = parse_document(snapshot.document, authority, value["migrationDigest"])
    return current["ledger"], Revision(body, snapshot, current["generation"])


def size(api, ledger):
    revision = getattr(api, "storage_revision", None)
    if revision is None:
        return len(state.render(ledger).encode())
    value = parse_pointer(revision.pointer, identity(api))
    return len(document(identity(api), ledger, value["migrationDigest"], revision.generation + 1))


def limit(api):
    return memory.MAX_BYTES if getattr(api, "storage_revision", None) is not None else state.MAX_BODY


def validate_ledger(api, ledger):
    state.validate(ledger)
    if ledger["repository"] != api.repository:
        raise ValueError("memory candidate target mismatch")
    pins = getattr(api, "validate_followup_pins", None)
    if pins is not None:
        pins(ledger)


def validate_candidate(api, ledger):
    validate_ledger(api, ledger)
    control = getattr(api, "control", None)
    if control is not None:
        matches = [record for record in ledger.get("workItems", []) if record["id"] == control["id"]]
        if len(matches) != 1 or matches[0]["control"] != control:
            raise ValueError("memory candidate work-item control mismatch")


def validate_transport_document(transport, raw):
    value = contracts.loads(raw.decode("utf-8"), max_bytes=memory.MAX_BYTES)
    contracts.exact(value, DOCUMENT_KEYS, "memory transport document")
    authority = value["authority"]
    parse_document(raw, authority, value["migrationDigest"])
    import live
    if (authority["controllerRepository"] != live.REPOSITORY
            or authority["controllerRepositoryId"] != live.REPOSITORY_ID
            or authority["trackerId"] != transport.tracker or authority["commentId"] != transport.authority
            or authority["binding"] != transport.binding.name
            or authority["targetRepository"] != transport.binding.repository
            or authority["targetRepositoryId"] != transport.binding.repository_id
            or authority["actorId"] != 1472 or authority["actorLogin"] != "radical"):
        raise ValueError("memory document transport identity mismatch")
    migration = getattr(transport, "memory_migration_document", None)
    if migration is not None:
        # Activation copies the exact unchanged legacy ledger before the receiver
        # reconciles a new/revised item. This host-only capability cannot authorize
        # any other document or survive into generation-one business writes.
        if raw != migration or value["generation"] != 0:
            raise ValueError("memory migration document changed")
        return
    control = getattr(transport, "control", None)
    if control is not None:
        matches = [record for record in value["ledger"].get("workItems", []) if record["id"] == control["id"]]
        if len(matches) != 1 or matches[0]["control"] != control:
            raise ValueError("memory document transport control mismatch")


def persist(api):
    revision = api.storage_revision
    value = parse_pointer(revision.pointer, identity(api))
    validate_candidate(api, api.ledger)
    raw = document(identity(api), api.ledger, value["migrationDigest"], revision.generation + 1)
    def guard():
        api.authority_guard()
        validate_candidate(api, api.ledger)
    saved = store(api, write=True).append(revision.snapshot, raw, guard)
    # Pointer must still be the original owned descriptor after branch publication.
    if api.authority_body() != revision.pointer:
        raise memory.MemoryUncertain("authority pointer changed after memory append")
    api.storage_revision = api.observed_revision = Revision(revision.pointer, saved, revision.generation + 1)
    api.expected = deepcopy(api.ledger)


def activate(api):
    from github import LostResponse, Response
    if (not api.write or not getattr(api, "memory_authorized", False)
            or getattr(api.transport, "read_only", False) or getattr(api, "_normal_followup", None) is not None):
        raise ValueError("explicit guarded memory activation authorization required")
    guard = getattr(api, "normal_guard", None)
    if guard is None:
        raise ValueError("memory activation requires installed host/exclusive-writer guards")
    guard()
    api.read_authority()
    if api.storage_revision is not None:
        api.authority_guard()
        return
    if api.ledger != api.expected:
        raise ValueError("dirty authority cannot migrate")
    original = api.authority_body()
    ledger = state.parse(original)
    # Subsequent reads are observational: do not pair a newer comment's digest
    # with a stale initialized ledger, which would erase its accounting/holds.
    if ledger != api.expected:
        raise ValueError("legacy authority changed before activation")
    authority, migration = identity(api), digest(original.encode())
    raw = document(authority, ledger, migration, 0)
    branch = store(api, write=True)
    branch.availability(api.actor)
    def before():
        guard()
        if api.authority_body() != original or api.ledger != ledger or api.expected != ledger:
            raise ValueError("legacy authority changed during activation")
        validate_ledger(api, ledger)
    before()
    previous_migration = getattr(api.transport, "memory_migration_document", None)
    api.transport.memory_migration_document = raw
    try:
        try:
            saved = branch.read(missing_file=True)
        except memory.MemoryRefMissing as error:
            if error.endpoint != branch.ref_endpoint:
                raise
            saved = branch.create(raw, before)
        if saved.document is not None and saved.document != raw:
            raise ValueError("existing memory authority is not exact pending migration")
        # Even exact staged data must confirm this selected credential's append
        # contract before activation. No generation/accounting/effect changes.
        saved = branch.append(saved, raw, before)
    finally:
        api.transport.memory_migration_document = previous_migration
    body = pointer(authority, saved, migration, ledger)
    before()
    if branch.head() != saved.commit:
        raise ValueError("prepared memory HEAD changed")
    previous = getattr(api.transport, "memory_request", None)
    api.transport.memory_request = ("PATCH", f"repos/{api.controller_repository}/issues/comments/{api.authority_id}",
                                    {"body": body})
    try:
        try:
            result = api.transport(*api.transport.memory_request)
            if not isinstance(result, Response) or result.status != 200 or result.payload.get("id") != api.authority_id:
                raise LostResponse("memory activation reply unknown")
        except LostResponse:
            pass
    finally:
        api.transport.memory_request = previous
    if api.authority_body() != body:
        raise memory.MemoryUncertain("memory pointer activation uncertain; no retry")
    observed, revision = read(api, body)
    if observed != ledger or revision.snapshot != saved or api.authority_body() != body:
        raise memory.MemoryUncertain("memory activation readback mismatch")
    api.ledger, api.expected = deepcopy(observed), deepcopy(observed)
    api.storage_revision = api.observed_revision = revision


def host_path(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError("host artifact symlink forbidden")
    return path


def write_host_json(path, value, *, max_bytes):
    raw = canonical(value)
    if len(raw) > max_bytes:
        raise ValueError("host artifact bound exhausted")
    descriptor = os.open(host_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def read_host_json(path, *, max_bytes):
    descriptor = os.open(host_path(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("host artifact must be a regular file")
        raw = stream.read(max_bytes + 1)
    return contracts.loads(raw.decode("utf-8"), max_bytes=max_bytes)


def install_local_policy(api):
    """Install only policy; explicit entrypoints activate under the local lock."""
    def guard():
        import local
        local.require_source(api.revision)
        local.require_idle_actions(api.token)
        if not api.enabled():
            raise ValueError("Shepherd disabled; cannot activate memory")
    api.normal_guard = guard
    api.memory_authorized = os.environ.get("CI_SHEPHERD_MEMORY_WRITE") == "true"


def initialize(api):
    if getattr(api, "memory_authorized", False):
        api.initialize_authority()
