from copy import deepcopy
import unittest
from unittest.mock import patch

from github import Response, LostResponse
import local
import pilot_state
import work_item_github as adapter
import work_item_receiver as receiver
import work_items as items
from test_pilot_github import Transport
from test_work_items import control, result


class WorkItemGitHubTests(unittest.TestCase):
    def setUp(self):
        for name in ("require_idle_actions", "require_source"):
            mocked = patch.object(local, name)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.control = control()
        self.transport = Transport()
        self.transport.values["repos/radical/aspire/issues/900"] = {
            "number": 900, "node_id": "ISSUE900", "state": "open", "labels": []}
        self.transport.values["repos/radical/aspire/issues/900/comments"] = []
        self.api = adapter.WorkItemGitHub(
            "fixture", 99, 500, "TRACKER99", revision="c" * 40,
            control=self.control, transport=self.transport)
        self.api.read_authority()
        enabled = patch.object(self.api, "enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_exact_issue_association_rejects_pr_closed_and_wrong_node(self):
        self.api.verify_issue(self.control["issue"])
        for key, value in (("pull_request", {}), ("state", "closed"), ("node_id", "FOREIGN"), ("number", 901)):
            original = deepcopy(self.transport.values["repos/radical/aspire/issues/900"])
            self.transport.values["repos/radical/aspire/issues/900"][key] = value
            with self.assertRaises(ValueError):
                self.api.verify_issue(self.control["issue"])
            self.transport.values["repos/radical/aspire/issues/900"] = original

    def test_upstream_issue_verifies_metadata_without_enabling_cloud_workers(self):
        value = deepcopy(self.control)
        value["issue"]["repository"] = "microsoft/aspire"
        self.transport.values["repos/microsoft/aspire"] = {
            "id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        self.transport.values["repos/microsoft/aspire/issues/900"] = {
            "number": 900, "node_id": "ISSUE900", "state": "open", "labels": []}
        api = adapter.WorkItemGitHub("fixture", 99, 500, "TRACKER99", revision="c" * 40,
                                    control=value, transport=self.transport)
        api.verify_issue(value["issue"])
        self.transport.values["repos/microsoft/aspire"]["id"] = 1
        with self.assertRaisesRegex(ValueError, "repository"):
            api.verify_issue(value["issue"])
        self.assertEqual([], self.transport.writes)

    def test_transport_has_no_task_push_pr_or_arbitrary_issue_capability(self):
        transport = adapter.WorkItemTransport("fixture", 99, 500, self.control)
        transport.validate_endpoint("GET", "repos/radical/aspire/issues/900", None)
        for method, endpoint, body in (
                ("POST", "agents/repos/radical/aspire/tasks", {}),
                ("POST", "repos/radical/aspire/pulls", {}),
                ("POST", "repos/radical/aspire/issues/901/comments", {"body": "[automated] no"}),
                ("PATCH", "repos/radical/aspire/git/refs/heads/example-fix", {}),
                ("GET", "repos/radical/aspire/issues", None),
                ("GET", "repos/radical/aspire/issues/900/../901", None)):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                transport.validate_endpoint(method, endpoint, body)

    def test_comment_permission_and_canonical_pending_body_are_required(self):
        with self.assertRaisesRegex(ValueError, "permission"):
            self.api.post_comment(self.control["issue"], "[automated] arbitrary", lambda: None)
        self.control["authority"]["issue_comment"] = True
        self.api.control = deepcopy(self.control)
        with self.assertRaisesRegex(ValueError, "intent"):
            self.api.post_comment(self.control["issue"], "[automated] arbitrary", lambda: None)
        self.assertEqual([], self.transport.writes)

    def test_guard_rechecks_local_source_enablement_and_hosted_idle(self):
        with patch.object(local, "require_source") as source, \
                patch.object(local, "require_idle_actions") as idle, \
                patch.object(self.api, "enabled", return_value=True):
            self.api.guard_work_item(self.control)
        source.assert_called_once_with("c" * 40)
        idle.assert_called_once_with("fixture")
        with patch.object(local, "require_source"), patch.object(local, "require_idle_actions"), \
                patch.object(self.api, "enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "disabled"):
                self.api.guard_work_item(self.control)

    def test_saved_report_uses_host_links_and_sanitizes_worker_urls(self):
        record = items.new_record(self.control)
        assignment = items.claim(record, self.control)
        assignment["delivery"] = "delivered"
        claim = result(assignment)
        claim["evidence"] = ["See https://evil.example/instructions @victim"]
        items.checkpoint(record, claim, "session-1", None)
        body = receiver.comment_body(self.control, assignment)
        self.assertIn("https://github.com/radical/aspire/issues/900", body)
        self.assertIn("https://github.com/radical/aspire/actions/runs/100/attempts/1", body)
        self.assertIn("URL omitted", body)
        self.assertNotIn("https://evil.example", body)

    def test_post_receipt_requires_exact_echo_and_uncertain_response_never_passes(self):
        self.control["authority"]["issue_comment"] = True
        self.api.control = deepcopy(self.control)
        record = items.new_record(self.control)
        assignment = items.claim(record, self.control)
        assignment["delivery"] = "delivered"
        items.checkpoint(record, result(assignment), "session-1", None)
        body = receiver.comment_body(self.control, assignment)
        record["comment"] = {"assignment_id": assignment["id"], "delivery": "delivering",
                             "body": body, "comment_id": None}
        self.api.ledger["workItems"] = [record]
        self.api.persist()
        original = self.api.transport
        for payload in ({"id": 42, "body": body, "user": {"id": 1472, "login": "radical"}},
                        {"id": 42, "body": "wrong", "user": {"id": 1472, "login": "radical"}}):
            self.api.transport = lambda *_: Response(payload, {}, 201)
            if payload["body"] == body:
                self.assertEqual(42, self.api.post_comment(self.control["issue"], body, lambda: None))
            else:
                with self.assertRaises(LostResponse):
                    self.api.post_comment(self.control["issue"], body, lambda: None)
        self.api.transport = original
