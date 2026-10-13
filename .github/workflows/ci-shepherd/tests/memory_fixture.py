"""Serialized offline Git objects; no network or local Git branch mutations."""

import base64
from copy import deepcopy
import hashlib
import json

import branch_memory as memory
from github import LostResponse, Response
import pilot_storage as storage
import pilot_state as state
from test_pilot_github import Transport


class MemoryRemote(Transport):
    def __init__(self, original=None):
        super().__init__()
        if original is not None:
            self.__dict__.update(deepcopy(original.__dict__))
            self.values["repos/radical/aspire/issues/99/comments"] = self.comments
        self.objects = {}
        self.head = None
        self.calls = []
        self.append_fault = None
        self.patch_fault = None
        self.available = True
        self.permission = "ADMIN"
        self.viewer = {"login": "radical", "databaseId": 1472}

    def object(self, kind, value):
        sha = hashlib.sha1((kind + json.dumps(value, sort_keys=True)).encode()).hexdigest()
        self.objects[sha] = {"kind": kind, "value": deepcopy(value)}
        return sha

    def tree(self, files):
        root = {}
        for path, blob in files.items():
            parts = path.split("/")
            current = root
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = blob
        def build(node):
            entries = []
            for name, child in sorted(node.items()):
                entries.append({"path": name, "mode": "040000" if isinstance(child, dict) else "100644",
                                "type": "tree" if isinstance(child, dict) else "blob",
                                "sha": build(child) if isinstance(child, dict) else child})
            return self.object("tree", {"tree": entries, "truncated": False})
        return build(root)

    def files(self, sha):
        root = self.objects[sha]["value"]["tree"]["sha"]
        result = {}
        def walk(tree, prefix):
            for entry in self.objects[tree]["value"]["tree"]:
                path = prefix + entry["path"]
                if entry["type"] == "tree":
                    walk(entry["sha"], path + "/")
                else:
                    result[path] = entry["sha"]
        walk(root, "")
        return result

    def append(self, expected, path, raw):
        if self.head != expected:
            return Response({"errors": [{"type": "STALE_DATA", "message": "expected head conflict"}]}, {})
        files = self.files(self.head)
        blob = self.object("blob", {"content": memory.encode(raw), "size": len(raw), "encoding": "base64"})
        files[path] = blob
        tree = self.tree(files)
        parents = [{"sha": expected}]
        if self.append_fault == "wrong-parent":
            parents = []
        sha = self.object("commit", {"tree": {"sha": tree}, "parents": parents,
                                     "sequence": len(self.calls)})
        self.head = sha
        return Response({"data": {"createCommitOnBranch": {
            "commit": {"oid": sha, "parents": {"nodes": [{"oid": p["sha"]} for p in parents]}},
            "ref": {"name": memory.BRANCH, "target": {"oid": sha}}}}}, {})

    def __call__(self, method, endpoint, body):
        prefix = "repos/radical/aspire/git/"
        if endpoint.startswith(prefix) or endpoint == "graphql" and body.get("query") in {memory.APPEND, memory.AVAILABILITY}:
            self.calls.append([method, endpoint])
            if endpoint == "graphql":
                if body["query"] == memory.AVAILABILITY:
                    if not self.available:
                        return Response({"errors": [{"type": "FORBIDDEN"}], "data": None}, {})
                    return Response({"data": {"viewer": self.viewer, "repository": {
                        "nameWithOwner": "radical/aspire", "databaseId": 746880239,
                        "viewerPermission": self.permission}}}, {})
                value = body["variables"]["input"]
                if self.append_fault == "denied":
                    return Response({"errors": [{"type": "FORBIDDEN"}], "data": None}, {})
                reply = self.append(value["expectedHeadOid"], value["fileChanges"]["additions"][0]["path"],
                                    base64.b64decode(value["fileChanges"]["additions"][0]["contents"], validate=True))
                fault = self.append_fault
                self.append_fault = None
                if fault == "lost":
                    raise LostResponse("lost append receipt")
                if fault == "crash":
                    raise KeyboardInterrupt("after staged append")
                if fault in {"errors", "partial"}:
                    return Response({"errors": [{"type": "RATE_LIMITED"}], "data": reply.payload.get("data")}, {})
                if fault == "unreadable":
                    self.head = None
                    raise LostResponse("lost append and ref")
                return reply
            resource = endpoint[len(prefix):]
            if method == "GET" and resource == "ref/heads/" + memory.BRANCH:
                if self.head is None:
                    raise memory.MemoryRefMissing(endpoint)
                return Response({"ref": memory.REF, "object": {"sha": self.head, "type": "commit"}}, {})
            if method == "GET":
                kind, sha = resource.split("/")
                value = deepcopy(self.objects[sha]["value"])
                value["sha"] = sha
                if kind == "commits":
                    for parent in value["parents"]:
                        parent["url"] = "https://api.github.com/repos/radical/aspire/git/commits/" + parent["sha"]
                        parent["html_url"] = "https://github.com/radical/aspire/commit/" + parent["sha"]
                if kind == "blobs":
                    content = value["content"]
                    value["content"] = "\n".join(content[i:i + 60] for i in range(0, len(content), 60)) + "\n"
                return Response(value, {})
            if resource == "blobs":
                raw = base64.b64decode(body["content"], validate=True)
                sha = self.object("blob", {"content": body["content"], "size": len(raw), "encoding": "base64"})
            elif resource == "trees":
                sha = self.tree({entry["path"]: entry["sha"] for entry in body["tree"]})
            elif resource == "commits":
                sha = self.object("commit", {"tree": {"sha": body["tree"]}, "parents": body["parents"]})
            elif resource == "refs":
                if self.head is not None:
                    raise LostResponse("concurrent ref creation")
                self.head = body["sha"]
                return Response({"ref": memory.REF, "object": {"sha": self.head, "type": "commit"}}, {}, 201)
            else:
                raise AssertionError(resource)
            return Response({"sha": sha}, {}, 201)
        response = super().__call__(method, endpoint, body)
        if method == "PATCH" and endpoint.endswith("/comments/500") and self.patch_fault:
            fault, self.patch_fault = self.patch_fault, None
            if fault == "crash":
                raise KeyboardInterrupt("after pointer PATCH")
            raise LostResponse("lost pointer response")
        return response

    def current_ledger(self):
        body = self.comments[0]["body"]
        if not storage.is_pointer(body):
            return state.parse(body)
        pointer = json.loads(body[len(storage.HEADER):])
        blob = self.files(self.head)[pointer["path"]]
        raw = base64.b64decode(self.objects[blob]["value"]["content"])
        return json.loads(raw)["ledger"]


