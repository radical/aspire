from copy import deepcopy
from contextlib import nullcontext, redirect_stdout
import io
import unittest
from unittest.mock import patch
from github import LostResponse

from helpers import WorkspaceTest
import pilot_state
import work_items as items
import work_item_receiver as receiver
import round as contracts
from test_work_items import control, result, validation


class Authority:
    def __init__(self, saved=None):
        self.saved = deepcopy(saved if saved is not None else pilot_state.new_ledger())
        self.ledger = deepcopy(self.saved)
        self.effects = []
        self.persist_error = False
        self.tracker, self.authority_id, self.tracker_node = 99, 500, "TRACKER99"

    def read_authority(self):
        return deepcopy(self.saved)

    def guard_work_item(self, control):
        if self.saved != self.expected:
            raise ValueError("authority changed")
        self.effects.append("guard")

    def verify_issue(self, issue):
        self.effects.append("verify")

    def persist(self):
        if self.persist_error:
            raise ValueError("persistence unavailable")
        pilot_state.render(self.ledger)
        self.saved = deepcopy(self.ledger)
        self.expected = deepcopy(self.saved)
        self.effects.append("persist")

    def issue_comments(self, issue):
        return []

    def post_comment(self, issue, body, before_send):
        before_send()
        self.effects.append(("comment", deepcopy(issue), body))
        return 42


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.control = control()
        self.api = Authority()
        self.output = []

    def receive(self, read=None, emit=None):
        return receiver.receive(self.api, read or (lambda: deepcopy(self.control)),
                                emit or self.output.append)

    def finish(self):
        self.receive()
        assignment = self.api.saved["workItems"][0]["assignments"][0]
        receiver.accept(self.api, lambda: deepcopy(self.control), result(assignment),
                        "session-1", validation(assignment))
        return assignment

    def test_restart_does_not_dispatch_again(self):
        self.assertEqual("dispatched", self.receive()["outcome"])
        self.assertEqual(1, len(self.output))
        self.assertEqual("delivered", self.api.saved["workItems"][0]["assignments"][0]["delivery"])
        self.api = Authority(self.api.saved)
        self.assertEqual("waiting", self.receive()["outcome"])
        self.assertEqual(1, len(self.output))
        self.assertEqual(1, len(self.api.saved["workItems"][0]["assignments"]))

    def test_persistence_failure_never_delivers_packet(self):
        self.api.persist_error = True
        with self.assertRaisesRegex(ValueError, "persistence"):
            self.receive()
        self.assertEqual([], self.output)
        self.assertNotIn("workItems", self.api.saved)

    def test_output_failure_retains_uncertain_delivery_without_replay(self):
        def fail(packet):
            raise OSError("output lost")
        with self.assertRaisesRegex(OSError, "output lost"):
            self.receive(emit=fail)
        self.assertEqual("delivering", self.api.saved["workItems"][0]["assignments"][0]["delivery"])
        self.api = Authority(self.api.saved)
        self.assertEqual("delivery_uncertain", self.receive()["outcome"])
        self.assertEqual([], self.output)

    def test_fresh_control_edit_before_delivery_is_known_undelivered(self):
        reads = 0
        def read():
            nonlocal reads
            reads += 1
            value = deepcopy(self.control)
            if reads >= 3:
                value.update(revision=2, requested_route="product_bug")
            return value
        with self.assertRaisesRegex(ValueError, "control changed"):
            self.receive(read)
        record = self.api.saved["workItems"][0]
        self.assertEqual("reserved", record["assignments"][0]["delivery"])
        self.assertEqual([], self.output)
        self.control.update(revision=2, requested_route="product_bug")
        self.api = Authority(self.api.saved)
        self.assertEqual("dispatched", self.receive()["outcome"])
        self.assertEqual("abandoned", self.api.saved["workItems"][0]["assignments"][0]["delivery"])
        self.assertEqual("product-bug/v1", self.output[0]["specialist"])

    def test_route_change_waits_for_checkpoint_then_dispatches_once(self):
        self.receive()
        assignment = self.api.saved["workItems"][0]["assignments"][0]
        self.control.update(revision=2, requested_route="product_bug")
        self.assertEqual("waiting", self.receive()["outcome"])
        receiver.accept(self.api, lambda: deepcopy(self.control), result(assignment), "session-1", None)
        self.assertEqual("dispatched", self.receive()["outcome"])
        self.assertEqual(2, len(self.output))
        self.assertEqual("waiting", self.receive()["outcome"])

    def test_preview_approval_revision_emits_one_request_without_new_worker(self):
        assignment = self.finish()
        self.assertEqual("preview", self.receive()["outcome"])
        preview = self.output[-1]
        self.control.update(revision=2, action="run", pr_review={
            "assignment_id": assignment["id"], "preview_revision": preview["preview_revision"],
            "title": preview["title"], "body": preview["body"]})
        self.assertEqual("pr_requested", self.receive()["outcome"])
        self.assertEqual("draft_pr_request", self.output[-1]["kind"])
        self.assertEqual("pr_request_delivered", self.receive()["outcome"])
        self.assertEqual(1, len(self.api.saved["workItems"][0]["assignments"]))
        self.assertEqual(3, len(self.output))
        self.control["revision"] = 3
        self.assertEqual("pr_request_delivered", self.receive()["outcome"])
        self.assertEqual(3, len(self.output))

    def test_no_pr_result_comment_is_next_run_and_separately_authorized(self):
        self.receive()
        assignment = self.api.saved["workItems"][0]["assignments"][0]
        claim = result(assignment)
        claim.update(actual_classification="flaky_test_candidate", outcome="no_fix", pr_proposal=None)
        receiver.accept(self.api, lambda: deepcopy(self.control), claim, "session-1", None)
        self.assertEqual([], [entry for entry in self.api.effects if isinstance(entry, tuple)])
        self.assertEqual("human_wait", self.receive()["outcome"])
        self.assertEqual([], [entry for entry in self.api.effects if isinstance(entry, tuple)])
        self.control["revision"] = 2
        self.control["action"] = "pause"
        self.control["authority"]["issue_comment"] = True
        self.assertEqual("human_wait", self.receive()["outcome"])
        self.control.update(revision=3, action="publish")
        self.assertEqual("commented", self.receive()["outcome"])
        self.assertEqual("human_wait", self.receive()["outcome"])
        comments = [entry for entry in self.api.effects if isinstance(entry, tuple)]
        self.assertEqual(1, len(comments))
        self.assertTrue(comments[0][2].startswith("[automated] "))
        self.assertIn("current control revision: 3.", comments[0][2])
        self.assertEqual(self.control["issue"], comments[0][1])

    def test_approval_revision_cannot_clear_uncertain_publication_delivery(self):
        assignment = self.finish()
        self.receive()
        preview = self.output[-1]
        self.control.update(revision=2, pr_review={
            "assignment_id": assignment["id"], "preview_revision": preview["preview_revision"],
            "title": preview["title"], "body": preview["body"]})
        def lost(packet):
            self.output.append(packet)
            raise OSError("output escaped before error")
        with self.assertRaises(OSError):
            self.receive(emit=lost)
        original = deepcopy(self.api.saved["workItems"][0]["publication"])
        self.control["revision"] = 3
        self.api = Authority(self.api.saved)
        self.assertEqual("pr_request_uncertain", self.receive()["outcome"])
        self.assertEqual(original, self.api.saved["workItems"][0]["publication"])
        self.assertEqual(3, len(self.output))
        self.control["revision"] = 4
        self.control["authority"]["draft_pr"] = False
        self.receive()
        self.assertEqual(original, self.api.saved["workItems"][0]["publication"])
        self.control.update(revision=5, pr_review=None)
        self.assertEqual("pr_request_uncertain", self.receive()["outcome"])
        self.assertEqual(1, len(self.api.saved["workItems"][0]["assignments"]))

    def test_distinct_assignment_requires_its_own_preview_and_approval(self):
        first = self.finish()
        self.receive()
        old_preview = self.output[-1]
        self.control.update(revision=2, pr_review={
            "assignment_id": first["id"], "preview_revision": old_preview["preview_revision"],
            "title": old_preview["title"], "body": old_preview["body"]})
        self.receive()
        self.control.update(revision=3, pr_review=None, requested_route="product_bug")
        self.assertEqual("dispatched", self.receive()["outcome"])
        second = self.api.saved["workItems"][0]["assignments"][-1]
        receiver.accept(self.api, lambda: deepcopy(self.control), result(second), "session-1", validation(second))
        self.assertEqual("preview", self.receive()["outcome"])
        new_preview = self.output[-1]
        self.assertEqual(second["id"], new_preview["assignment_id"])
        self.control.update(revision=4, pr_review={
            "assignment_id": second["id"], "preview_revision": new_preview["preview_revision"],
            "title": new_preview["title"], "body": new_preview["body"]})
        self.assertEqual("pr_requested", self.receive()["outcome"])
        self.assertEqual(second["id"], self.output[-1]["assignment_id"])

    def test_new_same_route_run_bypasses_consumed_preview_without_losing_receipt(self):
        first = self.finish()
        self.receive()
        preview = self.output[-1]
        self.control.update(revision=2, pr_review={
            "assignment_id": first["id"], "preview_revision": preview["preview_revision"],
            "title": preview["title"], "body": preview["body"]})
        self.receive()
        publication = deepcopy(self.api.saved["workItems"][0]["publication"])
        self.control.update(revision=3, pr_review=None)
        self.assertEqual("dispatched", self.receive()["outcome"])
        self.assertEqual(publication, self.api.saved["workItems"][0]["publication"])
        self.assertEqual(2, len(self.api.saved["workItems"][0]["assignments"]))
        self.assertEqual("workflow-failure/v1", self.output[-1]["specialist"])
        self.assertEqual("waiting", self.receive()["outcome"])
        second = self.api.saved["workItems"][0]["assignments"][-1]
        receiver.accept(self.api, lambda: deepcopy(self.control), result(second), "session-1", validation(second))
        self.assertEqual("preview", self.receive()["outcome"])
        self.assertEqual(second["id"], self.output[-1]["assignment_id"])
        self.assertEqual(publication, self.api.saved["workItems"][0]["publication"])

    def test_publication_revocation_immediately_before_output_emits_no_request(self):
        assignment = self.finish()
        self.receive()
        preview = self.output[-1]
        self.control.update(revision=2, action="run", pr_review={
            "assignment_id": assignment["id"], "preview_revision": preview["preview_revision"],
            "title": preview["title"], "body": preview["body"]})
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            value = deepcopy(self.control)
            if calls >= 4:
                value["revision"] = 3
                value["authority"]["draft_pr"] = False
            return value
        with self.assertRaisesRegex(ValueError, "control changed"):
            self.receive(read)
        self.assertEqual(2, len(self.output))
        self.assertEqual("delivering", self.api.saved["workItems"][0]["publication"]["delivery"])

    def test_revocation_during_comment_guard_sends_nothing(self):
        self.receive()
        assignment = self.api.saved["workItems"][0]["assignments"][0]
        claim = result(assignment)
        claim.update(outcome="inconclusive", pr_proposal=None)
        receiver.accept(self.api, lambda: deepcopy(self.control), claim, "session-1", None)
        self.control.update(revision=2, action="publish")
        self.control["authority"]["issue_comment"] = True
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            changed = deepcopy(self.control)
            if calls >= 4:
                changed["revision"] += 1
                changed["authority"]["issue_comment"] = False
            return changed
        with self.assertRaisesRegex(ValueError, "control changed"):
            self.receive(read)
        self.assertEqual([], [entry for entry in self.api.effects if isinstance(entry, tuple)])

    def test_lost_comment_response_holds_intent_and_reconciles_without_second_post(self):
        self.control["authority"]["issue_comment"] = True
        self.receive()
        assignment = self.api.saved["workItems"][0]["assignments"][0]
        claim = result(assignment)
        claim.update(outcome="inconclusive", pr_proposal=None)
        receiver.accept(self.api, lambda: deepcopy(self.control), claim, "session-1", None)
        posted = []
        def post(issue, body, before_send):
            before_send()
            posted.append(body)
            raise LostResponse("connection lost after send")
        self.api.post_comment = post
        self.assertEqual("comment_uncertain", self.receive()["outcome"])
        self.assertEqual("comment_uncertain", self.receive()["outcome"])
        self.api.issue_comments = lambda _: [{"id": 42, "body": posted[0], "owned": True}]
        self.assertEqual("human_wait", self.receive()["outcome"])
        self.assertEqual(1, len(posted))
        self.assertEqual(42, self.api.saved["workItems"][0]["comment"]["comment_id"])


