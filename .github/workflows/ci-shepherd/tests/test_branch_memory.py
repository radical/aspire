import unittest
import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import threading
from unittest.mock import patch

import helpers
import branch_memory as memory
from github import IncompleteInventory, LostResponse, Response
import live
import pilot_github
import pilot_storage as storage
import pilot_state as state
import round as contracts
from memory_fixture import MemoryRemote


class BranchMemoryTests(unittest.TestCase):
    def valid_document(self):
        api = pilot_github.PilotGitHub(MemoryRemote(), 99, 500, "TRACKER99", write=True)
        return storage.document(storage.identity(api), state.new_ledger(), "a" * 64, 1)

    def test_actual_rest_wrapping_round_trip(self):
        raw = b'{"retainedContext":"' + b"x" * 49903 + b'"}'
        encoded = memory.encode(raw)
        wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + "\n"
        self.assertEqual(raw, memory.decode_blob(
            {"sha": "a" * 40, "size": len(raw), "encoding": "base64", "content": wrapped}, "a" * 40))
        self.assertNotIn("\n", encoded)

    def branch(self, remote=None):
        remote = remote or MemoryRemote()
        return remote, memory.BranchMemory(remote, "radical/aspire", 746880239,
                                           "authorities/746880239/99-500-fork.json", write=True)

    def test_strict_wrapping_padding_and_size_fail_closed(self):
        for content, size in (("YQ= =", 1), ("YQ==\r\n", 1), ("YQ==\t", 1), ("YQ==\n\n", 1),
                              ("YQ==\nYQ==", 2), ("YR==", 1), ("YQ==", 2), ("?", 0)):
            with self.subTest(content=content), self.assertRaises(ValueError):
                memory.decode_blob({"sha": "a" * 40, "size": size, "encoding": "base64",
                                    "content": content}, "a" * 40)
        for change in ({"size": True}, {"size": memory.MAX_BYTES + 1}, {"encoding": "utf-8"},
                       {"sha": "b" * 40}, {"content": "a" * (memory.MAX_WIRE_BYTES + 1)}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                memory.decode_blob({"sha": "a" * 40, "size": 1, "encoding": "base64",
                                    "content": "YQ=="} | change, "a" * 40)

    def test_decoded_application_boundary_and_unwrapped_mutation(self):
        raw = b"a" * memory.MAX_BYTES
        self.assertEqual(raw, memory.decode_blob(
            {"sha": "a" * 40, "size": len(raw), "encoding": "base64", "content": memory.encode(raw)}, "a" * 40))
        with self.assertRaises(ValueError):
            memory.encode(raw + b"a")

    def test_bootstrap_orphan_and_exact_single_parent_append(self):
        remote, branch = self.branch()
        branch.availability({"id": 1472, "login": "radical"})
        original = branch.create(b'{"full":"history"}', lambda: None)
        self.assertEqual([], branch.commit(original.commit)["parents"])
        saved = branch.append(original, b'{"full":"new history"}', lambda: None)
        self.assertEqual([original.commit], [parent["sha"] for parent in branch.commit(saved.commit)["parents"]])
        self.assertEqual(b'{"full":"new history"}', branch.read().document)
        self.assertEqual([branch.path], list(remote.files(remote.head)))

    def test_other_authority_file_is_preserved(self):
        remote, branch = self.branch()
        original = branch.create(b"{}", lambda: None)
        other = memory.BranchMemory(remote, "radical/aspire", 746880239,
                                    "authorities/746880239/98-501-fork.json", write=True)
        empty = other.read(missing_file=True)
        added = other.append(empty, b'{"other":true}', lambda: None)
        original = branch.read()
        branch.append(original, b'{"changed":true}', lambda: None)
        self.assertEqual(b'{"other":true}', other.read().document)
        self.assertEqual(added.document, other.read().document)

    def test_stale_head_identical_content_and_deleted_ref_never_retry(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                remote, branch = self.branch()
                original = branch.create(b"{}", lambda: None)
                branch.append(original, b"{}", lambda: None)
                if missing:
                    remote.head = None
                before = len([call for call in remote.calls if call == ["POST", "graphql"]])
                with self.assertRaises((ValueError, memory.MemoryRefMissing)):
                    branch.append(original, b'{"stale":true}', lambda: None)
                self.assertEqual(before, len([call for call in remote.calls if call == ["POST", "graphql"]]))

    def test_lost_error_and_partial_receipts_only_accept_exact_readback(self):
        for fault in ("lost", "errors", "partial"):
            with self.subTest(fault=fault):
                remote, branch = self.branch()
                original = branch.create(b"{}", lambda: None)
                remote.append_fault = fault
                saved = branch.append(original, b'{"full":"preserved"}', lambda: None)
                self.assertEqual(saved, branch.read())
                self.assertEqual(1, sum(call == ["POST", "graphql"] for call in remote.calls))
        for fault in ("denied", "wrong-parent", "unreadable"):
            with self.subTest(fault=fault):
                remote, branch = self.branch()
                original = branch.create(b"{}", lambda: None)
                remote.append_fault = fault
                with self.assertRaises(memory.MemoryUncertain):
                    branch.append(original, b'{"full":"preserved"}', lambda: None)
                self.assertEqual(1, sum(call == ["POST", "graphql"] for call in remote.calls))

    def test_guard_rejection_prevents_mutation(self):
        remote, branch = self.branch()
        original = branch.create(b"{}", lambda: None)
        before = deepcopy(remote.calls)
        def reject():
            raise ValueError("host policy changed")
        with self.assertRaisesRegex(ValueError, "host policy"):
            branch.append(original, b'{"new":true}', reject)
        self.assertEqual(before, remote.calls)

    def test_source_root_symlink_truncated_and_wrong_blob_metadata_fail_closed(self):
        for fault in ("source", "symlink", "truncated", "wrong-blob-sha", "size"):
            remote, branch = self.branch()
            saved = branch.create(b"{}", lambda: None)
            root = remote.objects[saved.commit]["value"]["tree"]["sha"]
            root_value = remote.objects[root]["value"]
            if fault == "source":
                root_value["tree"][0]["path"] = "src"
            elif fault == "symlink":
                root_value["tree"][0]["mode"] = "120000"
            elif fault == "truncated":
                root_value["truncated"] = True
            elif fault == "size":
                remote.objects[saved.blob]["value"]["size"] = 3
            else:
                original = remote.__call__
                class Changed:
                    def __call__(self, method, endpoint, body):
                        result = original(method, endpoint, body)
                        if "/git/blobs/" in endpoint:
                            result.payload["sha"] = "f" * 40
                        return result
                branch.transport = Changed()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                branch.read()

    def test_controller_identity_failure_cannot_bootstrap_even_if_ref_is_missing(self):
        remote, branch = self.branch()
        remote.values["repos/radical/aspire"]["id"] = 1
        with self.assertRaisesRegex(ValueError, "controller"):
            branch.create(b"{}", lambda: None)
        self.assertEqual([], remote.calls)
        self.assertIsNone(remote.head)

    def test_availability_checks_actor_controller_errors_and_permission(self):
        for attribute, value in (("available", False), ("permission", "READ"),
                                 ("viewer", {"login": "other", "databaseId": 1472})):
            remote, branch = self.branch()
            setattr(remote, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                branch.availability({"id": 1472, "login": "radical"})
            self.assertIsNone(remote.head)
            self.assertTrue(all(endpoint == "graphql" for method, endpoint in remote.calls))

    def test_read_only_and_arbitrary_graphql_cannot_gain_write_capability(self):
        transport = pilot_github.PilotTransport("fixture", write=False, tracker=99, authority=500)
        body = {"query": memory.APPEND, "variables": {}}
        transport.memory_request = ("POST", "graphql", body)
        with self.assertRaisesRegex(ValueError, "read-only"):
            transport.validate_endpoint("POST", "graphql", body)
        transport.write = True
        transport.memory_request = ("POST", "graphql", {"query": "mutation Evil", "variables": {}})
        with self.assertRaises(ValueError):
            transport.validate_endpoint(*transport.memory_request)
        transport.memory_request = ("DELETE", "repos/radical/aspire/git/refs/heads/" + memory.BRANCH, None)
        with self.assertRaises(ValueError):
            transport.validate_endpoint(*transport.memory_request)

    def test_append_is_write_and_namespace_changes_are_rejected(self):
        remote, branch = self.branch()
        snapshot = branch.create(b"{}", lambda: None)
        calls = []
        original = remote.__call__
        def capture(method, endpoint, body):
            calls.append((method, endpoint, deepcopy(body)))
            return original(method, endpoint, body)
        branch.transport = capture
        # Callable fixture cannot carry scoped capability attributes.
        class Wrapper:
            def __call__(self, *args):
                return capture(*args)
        branch.transport = Wrapper()
        branch.append(snapshot, self.valid_document(), lambda: None)
        request = next(call for call in calls if call[1] == "graphql")
        transport = pilot_github.PilotTransport("fixture", write=True, tracker=99, authority=500)
        transport.memory_request = request
        self.assertFalse(transport.is_read(*request))
        changed = deepcopy(request[2])
        changed["variables"]["input"]["branch"]["branchName"] = "main"
        transport.memory_request = ("POST", "graphql", changed)
        with self.assertRaises(ValueError):
            transport.validate_endpoint(*transport.memory_request)

    def test_real_http_memory_cap_before_parse_typed_404_and_no_mutation_retry(self):
        requests = []
        payload = [b'{"ok":true}', 200]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.command)
                self.send_response(payload[1])
                self.send_header("Content-Length", str(len(payload[0])))
                self.end_headers()
                self.wfile.write(payload[0])
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.do_GET()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        class LocalOpener:
            def open(self, request, timeout):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
                connection.request(request.method, "/" + request.full_url.split("api.github.com/", 1)[1],
                                   body=request.data, headers=dict(request.header_items()))
                response = connection.getresponse()
                # urllib normally raises HTTPError; preserve that behavior here.
                if response.status >= 400:
                    from urllib.error import HTTPError
                    raise HTTPError(request.full_url, response.status, "fixture", response.headers, response)
                return response
        transport = pilot_github.PilotTransport("fixture", write=True, tracker=99, authority=500)
        transport.opener = LocalOpener()
        endpoint = "repos/radical/aspire/git/ref/heads/" + memory.BRANCH
        transport.memory_request = ("GET", endpoint, None)
        self.assertEqual({"ok": True}, transport("GET", endpoint, None).payload)
        payload[:] = [b'{"pad":"' + b"x" * (memory.MAX_WIRE_BYTES - 10) + b'"}', 200]
        self.assertEqual(memory.MAX_WIRE_BYTES, len(payload[0]))
        self.assertEqual("x" * (memory.MAX_WIRE_BYTES - 10), transport("GET", endpoint, None).payload["pad"])
        payload[0] += b" "
        with patch.object(contracts, "loads", side_effect=AssertionError("parser must not run")):
            with self.assertRaises(IncompleteInventory):
                transport("GET", endpoint, None)
        payload[:] = [b"{}", 404]
        with self.assertRaises(memory.MemoryRefMissing) as missing:
            transport("GET", endpoint, None)
        self.assertEqual(endpoint, missing.exception.endpoint)
        other = "repos/radical/aspire/git/blobs/" + "a" * 40
        transport.memory_request = ("GET", other, None)
        with self.assertRaises(IncompleteInventory):
            transport("GET", other, None)
        body = {"query": memory.APPEND, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": "radical/aspire", "branchName": memory.BRANCH},
            "expectedHeadOid": "a" * 40, "fileChanges": {"additions": [{
                "path": "authorities/746880239/99-500-fork.json", "contents": memory.encode(self.valid_document())}]},
            "message": {"headline": "Persist Shepherd authority"}}}}
        transport.memory_request = ("POST", "graphql", body)
        for raw, status in ((b"", 200), (b"{", 200), (b"{}", 403),
                            (b"x" * (memory.MAX_WIRE_BYTES + 1), 200)):
            payload[:] = [raw, status]
            before = len(requests)
            with self.subTest(status=status, size=len(raw)), self.assertRaises(LostResponse):
                transport("POST", "graphql", body)
            self.assertEqual(before + 1, len(requests))