def enable(fixture):
    import os
    fixture.remote = MemoryRemote(fixture.remote)
    fixture.saved = fixture.remote.current_ledger
    os.environ["CI_SHEPHERD_MEMORY_WRITE"] = "true"


def seed_full_context(fixture, target=49903, original_context=None):
    import normal_loop
    ledger = fixture.saved()
    receipt = ledger["normalRuns"][0]
    original = original_context or json.loads((fixture.root / "1/trusted/packet.json").read_text())["context"]
    receipt["context"] = deepcopy(original)
    receipt["context"]["issue"]["body"] = "Sanitized historic failure evidence. " * 700
    unrelated = deepcopy(receipt)
    unrelated.update(id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                     packetId="cccccccc-cccc-4ccc-8ccc-cccccccccccc", item="UNRELATED-HISTORY",
                     sessionId=None, decision=None)
    unrelated.pop("turnId", None)
    unrelated["context"]["issue"]["body"] = ""
    ledger["normalRuns"].append(unrelated)
    padding = target - len(state.render(ledger).encode())
    if padding < 0:
        raise ValueError("fixture shape exceeds requested size")
    unrelated["context"]["issue"]["body"] = "h" * padding
    normal_loop.validate_receipts(ledger["normalRuns"])
    fixture.remote.comments[0]["body"] = state.render(ledger)
    assert len(fixture.remote.comments[0]["body"].encode()) == target
    return deepcopy(ledger)