class ReceiverCliTests(WorkspaceTest, unittest.TestCase):
    def test_json_receiver_and_checkpoint_reenter_without_another_packet(self):
        value = control()
        path = self.work / "item.json"
        contracts.write_json(path, value)
        api = Authority()
        common = ["--item", str(path), "--tracker", "99", "--authority", "500",
                  "--tracker-node", "TRACKER99"]
        with patch("work_item_github.WorkItemGitHub", return_value=api), \
                patch.object(receiver.local, "selected_token", return_value="fixture"), \
                patch.object(receiver.local, "command", return_value="c" * 40), \
                patch.object(receiver.local, "require_source"), \
                patch.object(receiver.local, "require_idle_actions"), \
                patch.object(receiver.local, "authority_lock", return_value=nullcontext()), \
                redirect_stdout(io.StringIO()):
            first = self.work / "first"
            self.assertEqual(0, receiver.main(["receive", *common, "--workdir", str(first)]))
            packet = contracts.read_json(first / "packet.json")
            self.assertEqual("workflow-failure/v1", packet["specialist"])
            second = self.work / "second"
            self.assertEqual(0, receiver.main(["receive", *common, "--workdir", str(second)]))
            self.assertFalse((second / "packet.json").exists())
            assignment = api.saved["workItems"][0]["assignments"][0]
            result_path = self.work / "result.json"
            contracts.write_json(result_path, result(assignment))
            checkpoint = self.work / "checkpoint"
            self.assertEqual(0, receiver.main([
                "checkpoint", *common, "--workdir", str(checkpoint),
                "--result", str(result_path), "--worker-id", "session-1"]))
            receipt = contracts.read_json(checkpoint / "receipt.json")
            self.assertEqual({"outcome": "checkpointed", "evaluated_revision": 1, "control_revision": 1}, receipt)
        self.assertEqual(1, len(api.saved["workItems"][0]["assignments"]))
