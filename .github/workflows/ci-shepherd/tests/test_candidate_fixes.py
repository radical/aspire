"""Candidate discovery, known-unsent admission and observation-only reporting."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from github import IncompleteInventory, LostResponse, Response
import candidate_fixes as candidates
import live
import pilot_state as state
import work_item_execution as execution
import work_item_github
from test_pilot_github import Transport, pr
from test_work_item_execution import CloudAuthority
from test_work_items import control


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.control = control()
        self.control["authority"]["issue_comment"] = True
        self.read = lambda: deepcopy(self.control)
        self.api = CloudAuthority()
        self.approval = execution.prepare(self.api, self.read)["approval_template"]
        self.record = self.api.ledger["workItems"][0]
        self.remote = Transport()
        self.api.api = live.API(self.remote)
        self.api.transport = self.remote
        self.api.observe_candidates = lambda record: candidates.scan(self.api, record)
        self.api.guard_candidates = self.api.guard_work_item
        self.api.owned = lambda comment: comment.get("user", {}).get("id") == 1472
        self.prefix = "repos/radical/aspire"
        self.timeline = self.prefix + "/issues/900/timeline"
        self.remote.values[self.timeline] = []
        self.comments = self.prefix + "/issues/900/comments"
        self.remote.values[self.comments] = []
        self.posted = []
        self.lose = False

        def transport(method, endpoint, body):
            if method == "POST" and endpoint == self.comments:
                self.posted.append(deepcopy(body))
                value = {"id": len(self.posted), "body": body["body"], "user": {"id": 1472}}
                self.remote.values[self.comments].append(value)
                if self.lose:
                    raise LostResponse("lost")
                return Response(value, {}, 201)
            return self.remote(method, endpoint, body)
        # A callable wrapper also exposes the exact notice capability.
        class Reporting:
            candidate_notice = None

            def __call__(self, method, endpoint, body):
                return transport(method, endpoint, body)
        self.api.transport = Reporting()

    def ref(self, number, *, pull=True):
        issue = {"number": number, "node_id": f"NODE{number}",
                 "url": f"https://api.github.com/{self.prefix}/issues/{number}"}
        if pull:
            issue["pull_request"] = {"url": f"https://api.github.com/{self.prefix}/pulls/{number}"}
        return {"event": "cross-referenced", "source": {"issue": issue}}

    def pull(self, number=7, via=900):
        value = pr(number)
        value.update(node_id=f"NODE{number}", state="open", merged=False, body=f"Fixes #{via}")
        value["base"].update(ref="main", repo={"full_name": "radical/aspire"})
        # External fork heads are legitimate candidates, not managed branches.
        value["head"]["repo"] = {"full_name": "someone/aspire"}
        self.remote.values[f"{self.prefix}/pulls/{number}"] = value
        self.remote.values[f"{self.prefix}/pulls/{number}/closing-issues"] = [
            {"number": via, "repository": {"nameWithOwner": "radical/aspire"}}]
        self.remote.values[self.timeline].append(self.ref(number))
        return value

    def refresh(self):
        value = candidates.refresh(self.api, self.record)
        self.api.persist()
        return value

    def test_existing_fix_blocks_task_and_releases_capacity_across_reentry(self):
        self.pull()
        for _ in range(2):
            with self.assertRaisesRegex(execution.AdmissionDeferred, "Candidate fix"):
                execution.execute(self.api, self.read, lambda: self.approval)
        self.assertEqual([], self.api.posts)
        self.assertEqual(0, state.worker_slots(self.api.saved))
        self.assertEqual(0, state.repository_spend(self.api.saved, execution.live.clock()))
        self.assertEqual(1, len(self.api.saved["workItems"][0]["assignments"]))

    def test_final_guard_candidate_is_definitively_unsent_not_uncertain(self):
        start = self.api.start_task
        def late(body, before_send):
            self.pull()
            return start(body, before_send)
        with patch.object(self.api, "start_task", side_effect=late):
            with self.assertRaises(execution.AdmissionDeferred):
                execution.execute(self.api, self.read, lambda: self.approval)
        saved = self.api.saved["workItems"][0]["assignments"][0]["execution"]
        self.assertEqual(("reserved", 0, None), (saved["state"], saved["worker_reserved"], saved["task_id"]))
        self.assertEqual([], self.api.posts)

    def test_related_tracker_link_and_multiple_candidates(self):
        self.remote.values[self.timeline] = [self.ref(901, pull=False)]
        self.remote.values[f"{self.prefix}/issues/901"] = {
            "number": 901, "node_id": "NODE901", "body": "#900 is the incident.\n<!-- failing-test-signature: v1:abc -->"}
        self.pull(7, via=901)
        self.pull(8, via=901)
        self.remote.values[f"{self.prefix}/issues/901/timeline"] = [self.ref(7), self.ref(8)]
        self.assertEqual([(7, 901), (8, 901)], [(p["number"], p["via"]) for p in self.refresh()["pulls"]])

    def test_native_relationship_not_untrusted_prose_or_foreign_reference_controls_tracking(self):
        pull = self.pull()
        self.remote.values[f"{self.prefix}/pulls/7/closing-issues"] = []
        for text in ("Refs #900", "> Fixes #900", "```\nFixes #900\n```", "Fixes someone/aspire#900"):
            pull["body"] = text
            with self.subTest(text=text):
                self.assertEqual([], self.refresh()["pulls"])
        self.remote.values[f"{self.prefix}/pulls/7/closing-issues"] = [
            {"number": 900, "repository": {"nameWithOwner": "other/repo"}}]
        self.assertEqual([], self.refresh()["pulls"])
        self.remote.values[self.timeline][0]["source"]["issue"]["url"] = "https://api.github.com/repos/other/repo/issues/7"
        pull["body"] = "Fixes #900"
        self.assertEqual([], self.refresh()["pulls"])

    def test_closed_unlinked_and_merged_candidates_stay_on_hold(self):
        pull = self.pull()
        self.refresh()
        pull.update(state="closed", merged=True, body="")
        self.remote.values[self.timeline] = []
        value = self.refresh()
        self.assertEqual((False, "closed", True),
                         (value["pulls"][0]["linked"], value["pulls"][0]["state"], value["pulls"][0]["merged"]))
        self.assertTrue(candidates.blocks(value))

    def test_incomplete_discovery_preserves_history_and_blocks_launch(self):
        self.pull()
        self.refresh()
        with patch.object(self.api.api, "pages", side_effect=IncompleteInventory("pagination incomplete")):
            value = self.refresh()
        self.assertFalse(value["complete"])
        self.assertEqual(7, value["pulls"][0]["number"])
        self.assertIn("pagination incomplete", value["attention"])
        self.assertTrue(candidates.blocks(value))

    def test_native_closing_query_is_bounded_and_fails_closed(self):
        self.pull()
        original = self.api.transport
        def incomplete(method, endpoint, body):
            response = original(method, endpoint, body)
            if endpoint == "graphql":
                response.payload["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["pageInfo"][
                    "hasNextPage"] = True
            return response
        self.api.transport = incomplete
        value = self.refresh()
        self.assertFalse(value["complete"])
        self.assertIn("completeness mismatch", value["attention"])
        self.assertTrue(candidates.blocks(value))

    def test_arbitrary_related_issue_is_not_a_test_tracker(self):
        self.remote.values[self.timeline] = [self.ref(901, pull=False)]
        self.remote.values[f"{self.prefix}/issues/901"] = {
            "number": 901, "node_id": "NODE901", "body": "#900 is mentioned."}
        self.pull(7, via=901)
        self.remote.values[f"{self.prefix}/issues/901/timeline"] = [self.ref(7)]
        self.assertEqual([], self.refresh()["pulls"])

    def test_notice_transition_back_to_prior_inventory_does_not_repeat(self):
        pull = self.pull()
        self.refresh()
        candidates.publish(self.api, self.record)
        pull["state"] = "closed"
        self.refresh()
        candidates.publish(self.api, self.record)
        pull["state"] = "open"
        self.refresh()
        candidates.publish(self.api, self.record)
        self.assertEqual(2, len(self.posted))

    def test_changed_inventory_before_notice_sends_only_fresh_inventory(self):
        self.pull()
        self.refresh()
        self.api.guard_candidates = lambda control: self.pull(8)
        candidates.publish(self.api, self.record)
        self.assertEqual([], self.posted)
        self.assertEqual([], self.record["candidate_fixes"]["notices"])
        self.api.guard_candidates = self.api.guard_work_item
        candidates.publish(self.api, self.record)
        self.assertEqual(1, len(self.posted))
        self.assertIn("/pull/8", self.posted[0]["body"])

    def test_deleted_fork_head_repository_is_still_observable(self):
        self.pull()["head"]["repo"] = None
        self.assertEqual([7], [pull["number"] for pull in self.refresh()["pulls"]])

    def test_candidate_hold_does_not_starve_another_issue(self):
        self.pull()
        with self.assertRaises(execution.AdmissionDeferred):
            execution.execute(self.api, self.read, lambda: self.approval)
        other = deepcopy(self.control)
        other.update(id="other-item")
        other["issue"] = dict(other["issue"], number=901, node_id="ISSUE901")
        self.remote.values[self.prefix + "/issues/901/timeline"] = []
        approval = execution.prepare(self.api, lambda: other)["approval_template"]
        result = execution.execute(self.api, lambda: other, lambda: approval)
        self.assertEqual("sent", result["outcome"])
        self.assertEqual(1, len(self.api.posts))

    def test_notices_do_not_repeat_on_rerun_or_head_push(self):
        pull = self.pull()
        self.refresh()
        candidates.publish(self.api, self.record)
        pull["head"]["sha"] = "b" * 40
        self.refresh()
        candidates.publish(self.api, self.record)
        self.assertEqual(1, len(self.posted))
        self.assertEqual("delivered", self.api.saved["workItems"][0]["candidate_fixes"]["notices"][0]["state"])
        self.assertIn("observation only", self.posted[0]["body"])

    def sibling(self):
        record = deepcopy(self.record)
        record["id"] = record["control"]["id"] = "retry-item"
        record["assignments"] = []
        record["candidate_fixes"]["notices"] = []
        self.api.ledger["workItems"].append(record)
        self.api.persist()
        return record

    def test_sibling_inventory_is_combined_once_with_ownership_preserved(self):
        self.pull(7)
        self.pull(8)
        self.pull(9)
        self.refresh()
        self.record["candidate_fixes"]["pulls"][0]["own"] = True
        sibling = self.sibling()
        sibling["candidate_fixes"]["pulls"][0]["own"] = False
        sibling["candidate_fixes"]["pulls"][1]["own"] = True
        self.api.observe_candidates = lambda record: deepcopy(record["candidate_fixes"])
        self.api.persist()
        for _ in range(2):
            candidates.publish(self.api, sibling)
            candidates.publish(self.api, self.record)
        self.assertEqual(1, len(self.posted))
        self.assertEqual(candidates.PREFIX + "\n"
                         "<!-- ci-shepherd:candidates:radical/aspire:900:ISSUE900 -->\n"
                         "- [#7](https://github.com/radical/aspire/pull/7): open, our task.\n"
                         "- [#8](https://github.com/radical/aspire/pull/8): open, our task.\n"
                         "- [#9](https://github.com/radical/aspire/pull/9): open, observation only.\n"
                         "No additional Copilot repair will be launched. Already-started work is not cancelled.\n"
                         "External PR branches are not managed by Shepherd. This is not an issue-resolution claim.",
                         self.posted[0]["body"])
        self.assertEqual([], sibling["candidate_fixes"]["notices"])
        self.record["control"]["authority"]["issue_comment"] = False
        self.api.persist()
        candidates.publish(self.api, sibling)
        self.assertEqual(1, len(self.posted))

    def test_uncertain_legacy_sibling_notice_blocks_new_issue_notice(self):
        self.pull()
        self.refresh()
        sibling = self.sibling()
        sibling["candidate_fixes"]["notices"].append({
            "body": candidates.body(sibling), "state": "uncertain", "id": None})
        self.api.persist()
        candidates.publish(self.api, self.record)
        self.assertEqual([], self.posted)
        self.assertIn("outcome unknown", self.record["candidate_fixes"]["attention"])

    def test_sibling_control_change_before_post_blocks_publication(self):
        self.pull()
        self.refresh()
        sibling = self.sibling()
        def change(control):
            self.api.saved["workItems"][-1]["control"]["action"] = "pause"
        self.api.guard_candidates = change
        with self.assertRaisesRegex(ValueError, "notice authority changed"):
            candidates.publish(self.api, self.record)
        self.assertEqual([], self.posted)
        self.assertEqual("run", sibling["control"]["action"])

    def test_lost_notice_reconciles_before_reporting_new_inventory(self):
        self.pull()
        self.refresh()
        self.lose = True
        candidates.publish(self.api, self.record)
        self.assertEqual("uncertain", self.record["candidate_fixes"]["notices"][0]["state"])
        self.lose = False
        self.pull(8)
        self.refresh()
        candidates.publish(self.api, self.record)
        self.assertEqual(2, len(self.posted))
        self.assertEqual(["delivered", "delivered"],
                         [n["state"] for n in self.record["candidate_fixes"]["notices"]])

    def test_missing_or_duplicate_lost_notice_never_reposts(self):
        self.pull()
        self.refresh()
        self.lose = True
        candidates.publish(self.api, self.record)
        self.remote.values[self.comments] = []
        candidates.publish(self.api, self.record)
        self.assertEqual(1, len(self.posted))
        self.assertIn("outcome unknown", self.record["candidate_fixes"]["attention"])

    def test_no_permission_or_revoked_permission_never_posts(self):
        self.pull()
        self.refresh()
        self.control["authority"]["issue_comment"] = False
        self.record["control"]["authority"]["issue_comment"] = False
        candidates.publish(self.api, self.record)
        self.assertEqual([], self.posted)
        self.record["control"]["authority"]["issue_comment"] = True
        self.control["authority"]["issue_comment"] = True
        self.api.normal_policy = lambda: None
        def revoke(control):
            self.api.normal_policy = lambda: {"schema_version": 1, "issue": control["issue"],
                "allow_cloud_task": True, "allow_branch_writes": True, "allow_draft_pr": True,
                "track_linked_pr": True, "issue_comment": False}
        self.api.guard_candidates = revoke
        with self.assertRaisesRegex(ValueError, "notice authority changed"):
            candidates.publish(self.api, self.record)
        self.assertEqual([], self.posted)

    def test_candidate_transport_cannot_write_external_pr_or_cancel_tasks(self):
        transport = work_item_github.CloudWorkItemTransport("fixture", 99, 500, self.control)
        transport.candidate_repository = "radical/aspire"
        transport.validate_endpoint("GET", self.prefix + "/pulls/7", None)
        query = {"query": candidates.QUERY, "variables": {"owner": "radical", "name": "aspire", "number": 7}}
        transport.validate_endpoint("POST", "graphql", query)
        self.assertTrue(transport.is_read("POST", "graphql", query))
        with self.assertRaises(ValueError):
            transport.validate_endpoint("POST", "graphql", query | {"query": "mutation { unsafe }"})
        for method, path, body in (
            ("PATCH", "/pulls/7", {"body": "edit"}),
            ("POST", "/issues/7/comments", {"body": "unauthorized"}),
            ("POST", "/git/refs", {"ref": "refs/heads/fix"}),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.validate_endpoint(method, self.prefix + path, body)
        with self.assertRaises(ValueError):
            transport.validate_endpoint("DELETE", "agents/repos/radical/aspire/tasks/TASK1", None)
        transport.candidate_notice = {"endpoint": self.comments, "body": {"body": candidates.body(self.record)
            if "candidate_fixes" in self.record else candidates.PREFIX}}
        transport.validate_endpoint("POST", self.comments, transport.candidate_notice["body"])
        with self.assertRaises(ValueError):
            transport.validate_endpoint("POST", self.comments, {"body": candidates.PREFIX + " changed"})
