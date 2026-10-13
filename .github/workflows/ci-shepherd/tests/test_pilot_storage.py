from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from helpers import WorkspaceTest
import branch_memory as memory
from github import LostResponse
import live
import local
import normal_loop
import pilot
import pilot_github
import pilot_storage as storage
import pilot_state as state
import round as contracts
from memory_fixture import MemoryRemote, enable, seed_full_context
import test_normal_loop as normal_tests
import work_item_execution as execution
import work_item_github as work_github
import work_item_receiver as receiver
import work_items as items
from test_work_items import control as item_control

RUN = normal_tests.RUN


class PilotStorageTests(WorkspaceTest, unittest.TestCase):
    def api(self, remote=None, *, write=True):
        remote = remote or MemoryRemote()
        api = pilot_github.PilotGitHub(remote, 99, 500, "TRACKER99", write=write)
        api.normal_guard = lambda: None
        api.memory_authorized = True
        return api

    def legacy_with_hold(self):
        ledger = state.new_ledger()
        ledger["normalRuns"] = [{
            "id": "retained-inference", "packetId": "retained-packet", "item": "retained-item",
            "phase": "issue-intake", "at": "2026-10-04T00:00:00Z", "nativeActual": None,
            "nativeReserved": 30, "sessionId": None, "state": "reserved",
            "context": {"issue": {"identity": {"repository": "radical/aspire",
                                             "number": 7, "node_id": "ISSUE7"}}}, "decision": None}]
        state.validate(ledger)
        return ledger

    def work_item_api(self, control, ledger, *, cloud=False):
        remote, activated = MemoryRemote(), []
        remote.comments[0]["body"] = state.render(ledger)
        remote.values["repos/radical/aspire/issues/900"] = {
            "number": 900, "node_id": "ISSUE900", "state": "open", "labels": []}

        def http(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            response = remote(method, endpoint, body)
            if method == "PATCH" and endpoint.endswith("/comments/500"):
                activated.append(deepcopy(remote.current_ledger()))
            return response

        for mocked in (patch.object(live.HTTPTransport, "__call__", http),
                       patch.object(local, "require_source"), patch.object(local, "require_idle_actions")):
            mocked.start()
            self.addCleanup(mocked.stop)
        cls = work_github.CloudWorkItemGitHub if cloud else work_github.WorkItemGitHub
        api = cls("fixture", 99, 500, "TRACKER99", revision="c" * 40, control=control)
        enabled = patch.object(api, "enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        api.memory_authorized = True
        return api, remote, activated

    def test_stale_loaded_legacy_rejects_new_remote_hold_before_any_mutation(self):
        api = self.api()
        api.read_authority()
        loaded = deepcopy(api.ledger)
        changed = self.legacy_with_hold()
        changed["cursor"] = 1
        body = state.render(changed)
        api.transport.comments[0]["body"] = body
        with self.assertRaisesRegex(ValueError, "legacy authority changed"):
            api.initialize_authority()
        self.assertEqual(loaded, api.ledger)
        self.assertEqual(loaded, api.expected)
        self.assertEqual(body, api.authority_body())
        self.assertEqual(changed, api.transport.current_ledger())
        self.assertEqual([], api.transport.calls)
        self.assertEqual([], api.transport.writes)
        self.assertEqual({}, api.transport.objects)
        self.assertIsNone(api.transport.head)

    def test_legacy_drift_after_capture_rejects_before_staging(self):
        api = self.api()
        api.read_authority()
        changed = self.legacy_with_hold()
        body = state.render(changed)

        def drift(*_):
            api.transport.comments[0]["body"] = body

        with patch.object(memory.BranchMemory, "availability", side_effect=drift):
            with self.assertRaisesRegex(ValueError, "legacy authority changed"):
                api.initialize_authority()
        self.assertEqual(body, api.authority_body())
        self.assertEqual(changed, api.transport.current_ledger())
        self.assertEqual([], api.transport.calls)
        self.assertEqual([], api.transport.writes)
        self.assertEqual({}, api.transport.objects)
        self.assertIsNone(api.transport.head)

    def test_new_and_revised_controls_migrate_unchanged_then_reconcile_receiver_and_cloud(self):
        for cloud in (False, True):
            for revised in (False, True):
                with self.subTest(cloud=cloud, revised=revised):
                    supplied, legacy = item_control(), self.legacy_with_hold()
                    if revised:
                        previous = deepcopy(supplied)
                        previous["action"] = "pause"
                        legacy["workItems"] = [items.new_record(previous)]
                        supplied.update(revision=2, scope="Fix only the newly approved pinned boundary.")
                    api, remote, activated = self.work_item_api(supplied, legacy, cloud=cloud)
                    packets = []
                    outcome = (execution.prepare(api, lambda: deepcopy(supplied)) if cloud
                               else receiver.receive(api, lambda: deepcopy(supplied), packets.append))
                    self.assertEqual("prepared" if cloud else "dispatched", outcome["outcome"])
                    self.assertEqual([legacy], activated)
                    saved = remote.current_ledger()
                    self.assertEqual(legacy["normalRuns"], saved["normalRuns"])
                    self.assertEqual(legacy["cursor"], saved["cursor"])
                    record = saved["workItems"][0]
                    self.assertEqual(supplied, record["control"])
                    self.assertEqual(1, len(record["assignments"]))
                    self.assertEqual(supplied["revision"], record["assignments"][0]["revision"])
                    self.assertEqual("delivered", record["assignments"][0]["delivery"])
                    self.assertEqual(0 if cloud else 1, len(packets))
                    self.assertIsNone(getattr(api.transport, "memory_migration_document", None))
                    head, calls = remote.head, deepcopy(remote.calls)
                    api.ledger["workItems"][0]["control"]["scope"] = "Unauthorized replacement."
                    with self.assertRaisesRegex(ValueError, "control mismatch"):
                        api.persist()
                    self.assertEqual(head, remote.head)
                    self.assertEqual([c for c in calls if c[0] != "GET"],
                                     [c for c in remote.calls if c[0] != "GET"])

    def test_migration_does_not_authorize_unbound_new_control(self):
        for cloud in (False, True):
            with self.subTest(cloud=cloud):
                supplied, legacy = item_control(), self.legacy_with_hold()
                api, remote, activated = self.work_item_api(supplied, legacy, cloud=cloud)
                unbound = deepcopy(supplied)
                unbound["id"] = "different-item"
                packets = []
                with self.assertRaisesRegex(ValueError, "item binding changed"):
                    if cloud:
                        execution.prepare(api, lambda: deepcopy(unbound))
                    else:
                        receiver.receive(api, lambda: deepcopy(unbound), packets.append)
                self.assertEqual([legacy], activated)
                self.assertEqual(legacy, remote.current_ledger())
                self.assertEqual(legacy, api.ledger)
                self.assertEqual([], packets)
                self.assertEqual(1, len(remote.writes))
                self.assertEqual(0, api.storage_revision.generation)
                self.assertIsNone(getattr(api.transport, "memory_migration_document", None))

    def test_migration_capability_is_exact_and_cleared_after_success_denial_or_crash(self):
        for fault in (None, "denied", "crash"):
            with self.subTest(fault=fault):
                legacy = self.legacy_with_hold()
                api, remote, activated = self.work_item_api(item_control(), legacy)
                original = remote.comments[0]["body"]
                remote.append_fault = fault
                append = memory.BranchMemory.append

                def checked_append(branch, saved, raw, before):
                    self.assertEqual(raw, api.transport.memory_migration_document)
                    value = storage.parse_document(raw, storage.identity(api), storage.digest(original.encode()))
                    altered = deepcopy(value["ledger"])
                    altered["cursor"] += 1
                    previous = api.transport.memory_request
                    try:
                        for ledger, generation in ((altered, 0), (value["ledger"], 1)):
                            candidate = storage.document(value["authority"], ledger, value["migrationDigest"], generation)
                            api.transport.memory_request = (
                                "POST", branch.prefix + "/git/blobs",
                                {"encoding": "base64", "content": memory.encode(candidate)})
                            with self.assertRaisesRegex(ValueError, "migration document changed"):
                                api.transport.validate_endpoint(*api.transport.memory_request)
                    finally:
                        api.transport.memory_request = previous
                    return append(branch, saved, raw, before)

                with patch.object(memory.BranchMemory, "append", checked_append):
                    if fault is None:
                        api.initialize_authority()
                    else:
                        error = KeyboardInterrupt if fault == "crash" else pilot_github.AuthorityUncertain
                        with self.assertRaises(error):
                            api.initialize_authority()
                self.assertEqual([legacy] if fault is None else [], activated)
                self.assertEqual(legacy, remote.current_ledger())
                self.assertIsNone(api.transport.memory_migration_document)
                self.assertIsNone(api.transport.memory_request)
                branch = storage.store(api, write=True)
                saved = branch.read()
                calls = deepcopy(remote.calls)
                # A later direct append cannot reuse migration's control exemption.
                with self.assertRaises(memory.MemoryUncertain):
                    branch.append(saved, saved.document, lambda: None)
                self.assertEqual(saved.commit, remote.head)
                self.assertEqual([c for c in calls if c[0] != "GET"],
                                 [c for c in remote.calls if c[0] != "GET"])

    def test_full_history_activation_pointer_and_restart_noop(self):
        api = self.api()
        api.read_authority()
        state.adopt(api.ledger, 7, "issue", "ISSUE7")
        api.persist()
        original, body = deepcopy(api.ledger), api.authority_body()
        api.initialize_authority()
        self.assertEqual(original, api.ledger)
        self.assertEqual(original, api.transport.current_ledger())
        self.assertEqual(storage.digest(body.encode()),
                         storage.parse_pointer(api.authority_body(), storage.identity(api))["migrationDigest"])
        self.assertLessEqual(len(api.authority_body().encode()), storage.MAX_POINTER)
        restarted = self.api(api.transport)
        restarted.read_authority()
        self.assertEqual(api.storage_revision, restarted.storage_revision)
        before = deepcopy(api.transport.calls)
        api.persist()
        restarted.persist()
        self.assertEqual([call for call in before if call[0] != "GET"],
                         [call for call in api.transport.calls if call[0] != "GET"])

    def test_constructor_never_writes_and_activation_requires_installed_guard_and_grant(self):
        for change in ("grant", "guard", "readonly", "scope", "dirty", "policy"):
            remote = MemoryRemote()
            api = self.api(remote)
            self.assertEqual([], remote.calls)
            api.read_authority()
            body = api.authority_body()
            if change == "grant":
                api.memory_authorized = False
            elif change == "guard":
                del api.normal_guard
            elif change == "readonly":
                api.write = False
            elif change == "scope":
                api._normal_followup = {}
            elif change == "dirty":
                api.ledger["cursor"] = 1
            else:
                def reject():
                    raise ValueError("host disabled")
                api.normal_guard = reject
            with self.subTest(change=change), self.assertRaises(ValueError):
                storage.activate(api)
            self.assertEqual(body, api.authority_body())
            self.assertEqual([], remote.calls)

    def test_permission_read_does_not_prove_append_and_never_activates_on_denial(self):
        for fault in ("denied", "unreadable", "wrong-parent"):
            remote = MemoryRemote()
            api = self.api(remote)
            body = api.authority_body()
            remote.append_fault = fault
            with self.subTest(fault=fault), self.assertRaises(pilot_github.AuthorityUncertain):
                api.initialize_authority()
            self.assertEqual(body, api.authority_body())
            self.assertEqual([], remote.writes)
            self.assertEqual(2, sum(call == ["POST", "graphql"] for call in remote.calls))

    def test_branch_before_pointer_crash_reuses_only_exact_candidate(self):
        for drift in (False, True):
            remote = MemoryRemote()
            api = self.api(remote)
            body = api.authority_body()
            remote.append_fault = "crash"
            with self.assertRaises(KeyboardInterrupt):
                api.initialize_authority()
            self.assertEqual(body, api.authority_body())
            self.assertIsNotNone(remote.head)
            if drift:
                ledger = state.parse(body)
                ledger["cursor"] = 1
                remote.comments[0]["body"] = state.render(ledger)
            fresh = self.api(remote)
            if drift:
                with self.assertRaisesRegex(ValueError, "exact pending"):
                    fresh.initialize_authority()
                self.assertEqual([], remote.writes)
            else:
                fresh.initialize_authority()
                self.assertEqual(state.parse(body), fresh.ledger)
                self.assertEqual(1, len(remote.writes))

    def test_pointer_patch_lost_response_and_process_crash_recover_without_republication(self):
        for fault in ("lost", "crash"):
            remote = MemoryRemote()
            api = self.api(remote)
            remote.patch_fault = fault
            if fault == "crash":
                with self.assertRaises(KeyboardInterrupt):
                    api.initialize_authority()
            else:
                api.initialize_authority()
            before = deepcopy(remote.calls)
            fresh = self.api(remote)
            fresh.initialize_authority()
            self.assertEqual(state.new_ledger(), fresh.ledger)
            self.assertEqual(1, len(remote.writes))
            self.assertEqual([c for c in before if c[0] != "GET"], [c for c in remote.calls if c[0] != "GET"])

    def test_revision_drift_even_identical_ledger_and_changed_pointer_fail_closed(self):
        api = self.api()
        api.initialize_authority()
        revision = api.storage_revision
        branch = storage.store(api, write=True)
        branch.append(revision.snapshot, revision.snapshot.document, lambda: None)
        with self.assertRaisesRegex(ValueError, "authority changed"):
            api.authority_guard()
        api.ledger["cursor"] = 1
        before = deepcopy(api.transport.calls)
        with self.assertRaises(ValueError):
            api.persist()
        self.assertEqual([c for c in before if c[0] != "GET"], [c for c in api.transport.calls if c[0] != "GET"])
        api.transport.comments[0]["body"] = api.transport.comments[0]["body"].replace('"fork"', '"other"')
        with self.assertRaises(ValueError):
            api.read_authority()

    def test_pointer_identity_and_future_schema_rejected_before_network(self):
        api = self.api()
        api.initialize_authority()
        pointer = storage.parse_pointer(api.authority_body(), storage.identity(api))
        for key, value in (("storageVersion", True), ("storageVersion", 2), ("branch", "refs/heads/main"),
                           ("path", "https://other.invalid/ledger"), ("activationCommit", "invalid"),
                           ("migrationDigest", "f" * 63)):
            modified = deepcopy(pointer)
            modified[key] = value
            before = len(api.transport.calls)
            with self.subTest(key=key), self.assertRaises(ValueError):
                storage.parse_pointer(storage.HEADER + storage.canonical(modified).decode(), storage.identity(api))
            self.assertEqual(before, len(api.transport.calls))
        for key in storage.AUTHORITY_KEYS:
            modified = deepcopy(pointer)
            modified["authority"][key] = None
            with self.subTest(identity=key), self.assertRaises(ValueError):
                storage.parse_pointer(storage.HEADER + storage.canonical(modified).decode(), storage.identity(api))

    def test_document_strict_canonical_types_digest_and_exact_boundary(self):
        api = self.api()
        authority = storage.identity(api)
        ledger = state.new_ledger()
        raw = storage.document(authority, ledger, "a" * 64, 0)
        self.assertEqual(ledger, storage.parse_document(raw, authority, "a" * 64)["ledger"])
        with patch.object(memory, "MAX_BYTES", len(raw)):
            self.assertEqual(raw, storage.document(authority, ledger, "a" * 64, 0))
        with patch.object(memory, "MAX_BYTES", len(raw) - 1), self.assertRaises(ValueError):
            storage.document(authority, ledger, "a" * 64, 0)
        for invalid in (raw + b" ", raw.replace(b'"generation":0', b'"generation":true'),
                        raw.replace(b'"generation":0', b'"generation":0,"generation":0'),
                        raw.replace(b'"cursor":0', b'"cursor":NaN'), b"\xff"):
            with self.subTest(raw_size=len(invalid)), self.assertRaises((ValueError, UnicodeError)):
                storage.parse_document(invalid, authority, "a" * 64)
        altered = json.loads(raw)
        altered["ledger"]["cursor"] = 1
        with self.assertRaisesRegex(ValueError, "corrupt"):
            storage.parse_document(storage.canonical(altered), authority, "a" * 64)

    def test_controller_destination_is_independent_of_source_and_supports_production_identity(self):
        api = self.api()
        api.repository, api.repository_id = "microsoft/aspire", 696529789
        self.assertEqual("radical/aspire", storage.store(api).repository)
        api.controller_repository, api.controller_repository_id = "microsoft/aspire", 696529789
        branch = storage.store(api)
        self.assertEqual("microsoft/aspire", branch.repository)
        self.assertEqual("authorities/696529789/99-500-fork.json", branch.path)

    def test_host_artifacts_above_packet_bound_are_private_and_not_authority(self):
        envelope = {"packet": {"small": True}, "host": "h" * 300000}
        path = self.work / "envelope.json"
        contracts.write_envelope(path, envelope)
        self.assertEqual(envelope, contracts.read_envelope(path))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        with self.assertRaises(ValueError):
            contracts.read_json(path)
        preview = self.work / "authority-preview.json"
        storage.write_host_json(preview, envelope, max_bytes=memory.MAX_BYTES)
        self.assertEqual(envelope, storage.read_host_json(preview, max_bytes=memory.MAX_BYTES))
        link = self.work / "link.json"
        link.symlink_to(preview.resolve())
        with self.assertRaises((OSError, ValueError)):
            storage.read_host_json(link, max_bytes=memory.MAX_BYTES)
        with self.assertRaises(ValueError):
            contracts.write_envelope(self.work / "large.json", {"host": "h" * storage.MAX_ENVELOPE})
        self.assertFalse((self.work / "large.json").exists())
        with self.assertRaises(ValueError):
            contracts.write_packet(self.work / "packet.json", envelope)
        self.assertFalse((self.work / "packet.json").exists())

    def normal_fixture(self):
        fixture = normal_tests.NormalLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.terminal()
        original = seed_full_context(fixture)
        enable(fixture)
        return fixture, original

    def test_actual_shaped_49903_full_ledger_preserved_and_large_branch_assessment_report_settles(self):
        fixture, original = self.normal_fixture()
        fixture.policy(allow_cloud_task=False, allow_branch_writes=False,
                       allow_draft_pr=False, track_linked_pr=False)
        api = pilot.hosted_api(RUN, os.environ)
        api.initialize_authority()
        self.assertEqual(original, fixture.saved())
        self.assertEqual(49903, len(state.render(original).encode()))
        extra = deepcopy(api.ledger["normalRuns"][-1])
        extra.update(id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", packetId="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                     item="SECOND-HISTORY")
        api.ledger["normalRuns"].append(extra)
        api.persist()
        self.assertGreater(storage.size(api, api.ledger), state.MAX_BODY)
        with self.assertRaisesRegex(ValueError, "body bound"):
            state.render(api.ledger)
        retained = deepcopy(api.ledger["normalRuns"][:3])
        packet = fixture.prepare()
        self.assertEqual("issue-assessment", packet["normal"])
        receipt = fixture.settle(packet, fixture.assessment(packet), usage=2)
        self.assertEqual("commented", receipt["report"]["outcome"])
        settled = fixture.saved()
        self.assertEqual(retained, settled["normalRuns"][:3])
        self.assertEqual(2, settled["normalRuns"][-1]["nativeActual"])
        self.assertEqual(0, settled["normalRuns"][-1]["nativeReserved"])
        pointer = fixture.remote.comments[0]["body"]
        before = deepcopy(settled)
        before_mutations = sum(method != "GET" for method, endpoint in fixture.remote.calls)
        self.assertIsNone(fixture.prepare())
        self.assertIsNone(fixture.prepare())
        self.assertEqual(before, fixture.saved())
        self.assertEqual(pointer, fixture.remote.comments[0]["body"])
        self.assertEqual(before_mutations, sum(method != "GET" for method, endpoint in fixture.remote.calls))
        self.assertEqual(1, len(fixture.issue_posts))
        self.assertEqual(1, len(fixture.posts))

    def test_unknown_assessment_preserves_hold_without_inference_replay(self):
        fixture, original = self.normal_fixture()
        packet = fixture.prepare()
        before = deepcopy(fixture.saved())
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertIsNone(fixture.prepare())
        self.assertIsNone(fixture.prepare())
        after = fixture.saved()
        self.assertEqual(before["normalRuns"], after["normalRuns"])
        self.assertEqual(state.repository_spend(before, fixture.clock()), state.repository_spend(after, fixture.clock()))
        self.assertEqual(original["normalRuns"][:2], after["normalRuns"][:2])
        self.assertEqual([], fixture.issue_posts)

    def test_assessment_admission_past_legacy_comment_limit(self):
        fixture, original = self.normal_fixture()
        api = pilot.hosted_api(RUN, os.environ)
        api.initialize_authority()
        extra = deepcopy(api.ledger["normalRuns"][-1])
        extra.update(id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", packetId="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                     item="SECOND-HISTORY")
        api.ledger["normalRuns"].append(extra)
        api.persist()
        packet = fixture.prepare()
        self.assertIsNotNone(packet, "full branch history must not exhaust the comment-only capacity gate")
        self.assertEqual("issue-assessment", packet["normal"])
        receipt = fixture.settle(packet, fixture.assessment(packet))
        self.assertEqual("assessed", receipt["outcome"])
        self.assertEqual(original["normalRuns"][:2], fixture.saved()["normalRuns"][:2])

    def test_actual_shaped_49903_admission_recovers_without_compaction(self):
        fixture, original = self.normal_fixture()
        packet = fixture.prepare()
        self.assertIsNotNone(packet, "49903-byte full history must admit the real assessment reservation")
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertEqual(original["normalRuns"][:2], fixture.saved()["normalRuns"][:2])
        settled = fixture.settle(packet, fixture.assessment(packet), usage=2)
        self.assertEqual("assessed", settled["outcome"])
        self.assertEqual(original["normalRuns"][:2], fixture.saved()["normalRuns"][:2])

    def test_precise_assessment_reservation_threshold_equality_and_one_byte_below(self):
        for below in (False, True):
            with self.subTest(below=below):
                api = self.api()
                api.initialize_authority()
                context = {"issueDigest": "a" * 64, "issue": {"identity": {
                    "repository": "radical/aspire", "number": 7, "node_id": "ISSUE7"}},
                    "taskVersion": "TASK1", "mapping": {}, "resultDigest": "b" * 64}
                before = deepcopy(api.ledger)
                with patch.object(api, "persist"):
                    normal_loop.reserve(api, RUN, api.clock(), "ITEM7", "issue-assessment", context)
                boundary = storage.size(api, api.ledger) + 10000
                api.ledger = deepcopy(before)
                head, calls = api.transport.head, deepcopy(api.transport.calls)
                with patch.object(storage, "limit", return_value=boundary - int(below)):
                    if below:
                        with self.assertRaisesRegex(ValueError, "settlement capacity"):
                            normal_loop.reserve(api, RUN, api.clock(), "ITEM7", "issue-assessment", context)
                        self.assertEqual(head, api.transport.head)
                        self.assertEqual([c for c in calls if c[0] != "GET"],
                                         [c for c in api.transport.calls if c[0] != "GET"])
                        self.assertEqual([], api.ledger["normalRuns"])
                    else:
                        packet = normal_loop.reserve(api, RUN, api.clock(), "ITEM7", "issue-assessment", context)
                        self.assertEqual("issue-assessment", packet["normal"])
                        self.assertEqual(boundary, len(api.storage_revision.snapshot.document) + 10000)
                        self.assertNotEqual(head, api.transport.head)

    def test_fresh_processes_recover_exact_lost_report_receipt_and_never_duplicate(self):
        fixture = normal_tests.NormalLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for crash in (False, True):
            with self.subTest(crash=crash):
                path = fixture.root / f"memory-process-{crash}.json"
                run = lambda command: fixture.process(path, command)
                run("prepare-feed")
                run("settle-intake")
                run("approve")
                run("prepare")
                run("terminal")
                activation = run("activate-memory")
                self.assertEqual(49903, activation["legacyBytes"])
                activated = json.loads(path.read_text())
                run("grant-report")
                packet = run("prepare")
                self.assertEqual("issue-assessment", packet["normal"])
                if crash:
                    self.assertEqual("interrupted-after-post", run("settle-crash-after-post")["outcome"])
                else:
                    self.assertEqual("commented", run("settle-assessment")["report"]["outcome"])
                self.assertIsNone(run("prepare"))
                stable = json.loads(path.read_text())
                self.assertIsNone(run("prepare"))
                again = json.loads(path.read_text())
                self.assertEqual(stable["memory"]["head"], again["memory"]["head"])
                self.assertEqual(activated["authority"], again["authority"])
                self.assertEqual(1, len(again["posts"]))
                self.assertEqual(1, len(again["issue_posts"]))
                def ledger(saved):
                    pointer = json.loads(saved["authority"][len(storage.HEADER):])
                    remote = MemoryRemote()
                    remote.objects, remote.head = saved["memory"]["objects"], saved["memory"]["head"]
                    import base64
                    blob = remote.files(remote.head)[pointer["path"]]
                    return json.loads(base64.b64decode(remote.objects[blob]["value"]["content"]))["ledger"]
                self.assertEqual(ledger(stable), ledger(again))
                record = ledger(again)["workItems"][0]
                self.assertEqual("delivered", record["comment"]["delivery"])
                self.assertEqual(8000, record["comment"]["comment_id"])
                self.assertEqual(1, len(record["assignments"]))

    def test_fresh_process_unknown_reservation_keeps_same_native_hold(self):
        fixture = normal_tests.NormalLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path = fixture.root / "unknown-memory-process.json"
        run = lambda command: fixture.process(path, command)
        for command in ("prepare-feed", "settle-intake", "approve", "prepare", "terminal", "activate-memory"):
            run(command)
        packet = run("prepare")
        self.assertEqual("issue-assessment", packet["normal"])
        reserved = json.loads(path.read_text())
        self.assertIsNone(run("prepare"))
        self.assertIsNone(run("prepare"))
        again = json.loads(path.read_text())
        remote = MemoryRemote()
        def rows(saved):
            remote.objects, remote.head = saved["memory"]["objects"], saved["memory"]["head"]
            remote.comments[0]["body"] = saved["authority"]
            return remote.current_ledger()["normalRuns"]
        self.assertEqual(rows(reserved), rows(again))
        self.assertEqual(30, rows(again)[-1]["nativeReserved"])
        self.assertIsNone(rows(again)[-1]["nativeActual"])
        self.assertEqual(1, len(again["posts"]))
        self.assertEqual([], again["issue_posts"])

    def test_maximum_assessment_and_json_escaped_report_fit_reserved_branch_capacity(self):
        fixture, original = self.normal_fixture()
        fixture.policy(allow_cloud_task=False, allow_branch_writes=False,
                       allow_draft_pr=False, track_linked_pr=False)
        packet = fixture.prepare()
        decision = fixture.assessment(packet)
        assessment = {"summary": "s" * 800, "evidence": ["e" * 1000], "unknowns": [""]}
        remaining = 2500 - len(json.dumps(assessment, separators=(",", ":")).encode())
        assessment["unknowns"][0] = "u" * remaining
        self.assertEqual(2500, len(json.dumps(assessment, separators=(",", ":")).encode()))
        decision["assessment"] = assessment
        import work_item_followup
        original_render = work_item_followup.normal_comment_body
        def maximal(*args, **kwargs):
            body = original_render(*args, **kwargs)
            return body + "x" * (4000 - len(json.dumps(body).encode()))
        with patch.object(work_item_followup, "normal_comment_body", maximal):
            receipt = fixture.settle(packet, decision, usage=2)
        self.assertEqual("commented", receipt["report"]["outcome"])
        settled = fixture.saved()
        self.assertEqual(assessment, settled["normalRuns"][-1]["decision"]["assessment"])
        saved_assignment = settled["workItems"][0]["assignments"][-1]
        self.assertEqual(assessment, saved_assignment["execution"]["followup"]["assessment"])
        self.assertEqual(4000, len(json.dumps(settled["workItems"][0]["comment"]["body"]).encode()))
        self.assertEqual(original["normalRuns"][:2], settled["normalRuns"][:2])

    def test_branch_report_zero_or_duplicate_receipts_never_recollect_or_resend(self):
        import result_collector
        for duplicate in (False, True):
            fixture, original = self.normal_fixture()
            fixture.policy(allow_cloud_task=False, allow_branch_writes=False,
                           allow_draft_pr=False, track_linked_pr=False)
            packet = fixture.prepare()
            post = pilot_github.PilotGitHub.post_comment
            def lost(api, *args):
                post(api, *args)
                raise LostResponse("successful report receipt lost")
            with patch.object(pilot_github.PilotGitHub, "post_comment", lost):
                receipt = fixture.settle(packet, fixture.assessment(packet))
            self.assertEqual("comment_uncertain", receipt["report"]["outcome"])
            endpoint = fixture.prefix + "/issues/20872/comments"
            comments = fixture.remote.values[endpoint]
            fixture.remote.values[endpoint] = comments + [comments[0] | {"id": 8001}] if duplicate else []
            with patch.object(result_collector.HostedCollector, "__call__", side_effect=AssertionError("no recollection")):
                self.assertIsNone(fixture.prepare())
                self.assertIsNone(fixture.prepare())
            self.assertEqual(1, len(fixture.issue_posts))
            self.assertEqual(1, len(fixture.posts))
            self.assertEqual("uncertain", fixture.saved()["workItems"][0]["comment"]["delivery"])
            self.assertEqual(original["normalRuns"][:2], fixture.saved()["normalRuns"][:2])

    def test_receiver_reload_preserves_original_revision_and_ownership_pins_on_rejection(self):
        import work_item_receiver
        fixture, original = self.normal_fixture()
        fixture.prepare()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        record = api.ledger["workItems"][0]
        assignment, control = record["assignments"][0], record["control"]
        old_revision, old_expected = api.storage_revision, deepcopy(api.expected)
        with api.followup_scope(control, assignment, normal_loop.source(api, 20872)):
            changed = deepcopy(api.ledger)
            changed["normalRuns"][0]["context"]["issueDigest"] = "f" * 64
            descriptor = storage.parse_pointer(api.authority_body(), storage.identity(api))
            raw = storage.document(storage.identity(api), changed, descriptor["migrationDigest"],
                                   old_revision.generation + 1)
            storage.store(api, write=True).append(old_revision.snapshot, raw, lambda: None)
            with self.assertRaisesRegex(ValueError, "intake changed"):
                work_item_receiver.load(api, lambda: deepcopy(control))
            self.assertEqual(old_revision, api.storage_revision)
            self.assertEqual(old_expected, api.expected)

    def test_item_bound_memory_transport_cannot_change_control_or_other_authority_path(self):
        from work_item_github import FollowupTransport, CloudWorkItemTransport
        fixture, original = self.normal_fixture()
        fixture.prepare()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        control = api.ledger["workItems"][0]["control"]
        descriptor = storage.parse_pointer(api.authority_body(), storage.identity(api))
        raw = storage.document(storage.identity(api), api.ledger, descriptor["migrationDigest"],
                               api.storage_revision.generation + 1)
        body = {"query": memory.APPEND, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": "radical/aspire", "branchName": memory.BRANCH},
            "expectedHeadOid": api.storage_revision.snapshot.commit,
            "fileChanges": {"additions": [{"path": storage.path_for(storage.identity(api)),
                                         "contents": memory.encode(raw)}]},
            "message": {"headline": "Persist Shepherd authority"}}}}
        for cls in (FollowupTransport, CloudWorkItemTransport):
            transport = cls("fixture", 99, 500, control)
            transport.memory_request = ("POST", "graphql", body)
            transport.validate_endpoint(*transport.memory_request)
            changed = deepcopy(api.ledger)
            changed["workItems"][0]["control"]["action"] = "pause"
            altered = deepcopy(body)
            altered["variables"]["input"]["fileChanges"]["additions"][0]["contents"] = memory.encode(
                storage.document(storage.identity(api), changed, descriptor["migrationDigest"], 1))
            transport.memory_request = ("POST", "graphql", altered)
            with self.subTest(cls=cls.__name__), self.assertRaisesRegex(ValueError, "control mismatch"):
                transport.validate_endpoint(*transport.memory_request)
            altered = deepcopy(body)
            altered["variables"]["input"]["fileChanges"]["additions"][0]["path"] = "authorities/746880239/98-501-upstream.json"
            transport.memory_request = ("POST", "graphql", altered)
            with self.assertRaises(ValueError):
                transport.validate_endpoint(*transport.memory_request)
            transport.read_only = True
            transport.memory_request = ("POST", "graphql", body)
            with self.assertRaisesRegex(ValueError, "read-only"):
                transport.validate_endpoint(*transport.memory_request)

    def test_disabled_billing_uses_activated_large_store_but_never_migrates_legacy(self):
        fixture, original = self.normal_fixture()
        packet = fixture.prepare()
        os.environ["CI_SHEPHERD_ENABLE"] = "false"
        api = pilot.hosted_api(RUN, os.environ, billing=True)
        self.assertFalse(api.memory_authorized)
        result = pilot.settle(api, packet, None, 2, fixture.clock(), billing_only=True)
        self.assertEqual("rejected", result["outcome"])
        self.assertEqual(2, fixture.saved()["normalRuns"][-1]["nativeActual"])
        self.assertEqual(0, fixture.saved()["normalRuns"][-1]["nativeReserved"])
        self.assertEqual(original["normalRuns"][:2], fixture.saved()["normalRuns"][:2])
        self.assertEqual([], fixture.issue_posts)
        # A separate legacy authority is read under disabled billing, never
        # interpreted as permission to create Git objects or activate memory.
        fixture.remote = MemoryRemote()
        fixture.remote.comments[0]["body"] = state.render(original)
        fixture.remote.values["repos/microsoft/aspire"] = fixture.repo | {"default_branch": "main"}
        legacy = pilot.hosted_api(RUN, os.environ, billing=True)
        legacy.read_authority()
        storage.initialize(legacy)
        self.assertEqual([], fixture.remote.calls)
        self.assertFalse(storage.is_pointer(legacy.authority_body()))

    def test_local_and_hosted_construction_install_guards_without_migration(self):
        import local
        import pilot_binding
        fixture, original = self.normal_fixture()
        local_api = local.LocalGitHub("gho_fixture", 99, 500, "TRACKER99", write=True,
                                     revision="b" * 40, binding=pilot_binding.UPSTREAM_ALL)
        hosted_api = pilot.hosted_api(RUN, os.environ)
        self.assertTrue(callable(local_api.normal_guard))
        self.assertTrue(callable(hosted_api.normal_guard))
        self.assertTrue(local_api.memory_authorized)
        self.assertTrue(hosted_api.memory_authorized)
        self.assertEqual([], fixture.remote.calls)
        self.assertEqual(original, fixture.saved())

    def test_uncertain_branch_write_preserves_existing_authority_uncertain_contract(self):
        api = self.api()
        api.initialize_authority()
        previous, revision = deepcopy(api.expected), api.storage_revision
        api.ledger["cursor"] = 1
        api.transport.append_fault = "unreadable"
        before = sum(call == ["POST", "graphql"] for call in api.transport.calls)
        with self.assertRaises(pilot_github.AuthorityUncertain):
            api.persist()
        self.assertEqual(previous, api.expected)
        self.assertEqual(revision, api.storage_revision)
        self.assertEqual(before + 1, sum(call == ["POST", "graphql"] for call in api.transport.calls))
