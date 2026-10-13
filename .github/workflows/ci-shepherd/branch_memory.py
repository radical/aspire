"""Host-only isolated Git memory, with exact expected-head publication."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re

from github import IncompleteInventory, LostResponse, Response
import round as contracts

BRANCH = "memory/ci-shepherd"
REF = "refs/heads/" + BRANCH
MAX_BYTES = 1048576
MAX_WIRE_BYTES = 2097152
APPEND = """mutation ShepherdMemoryAppend($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid parents(first: 2) { nodes { oid } } }
    ref { name target { oid } }
  }
}"""
# expectedHeadOid is required, unlike a REST fast-forward ancestry check.
# https://docs.github.com/en/graphql/reference/commits#createcommitonbranchinput
AVAILABILITY = """query ShepherdMemoryAvailability($owner: String!, $name: String!) {
  viewer { login databaseId }
  repository(owner: $owner, name: $name) { nameWithOwner databaseId viewerPermission }
}"""


class MemoryRefMissing(IncompleteInventory):
    def __init__(self, endpoint):
        super().__init__("exact memory ref is absent")
        self.endpoint = endpoint


class MemoryUncertain(ValueError):
    pass


def oid(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("invalid Git object identity")
    return value


def encode(document):
    if not isinstance(document, bytes) or len(document) > MAX_BYTES:
        raise ValueError("memory application bound exhausted")
    return base64.b64encode(document).decode("ascii")


def decode_blob(value, expected):
    if (not isinstance(value, dict) or value.get("sha") != oid(expected)
            or value.get("encoding") != "base64" or type(value.get("size")) is not int
            or not 0 <= value["size"] <= MAX_BYTES or not isinstance(value.get("content"), str)
            or len(value["content"]) > MAX_WIRE_BYTES):
        raise ValueError("invalid bounded memory blob")
    # GitHub REST blobs use 60-character LF lines, e.g. "eyJ...\n...==\n".
    # Validate those separators, rather than stripping arbitrary whitespace.
    encoded = value["content"]
    if "\n" in encoded:
        lines = encoded.removesuffix("\n").split("\n")
        if (not lines[-1] or len(lines[-1]) > 60
                or any(len(line) != 60 for line in lines[:-1])):
            raise ValueError("invalid memory base64 wrapping")
        encoded = "".join(lines)
    if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("encoded memory bound exhausted")
    try:
        document = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid memory base64") from error
    if base64.b64encode(document).decode("ascii") != encoded or len(document) != value["size"]:
        raise ValueError("noncanonical memory base64 or size")
    return document


def validate_request(method, endpoint, body, repository, repository_id, tracker, comment, binding):
    """Closed transport capability; no arbitrary mutations or ref rewrites."""
    prefix = "repos/" + repository
    path_pattern = (r"authorities/" + str(repository_id) + "/"
                    + str(tracker) + "-" + str(comment) + "-" + re.escape(binding) + r"\.json")
    if method == "GET":
        if body is not None or not (endpoint == prefix or endpoint == prefix + "/git/ref/heads/" + BRANCH
                                  or re.fullmatch(re.escape(prefix) + r"/git/(?:commits|trees|blobs)/[0-9a-f]{40}", endpoint)):
            raise ValueError("memory read capability mismatch")
        return
    if endpoint == "graphql" and method == "POST":
        contracts.exact(body, {"query", "variables"}, "memory GraphQL request")
        if body["query"] == AVAILABILITY:
            owner, name = repository.split("/")
            if body["variables"] != {"owner": owner, "name": name}:
                raise ValueError("memory availability namespace mismatch")
            return
        if body["query"] != APPEND:
            raise ValueError("fixed memory append required")
        contracts.exact(body["variables"], {"input"}, "memory variables")
        value = body["variables"]["input"]
        contracts.exact(value, {"branch", "expectedHeadOid", "fileChanges", "message"}, "memory append")
        oid(value["expectedHeadOid"])
        if (value["branch"] != {"repositoryNameWithOwner": repository, "branchName": BRANCH}
                or value["message"] != {"headline": "Persist Shepherd authority"}):
            raise ValueError("memory append namespace/message mismatch")
        contracts.exact(value["fileChanges"], {"additions"}, "memory file changes")
        additions = value["fileChanges"]["additions"]
        if not isinstance(additions, list) or len(additions) != 1:
            raise ValueError("exact one memory addition required")
        addition = additions[0]
        contracts.exact(addition, {"path", "contents"}, "memory addition")
        if not isinstance(addition["path"], str) or re.fullmatch(path_pattern, addition["path"]) is None:
            raise ValueError("memory addition path mismatch")
        mutation_bytes(addition["contents"])
        return
    if method == "PATCH" and endpoint == prefix + "/issues/comments/" + str(comment):
        contracts.exact(body, {"body"}, "memory pointer patch")
        return
    if method != "POST":
        raise ValueError("memory mutation forbidden")
    if endpoint == prefix + "/git/blobs":
        contracts.exact(body, {"encoding", "content"}, "memory bootstrap blob")
        if body["encoding"] != "base64" or not isinstance(body["content"], str) or "\n" in body["content"]:
            raise ValueError("memory bootstrap encoding mismatch")
        mutation_bytes(body["content"])
    elif endpoint == prefix + "/git/trees":
        contracts.exact(body, {"tree"}, "memory bootstrap tree")
        if not isinstance(body["tree"], list) or len(body["tree"]) != 1:
            raise ValueError("memory bootstrap exact one file required")
        entry = body["tree"][0]
        contracts.exact(entry, {"path", "mode", "type", "sha"}, "memory bootstrap tree entry")
        if (re.fullmatch(path_pattern, entry["path"]) is None
                or entry["mode"] != "100644" or entry["type"] != "blob"):
            raise ValueError("memory bootstrap path/type mismatch")
        oid(entry["sha"])
    elif endpoint == prefix + "/git/commits":
        contracts.exact(body, {"message", "tree", "parents"}, "memory bootstrap commit")
        if body["message"] != "Initialize Shepherd memory" or body["parents"] != []:
            raise ValueError("memory bootstrap must be orphan")
        oid(body["tree"])
    elif endpoint == prefix + "/git/refs":
        contracts.exact(body, {"ref", "sha"}, "memory bootstrap ref")
        if body["ref"] != REF:
            raise ValueError("exact memory ref required")
        oid(body["sha"])
    else:
        raise ValueError("memory bootstrap endpoint forbidden")


def mutation_bytes(encoded):
    if (not isinstance(encoded, str) or "\n" in encoded
            or len(encoded) > 4 * ((MAX_BYTES + 2) // 3)):
        raise ValueError("bounded unwrapped mutation encoding required")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid mutation encoding") from error
    if encode(raw) != encoded:
        raise ValueError("noncanonical mutation encoding")
    return raw


def graphql_errors(value):
    if set(value) - {"data", "errors", "extensions"}:
        return True
    errors = value.get("errors", [])
    # Do not log untrusted diagnostics or interpret message substrings as
    # proof of rejection. Malformed/partial HTTP 200 also requires readback.
    return not isinstance(errors, list) or bool(errors)


@dataclass(frozen=True)
class MemorySnapshot:
    commit: str
    blob: str | None
    document: bytes | None


class BranchMemory:
    def __init__(self, transport, repository, repository_id, path, *, write=False):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or type(repository_id) is not int or repository_id <= 0
                or not re.fullmatch(r"authorities/" + str(repository_id) + r"/[1-9][0-9]*-[1-9][0-9]*-[a-z0-9-]+\.json", path)):
            raise ValueError("trusted memory identity/path required")
        self.transport, self.repository, self.repository_id = transport, repository, repository_id
        self.path, self.write = path, write
        self.prefix = "repos/" + repository
        self.ref_endpoint = self.prefix + "/git/ref/heads/" + BRANCH

    def request(self, method, endpoint, body=None):
        if method != "GET" and not (
                method == "POST" and endpoint == "graphql" and body.get("query") == AVAILABILITY):
            if not self.write:
                raise ValueError("memory writer authorization required")
        # A scoped, exact host-created capability; never forwarded to agent tools.
        previous = getattr(self.transport, "memory_request", None)
        self.transport.memory_request = (method, endpoint, body)
        try:
            result = self.transport(method, endpoint, body)
        finally:
            self.transport.memory_request = previous
        if not isinstance(result, Response) or result.status not in {200, 201} or not isinstance(result.payload, dict):
            raise (IncompleteInventory if method == "GET" else LostResponse)("memory result unavailable")
        return result.payload

    def verify_repository(self):
        value = self.request("GET", self.prefix)
        if value.get("id") != self.repository_id or value.get("full_name") != self.repository:
            raise ValueError("memory controller repository changed")

    def availability(self, actor):
        self.verify_repository()
        owner, name = self.repository.split("/")
        value = self.request("POST", "graphql",
                             {"query": AVAILABILITY, "variables": {"owner": owner, "name": name}})
        data = value.get("data")
        if graphql_errors(value) or not isinstance(data, dict):
            raise ValueError("selected memory credential GraphQL unavailable")
        if data.get("viewer") != {"login": actor["login"], "databaseId": actor["id"]}:
            raise ValueError("memory credential actor changed")
        repository = data.get("repository")
        if (not isinstance(repository, dict) or repository.get("databaseId") != self.repository_id
                or repository.get("nameWithOwner") != self.repository
                or repository.get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}):
            raise ValueError("memory credential write permission unavailable")
        # Permission metadata is not mutation proof. Activation also requires
        # an authorized fixed append and exact commit-bound readback.

    def head(self):
        value = self.request("GET", self.ref_endpoint)
        obj = value.get("object")
        if value.get("ref") != REF or not isinstance(obj, dict) or obj.get("type") != "commit":
            raise ValueError("invalid exact memory ref")
        return oid(obj.get("sha"))

    def commit(self, sha):
        value = self.request("GET", self.prefix + "/git/commits/" + oid(sha))
        if value.get("sha") != sha or not isinstance(value.get("tree"), dict):
            raise ValueError("invalid memory commit")
        oid(value["tree"].get("sha"))
        parents = value.get("parents")
        if not isinstance(parents, list) or any(not isinstance(p, dict) for p in parents):
            raise ValueError("invalid memory parents")
        for parent in parents:
            oid(parent.get("sha"))
        return value

    def at(self, sha, *, missing_file=False):
        tree = self.commit(sha)["tree"]["sha"]
        parts = self.path.split("/")
        for index, part in enumerate(parts):
            value = self.request("GET", self.prefix + "/git/trees/" + tree)
            entries = value.get("tree")
            if (value.get("sha") != tree or value.get("truncated") is not False
                    or not isinstance(entries, list) or len(entries) > 10000
                    or any(not isinstance(e, dict) for e in entries)):
                raise ValueError("invalid/truncated memory tree")
            if index == 0 and any(e.get("path") != "authorities" for e in entries):
                raise ValueError("memory branch contains source or unrelated root")
            matches = [e for e in entries if e.get("path") == part]
            if not matches and missing_file:
                return MemorySnapshot(sha, None, None)
            if len(matches) != 1:
                raise ValueError("missing/duplicate memory path")
            entry = matches[0]
            leaf = index == len(parts) - 1
            if entry.get("mode") != ("100644" if leaf else "040000") or entry.get("type") != ("blob" if leaf else "tree"):
                raise ValueError("memory path is not a regular data file")
            tree = oid(entry.get("sha"))
        document = decode_blob(self.request("GET", self.prefix + "/git/blobs/" + tree), tree)
        return MemorySnapshot(sha, tree, document)

    def read(self, *, missing_file=False):
        head = self.head()
        result = self.at(head, missing_file=missing_file)
        if self.head() != head:
            raise ValueError("memory ref changed during read")
        return result

    def create(self, document, before_publish):
        encode(document)
        self.verify_repository()
        try:
            self.head()
        except MemoryRefMissing as error:
            if error.endpoint != self.ref_endpoint:
                raise
        else:
            raise ValueError("memory branch already exists")
        before_publish()
        blob = oid(self.request("POST", self.prefix + "/git/blobs",
                                {"encoding": "base64", "content": encode(document)}).get("sha"))
        tree = oid(self.request("POST", self.prefix + "/git/trees",
                                {"tree": [{"path": self.path, "mode": "100644", "type": "blob", "sha": blob}]}).get("sha"))
        root = oid(self.request("POST", self.prefix + "/git/commits",
                                {"message": "Initialize Shepherd memory", "tree": tree, "parents": []}).get("sha"))
        before_publish()
        try:
            value = self.request("POST", self.prefix + "/git/refs", {"ref": REF, "sha": root})
            if value.get("ref") != REF or value.get("object", {}).get("sha") != root:
                raise LostResponse("memory ref creation uncertain")
        except LostResponse:
            pass
        try:
            saved = self.read()
            if saved != MemorySnapshot(root, blob, document) or self.commit(root)["parents"]:
                raise ValueError("bootstrap readback mismatch")
        except (ValueError, IncompleteInventory) as error:
            raise MemoryUncertain("memory bootstrap uncertain; no retry") from error
        return saved

    def append(self, expected, document, before_publish):
        encoded = encode(document)
        before_publish()
        if self.head() != expected.commit:
            raise ValueError("memory expected HEAD changed; no retry")
        body = {"query": APPEND, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": self.repository, "branchName": BRANCH},
            "expectedHeadOid": expected.commit,
            "fileChanges": {"additions": [{"path": self.path, "contents": encoded}]},
            "message": {"headline": "Persist Shepherd authority"}}}}
        before_publish()
        returned = None
        try:
            value = self.request("POST", "graphql", body)
            data = value.get("data")
            result = data.get("createCommitOnBranch") if isinstance(data, dict) else None
            if graphql_errors(value) or not isinstance(result, dict):
                raise LostResponse("memory GraphQL errors or partial result")
            commit, ref = result["commit"], result["ref"]
            returned = oid(commit["oid"])
            if (commit["parents"] != {"nodes": [{"oid": expected.commit}]}
                    or ref != {"name": BRANCH, "target": {"oid": returned}}):
                raise LostResponse("memory append receipt mismatch")
        except (LostResponse, KeyError, TypeError, ValueError):
            # A write reply (including HTTP 200 errors) cannot establish no write.
            # Resolve only the exact document and single expected parent, never retry.
            pass
        try:
            saved = self.read()
            if (saved.commit == expected.commit or returned is not None and saved.commit != returned
                    or saved.document != document
                    or [parent["sha"] for parent in self.commit(saved.commit)["parents"]] != [expected.commit]):
                raise ValueError("memory append readback mismatch")
        except (ValueError, IncompleteInventory) as error:
            raise MemoryUncertain("memory append uncertain/conflicted; no retry") from error
        return saved
