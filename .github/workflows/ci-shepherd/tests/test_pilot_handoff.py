from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import base64
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from helpers import WorkspaceTest, reconciliation_evidence
from github import IncompleteInventory, LostResponse, Response
from test_pilot import RUN
import test_pilot_tracked_only as fixtures
import live
import local
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_patch
import pilot_reminders as reminders
import pilot_state as state


def annotation(message="The runner fixture lost communication with the server.", level="failure"):
    return {"path": ".github", "blob_href": "https://github.com/untrusted",
            "start_line": 1, "end_line": 1, "start_column": None, "end_column": None,
            "annotation_level": level, "title": None, "message": message, "raw_details": None}


def check(head, identity=10, conclusion="failure", annotations_count=1):
    return {"id": identity, "head_sha": head, "status": "completed", "conclusion": conclusion,
            "name": "Tests", "html_url": "https://github.com/untrusted",
            "check_suite": {"id": 100}, "output": {"annotations_count": annotations_count,
                "title": None, "summary": "Untrusted diagnostic", "text": None,
                "annotations_url": "https://evil.invalid/never-read"}}


class HandoffTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.api, self.transport = fixtures.TrackedOnlyTests().api(bindings.UPSTREAM)
        self.api.read_authority()
        self.value = self.transport.values[f"{self.api.prefix}/pulls/20722"]
        self.head = self.value["head"]["sha"]
        self.chain = state.adopt(self.api.ledger, 20722, "pr", self.value["node_id"])
        self.api.persist()
        self.transport.values[f"{self.api.prefix}/pulls/20722/comments"] = []
        self.addCleanup(self.assert_no_foreign_reads)

    def assert_no_foreign_reads(self):
        self.assertEqual([], [endpoint for method, endpoint, _ in self.transport.reads
                              if method == "GET" and ("evil.invalid" in endpoint or endpoint.endswith("/tasks"))])

    def ci(self, checks, annotations=None):
        self.transport.values[f"{self.api.prefix}/commits/{self.head}/check-runs"] = {
            "total_count": len(checks), "check_runs": checks}
        for item in checks:
            self.transport.values[f"{self.api.prefix}/check-runs/{item['id']}/annotations"] = (
                annotations if annotations is not None else [annotation()])

    def review(self, count=1, body="Repair the source"):
        self.transport.values[f"{self.api.prefix}/pulls/20722/comments"] = [
            {"id": 31 + index, "body": body, "updated_at": "2026-10-04T00:00:00Z",
             "user": {"id": 1472, "login": "radical"}, "path": "src/fixture.py", "line": 1, "commit_id": self.head}
            for index in range(count)]

    def prepare(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pilot.prepare(self.api, RUN, self.api.clock(), present=False)

    def test_positive_infrastructure_and_cancellation_wait_without_round_or_human(self):
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion=conclusion):
                self.ci([check(self.head, conclusion=conclusion)])
                observed = self.api.observe(self.chain)
                self.assertFalse(observed["actionable"])
                self.assertFalse(observed["ready"])
                self.assertIn("rerun required", self.api.next_action(self.chain, observed))
                self.assertIsNone(self.prepare())
                self.assertEqual((0, "open", []), (
                    self.chain["rounds"], self.chain["state"], self.chain["operations"]))

    def test_infrastructure_plus_fresh_review_is_review_only_and_evidence_reaches_both_agents(self):
        self.ci([check(self.head)])
        self.review()
        packet = self.prepare()
        self.assertTrue(packet["observation"]["reviewOnly"])
        self.assertEqual(["review-comment:31:2026-10-04T00:00:00Z"],
                         [item["id"] for item in packet["observation"]["feedback"]])
        body = pilot.worker_request(self.api, self.chain, self.chain["operations"][-1], packet)
        context = json.loads(body["prompt"].split("Bounded source/feedback JSON:\n")[1])
        self.assertEqual(packet["observation"], context)
        self.assertTrue(context["diagnostics"][0]["complete"])
        self.assertIn("lost communication", context["diagnostics"][0]["annotations"][0]["message"])
        self.assertIn(json.dumps(context, ensure_ascii=True), pilot.prompt(packet))
        self.assertFalse(context["ready"])

    def test_notice_scarcity_real_failure_and_raw_dispositioned_mixed_checks_are_investigatable(self):
        self.ci([check(self.head)], [annotation("Runner scarcity", "notice"), annotation("AssertionError")])
        self.transport.values[f"{self.api.prefix}/commits/{self.head}/check-runs"]["check_runs"][0]["output"]["annotations_count"] = 2
        observed = self.api.observe(self.chain)
        self.assertIsNone(observed["ciWait"])
        self.assertTrue(observed["actionable"])
        self.ci([check(self.head), check(self.head, 11)])
        self.transport.values[f"{self.api.prefix}/check-runs/11/annotations"] = [annotation("AssertionError")]
        self.chain["dispositions"][f"check:11:{self.head}:failure"] = "addressed"
        observed = self.api.observe(self.chain)
        self.assertIsNone(observed["ciWait"])
        self.assertTrue(observed["actionable"])
        self.assertEqual(1, len(observed["feedback"]))

    def test_hosted_runner_disconnect_is_positive_but_exit_failures_with_notices_are_not(self):
        # Captured REST annotation; only the blob URL's repository/head is
        # scrubbed. The runner error and nullable/location fields are verbatim.
        captured = json.loads((Path(__file__).parent / "fixtures" / "hosted-runner-disconnection.json").read_text())
        for annotations, infrastructure in (
            (captured, True),
            ([annotation("Process completed with exit code 2."),
              annotation("The ubuntu-latest label will migrate to Ubuntu 26.", "notice")], False),
            ([annotation("Process completed with exit code 7."),
              annotation("Due to capacity constraints, jobs targeting macOS arm64 runners may experience "
                         "longer queue times.", "notice")], False),
        ):
            with self.subTest(infrastructure=infrastructure, message=annotations[0]["message"]):
                self.ci([check(self.head, annotations_count=len(annotations))], annotations)
                observed = self.api.observe(self.chain)
                self.assertTrue(observed["diagnostics"][0]["complete"])
                self.assertEqual(infrastructure, observed["diagnostics"][0]["infrastructure"])
                self.assertEqual(not infrastructure, observed["actionable"])
                self.assertEqual(infrastructure, observed["ciWait"] is not None)

    def test_aggregate_check_is_retained_and_unknown_gate_evidence_reaches_worker(self):
        gate = check(self.head, 11)
        gate["name"] = "Final Results"
        self.ci([check(self.head), gate])
        self.transport.values[f"{self.api.prefix}/check-runs/11/annotations"] = [
            annotation("Process completed with exit code 1.")]
        observed = self.api.observe(self.chain)
        self.assertIsNone(observed["ciWait"])
        self.assertTrue(observed["actionable"])
        self.assertFalse(observed["ready"])
        self.assertEqual({f"check:{identity}:{self.head}:failure" for identity in (10, 11)},
                         {item["id"] for item in observed["feedback"]})
        packet = self.prepare()
        request = pilot.worker_request(self.api, self.chain, self.chain["operations"][-1], packet)
        context = json.loads(request["prompt"].split("Bounded source/feedback JSON:\n")[1])
        self.assertEqual(packet["observation"], context)
        self.assertEqual("Final Results: failure", next(
            item["body"] for item in context["feedback"] if item["id"].startswith("check:11:")))
        self.assertEqual({10, 11}, {item["checkId"] for item in context["diagnostics"]})

    def test_failed_commit_status_prevents_infrastructure_only_classification(self):
        self.ci([check(self.head)])
        self.transport.values[f"{self.api.prefix}/commits/{self.head}/status"] = {
            "statuses": [{"id": 12, "state": "error", "context": "Test service", "target_url": None}]}
        observed = self.api.observe(self.chain)
        self.assertIsNone(observed["ciWait"])
        self.assertTrue(observed["actionable"])

    def test_pending_ci_pauses_fresh_reviews_before_reservation_and_guards_repair(self):
        self.review()
        pending = check(self.head)
        pending.update(status="in_progress", conclusion=None)
        self.ci([pending])
        observed = self.api.observe(self.chain)
        self.assertFalse(observed["actionable"])
        self.assertFalse(observed["ready"])
        self.assertIsNone(self.prepare())
        with self.assertRaisesRegex(ValueError, "CI wait"):
            self.api.guard(self.chain, observed)
        self.assertEqual(0, self.chain["rounds"])

    def test_old_head_rejected_before_annotation_get_and_foreign_endpoint_denied(self):
        self.ci([check("b" * 40)])
        self.transport.reads.clear()
        with self.assertRaisesRegex(ValueError, "old head"):
            self.api.observe(self.chain)
        self.assertEqual([], [path for _, path, _ in self.transport.reads if "/annotations" in path])
        transport = github.PilotTransport("fixture", binding=bindings.UPSTREAM)
        transport.validate_endpoint("GET", f"{self.api.prefix}/check-runs/10/annotations?per_page=100&page=1", None)
        for endpoint in (
            "repos/foreign/aspire/check-runs/10/annotations?per_page=100&page=1",
            f"{self.api.prefix}/check-runs/10/annotations?per_page=100&page=11",
            f"{self.api.prefix}/check-runs/10/annotations?per_page=100&page=1&url=evil",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                transport.validate_endpoint("GET", endpoint, None)
        with self.assertRaises(ValueError):
            transport.validate_endpoint("POST", f"{self.api.prefix}/check-runs/10/annotations", {})

    def test_annotation_missing_count_malformed_partial_and_unavailable_are_explicit_unknown(self):
        for problem in ("count", "partial", "malformed", "unavailable"):
            with self.subTest(problem=problem):
                item = check(self.head)
                self.ci([item])
                if problem == "count":
                    item.pop("output")
                    self.ci([item])
                elif problem == "partial":
                    item["output"]["annotations_count"] = 2
                    self.ci([item])
                elif problem == "malformed":
                    self.ci([item], [{"annotation_level": "failure"}])
                original = self.api.transport
                def read(method, endpoint, body):
                    if "/annotations?" in endpoint and problem == "unavailable":
                        return Response({}, {}, 403)
                    return original(method, endpoint, body)
                with patch.object(self.api, "transport", read), redirect_stderr(io.StringIO()) as errors:
                    observed = self.api.observe(self.chain)
                diagnostic = observed["diagnostics"][0]
                self.assertFalse(diagnostic["complete"])
                self.assertIn("unknown", diagnostic)
                self.assertIn("diagnostics unknown", errors.getvalue())
                self.assertIsNone(observed["ciWait"])
                self.assertTrue(observed["actionable"])

    def test_annotation_aggregate_request_and_serialized_byte_limits_are_unknown_not_empty(self):
        self.ci([check(self.head, index) for index in range(1, 13)])
        self.transport.reads.clear()
        with redirect_stderr(io.StringIO()):
            observed = self.api.observe(self.chain)
        self.assertEqual(10, len([path for _, path, _ in self.transport.reads if "/annotations?" in path]))
        self.assertEqual(2, sum(not item["complete"] for item in observed["diagnostics"]))
        self.assertIsNone(observed["ciWait"])
        self.ci([check(self.head)], [annotation("漢" * 6000)])
        with redirect_stderr(io.StringIO()):
            observed = self.api.observe(self.chain)
        self.assertIn("byte bound", observed["diagnostics"][0]["unknown"])

    def test_superseded_same_head_workflow_failure_does_not_hold_recovered_ci(self):
        self.ci([check(self.head, conclusion="success")], [])
        self.transport.values[f"{self.api.prefix}/pulls/20722/reviews"] = [{
            "id": 22, "user": {"id": 1472, "login": "radical"}, "state": "APPROVED", "commit_id": self.head}]
        runs = [{"id": identity, "workflow_id": 90, "head_sha": self.head, "status": "completed",
                 "conclusion": conclusion, "repository": {"id": self.api.repository_id, "full_name": self.api.repository},
                 "html_url": f"https://github.com/{self.api.repository}/actions/runs/{identity}"}
                for identity, conclusion in ((21, "cancelled"), (22, "success"))]
        self.transport.values[f"{self.api.prefix}/actions/runs"] = {"total_count": 2, "workflow_runs": runs}
        observed = self.api.observe(self.chain)
        self.assertTrue(observed["ready"])
        self.assertFalse(observed["pendingCI"])
        self.assertIsNone(observed["ciWait"])
        self.review()
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            with self.subTest(older_run_status=status):
                runs[0].update(status=status, conclusion=None)
                observed = self.api.observe(self.chain)
                self.assertTrue(observed["pendingCI"])
                self.assertFalse(observed["actionable"])
                self.assertFalse(observed["ready"])
                self.assertEqual(1, len(observed["feedback"]))
                self.assertIsNone(self.prepare())
                self.assertEqual(0, self.chain["rounds"])
                with self.assertRaisesRegex(ValueError, "CI wait"):
                    self.api.guard(self.chain, observed)

    def test_unknown_completed_check_conclusion_is_not_infrastructure_even_with_runner_annotation(self):
        self.ci([check(self.head, conclusion="unknown")])
        observed = self.api.observe(self.chain)
        self.assertIsNone(observed["ciWait"])
        self.assertTrue(observed["actionable"])

    def test_newer_nonterminal_run_does_not_hide_terminal_workflow_approval_evidence(self):
        runs = [{"id": identity, "run_attempt": 1, "workflow_id": 90, "head_sha": self.head, "status": status,
                 "conclusion": conclusion, "repository": {"id": self.api.repository_id, "full_name": self.api.repository},
                 "html_url": f"https://github.com/{self.api.repository}/actions/runs/{identity}"}
                for identity, status, conclusion in (
                    (21, "completed", "action_required"), (22, "queued", None))]
        self.transport.values[f"{self.api.prefix}/actions/runs"] = {"total_count": 2, "workflow_runs": runs}
        observed = self.api.observe(self.chain)
        self.assertTrue(observed["pendingCI"])
        self.assertEqual({"id": "21", "url": runs[0]["html_url"]}, observed["approval"])
        self.assertFalse(observed["ready"])
        self.assertFalse(observed["actionable"])
        self.assertIsNone(self.prepare())
        self.assertEqual(0, self.chain["rounds"])

    def test_terminal_workflow_failure_without_jobs_is_investigatable_but_cancelled_waits(self):
        run = {"id": 12, "run_attempt": 1, "workflow_id": 90, "head_sha": self.head, "status": "completed",
               "conclusion": "failure", "repository": {"id": self.api.repository_id, "full_name": self.api.repository},
               "html_url": f"https://github.com/{self.api.repository}/actions/runs/12"}
        self.transport.values[f"{self.api.prefix}/actions/runs"] = {"total_count": 1, "workflow_runs": [run]}
        observed = self.api.observe(self.chain)
        self.assertTrue(observed["actionable"])
        self.assertEqual([f"workflow:12:{self.head}:failure"], [item["id"] for item in observed["feedback"]])
        self.assertIsNone(observed["ciWait"])
        run["conclusion"] = "cancelled"
        observed = self.api.observe(self.chain)
        self.assertFalse(observed["actionable"])
        self.assertIn("outage not established", observed["ciWait"])

    def test_large_feedback_escaping_is_bounded_before_native_and_preserves_all_ids(self):
        for content in ("x" * 2000, '漢"\\' * 1000):
            with self.subTest(non_ascii=content[0] == "漢"):
                api, transport = fixtures.TrackedOnlyTests().api(bindings.UPSTREAM)
                transport.values[f"{api.prefix}/pulls/20722/comments"] = [
                    {"id": index + 1, "body": content, "updated_at": "2026-10-04T00:00:00Z",
                     "user": {"id": 1472, "login": "radical"}, "path": "path", "line": 1} for index in range(30)]
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(api, RUN, api.clock(), present=False)
                chain = api.ledger["chains"][0]
                request = pilot.worker_request(api, chain, chain["operations"][-1], packet)
                self.assertLessEqual(len(json.dumps(request, ensure_ascii=True).encode()), 20000)
                context = json.loads(request["prompt"].split("Bounded source/feedback JSON:\n")[1])
                self.assertEqual(packet["observation"], context)
                self.assertEqual({f"review-comment:{index + 1}:2026-10-04T00:00:00Z" for index in range(30)},
                                 {item["id"] for item in context["feedback"]})
                self.assertTrue(all("[truncated]" in item["body"] for item in context["feedback"]))
                self.assertEqual(1, chain["rounds"])
                self.assertEqual(30, chain["operations"][-1]["nativeReserved"])

    def test_all_declined_pr_human_or_cloud_completes_open_without_worker_or_reminder(self):
        for action in ("human", "cloud"):
            with self.subTest(action=action):
                api, transport = fixtures.TrackedOnlyTests().api(bindings.UPSTREAM)
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(api, RUN, api.clock(), present=False)
                chain = api.ledger["chains"][0]
                operation = chain["operations"][-1]
                decision = fixtures.decision(packet)
                decision.update(action=action, dispositions={
                    item["id"]: "declined" for item in packet["observation"]["feedback"]})
                result = pilot.settle(api, packet, reconciliation_evidence(decision), 3.2, api.clock())
                self.assertEqual({"outcome": "declined"}, result)
                self.assertEqual("open", chain["state"])
                self.assertEqual(decision["dispositions"], chain["dispositions"])
                self.assertEqual(("completed", 3.2, 0, 0, None, 1), (
                    operation["state"], operation["nativeActual"], operation["nativeReserved"],
                    operation["workerReserved"], operation["taskId"], chain["rounds"]))
                self.assertFalse(state.pending(chain))
                self.assertEqual(0, state.worker_slots(api.ledger))
                with redirect_stdout(io.StringIO()):
                    observed = api.observe(chain)
                    reminders.process(api, chain, observed, api.clock())
                    self.assertIsNone(pilot.prepare(api, RUN, api.clock(), present=False))
                self.assertNotIn("reminder", chain)
                self.assertEqual([], [write for write in transport.writes if write[0] == "POST"])
                self.assertEqual(api.ledger, state.parse(transport.comments[0]["body"]))
                previous = deepcopy(operation)
                transport.values[f"{api.prefix}/pulls/20722/comments"].append({
                    "id": 32, "body": "New actionable review", "updated_at": "2026-10-05T00:00:00Z",
                    "user": {"id": 1472, "login": "radical"}})
                with redirect_stdout(io.StringIO()):
                    fresh = pilot.prepare(api, RUN, api.clock(), present=False)
                self.assertEqual(2, chain["rounds"])
                self.assertEqual(previous, chain["operations"][0])
                self.assertEqual(["review-comment:32:2026-10-05T00:00:00Z"],
                                 [item["id"] for item in fresh["observation"]["feedback"]])

    def test_declined_unrelated_failure_skips_repair_but_stays_red_until_ci_recovers(self):
        self.ci([check(self.head), check(self.head, 11, conclusion="success", annotations_count=0)],
                [annotation("Known baseline test failure")])
        self.transport.values[f"{self.api.prefix}/pulls/20722/reviews"] = [{
            "id": 22, "user": {"id": 1472, "login": "radical"}, "state": "APPROVED", "commit_id": self.head}]
        packet = self.prepare()
        self.assertEqual([f"check:10:{self.head}:failure"],
                         [item["id"] for item in packet["observation"]["feedback"]])
        decision = fixtures.decision(packet)
        decision["dispositions"] = {item["id"]: "declined" for item in packet["observation"]["feedback"]}
        result = pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.api.clock())
        self.assertEqual({"outcome": "declined"}, result)
        self.assertEqual([], [write for write in self.transport.writes if write[0] == "POST"])
        self.assertEqual("open", self.chain["state"])
        self.assertEqual(decision["dispositions"], self.chain["dispositions"])
        observed = self.api.observe(self.chain)
        self.assertEqual([], observed["feedback"])
        self.assertFalse(observed["ready"])
        self.assertIsNone(self.prepare())
        self.assertEqual(1, self.chain["rounds"])
        self.ci([check(self.head, conclusion="success"), check(self.head, 11, conclusion="success")], [])
        self.assertTrue(self.api.observe(self.chain)["ready"])
        self.assertEqual(1, self.chain["rounds"])

    def test_genuine_pr_native_blocker_still_pauses_new_feedback_and_starts_handoff_reminder(self):
        self.review(2)
        packet = self.prepare()
        decision = fixtures.decision(packet)
        decision.update(action="human", dispositions={
            item["id"]: "needs-human" if index == 0 else "declined"
            for index, item in enumerate(packet["observation"]["feedback"])})
        result = pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.api.clock())
        self.assertEqual({"outcome": "human"}, result)
        self.assertEqual("human", self.chain["state"])
        self.transport.values[f"{self.api.prefix}/pulls/20722/comments"].append({
            "id": 33, "body": "Fresh feedback", "updated_at": "2026-10-05T00:00:00Z", "user": {"id": 1472, "login": "radical"}})
        with redirect_stdout(io.StringIO()):
            observed = self.api.observe(self.chain)
            self.assertTrue(observed["actionable"])
            reminders.process(self.api, self.chain, observed, self.api.clock())
            self.assertIsNone(self.prepare())
        self.assertEqual(1, self.chain["rounds"])
        self.assertEqual(("native-handoff", packet["operation"]), (
            self.chain["reminder"]["kind"], self.chain["reminder"]["reason"]))
        self.assertEqual([], [write for write in self.transport.writes if write[0] == "POST"])

    def test_all_declined_issue_comments_do_not_cancel_issue_body_work_or_handoff(self):
        for action in ("human", "cloud"):
            with self.subTest(action=action):
                api, transport = fixtures.TrackedOnlyTests().api(bindings.FORK)
                issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                         "labels": [{"name": "shepherd-adopted"}], "title": "Bug", "body": "Implement the issue body"}
                transport.values[f"{api.prefix}/issues"] = [issue]
                transport.values[f"{api.prefix}/issues/8"] = issue
                transport.values[f"{api.prefix}/issues/8/comments"] = [{
                    "id": 90, "body": "Unrelated suggestion", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(api, RUN, api.clock(), present=False)
                chain = api.ledger["chains"][0]
                decision = fixtures.decision(packet)
                decision.update(action=action, dispositions={
                    item["id"]: "declined" for item in packet["observation"]["feedback"]})
                with redirect_stderr(io.StringIO()):
                    result = pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
                self.assertEqual("issue", packet["observation"]["kind"])
                self.assertEqual("human" if action == "human" else "uncertain", result["outcome"])
                self.assertEqual(2, chain["operations"][-1]["nativeActual"])
                posts = [write for write in transport.writes if write[1].endswith("/tasks")]
                self.assertEqual(0 if action == "human" else 1, len(posts))
                if posts:
                    self.assertTrue(posts[0][2]["create_pull_request"])
                    self.assertIn("Implement the issue body", posts[0][2]["prompt"])

    def test_real_inline_patch_is_not_cancelled_by_declined_feedback(self):
        api, transport = fixtures.TrackedOnlyTests().api(bindings.FORK)
        transport.values[f"{api.prefix}/pulls/7/files"] = [
            {"sha": "d" * 40, "filename": pilot_patch.SOURCE, "status": "modified"}]
        for path, content in {
            pilot_patch.SOURCE: "def normalize_label(value):\n    return value\n",
            pilot_patch.TEST: "import unittest\n",
        }.items():
            transport.values[f"{api.prefix}/contents/{path}"] = {
                "type": "file", "path": path, "size": len(content.encode()), "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode()}
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        decision = fixtures.decision(packet)
        decision.update(action="patch", replacement="def normalize_label(value):\n    return value.strip()\n",
                        dispositions={item["id"]: "declined" for item in packet["observation"]["feedback"]})
        result = pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
        self.assertEqual("validate", result["outcome"])
        self.assertEqual(decision["replacement"], result["proposal"]["replacement"])
        self.assertEqual(decision["dispositions"], result["dispositions"])

    def test_mandatory_worker_fields_cannot_fit_pauses_without_reserving(self):
        self.review()
        self.api.tracker_node = "N" * 22000
        # Identity was already authenticated; emulate an oversized mandatory
        # worker preamble, not an arbitrary dropped feedback batch.
        with patch.object(self.api, "read_authority", return_value=self.api.expected), \
                redirect_stderr(io.StringIO()) as errors:
            self.assertIsNone(self.prepare())
        self.assertEqual(0, self.chain["rounds"])
        self.assertEqual([], self.chain["operations"])

    def test_late_ci_wait_at_every_dispatch_guard_releases_only_unused_worker_budget(self):
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary):
                api, transport = fixtures.TrackedOnlyTests().api(bindings.UPSTREAM)
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(api, RUN, api.clock(), present=False)
                chain = api.ledger["chains"][0]
                operation = chain["operations"][-1]
                decision = {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                            "action": "cloud", "replacement": None,
                            "dispositions": {item["id"]: "addressed" for item in packet["observation"]["feedback"]}}
                original = api.guard
                calls = 0
                def guard(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == boundary:
                        pending = check(packet["observation"]["head"])
                        pending.update(status="in_progress", conclusion=None)
                        transport.values[f"{api.prefix}/commits/{pending['head_sha']}/check-runs"] = {
                            "total_count": 1, "check_runs": [pending]}
                    return original(*args, **kwargs)
                with patch.object(api, "guard", guard), redirect_stderr(io.StringIO()):
                    result = pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
                self.assertEqual("failed" if boundary == 1 else "no-send", result["outcome"])
                self.assertEqual((2, 0, 0, None, 1), (
                    operation["nativeActual"], operation["nativeReserved"], operation["workerReserved"],
                    operation["taskId"], chain["rounds"]))
                self.assertFalse(state.pending(chain))
                self.assertEqual(0, state.worker_slots(api.ledger))
                self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])
                self.assertEqual(api.ledger, state.parse(transport.comments[0]["body"]))

    def test_unknown_send_and_unknown_authority_publication_never_refund_as_no_send(self):
        self.review()
        packet = self.prepare()
        operation = self.chain["operations"][-1]
        state.settle_native(operation, 2)
        original = self.api.transport
        def uncertain(method, endpoint, body):
            if method == "PATCH" and endpoint.endswith("/comments/700"):
                proposed = state.parse(body["body"])
                if proposed["chains"][0]["operations"][-1]["state"] == "sent":
                    raise LostResponse("unconfirmed authority publication")
            return original(method, endpoint, body)
        with patch.object(self.api, "transport", uncertain), self.assertRaises(github.AuthorityUncertain):
            pilot.dispatch(self.api, self.chain, operation, packet, self.api.clock())
        self.assertEqual("uncertain", operation["state"])
        self.assertGreater(operation["workerReserved"], 0)
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])


class AnnotationPagingTests(unittest.TestCase):
    def test_idless_full_final_page_requires_verified_last_page_and_does_not_overfetch(self):
        path = "repos/microsoft/aspire/check-runs/10/annotations"
        calls = []
        def complete(method, endpoint, body):
            calls.append(endpoint)
            return Response([annotation()] * 100, {"Link":
                f'<https://api.github.com/repositories/696529789/check-runs/10/annotations?per_page=100&page=1>; rel="last"'})
        values = live.API(complete, repository_id=696529789).pages(path, identity_key=None)
        self.assertEqual(100, len(values))
        self.assertEqual([path + "?per_page=100&page=1"], calls)
        with self.assertRaisesRegex(IncompleteInventory, "missing next link"):
            live.API(lambda *_: Response([annotation()] * 100, {})).pages(path, identity_key=None)

    def test_idless_nullable_annotations_page_completely_without_relaxing_default_identity(self):
        path = "repos/microsoft/aspire/check-runs/10/annotations"
        values = [annotation()] * 100
        calls = []
        def read(method, endpoint, body):
            calls.append(endpoint)
            return Response(values if endpoint.endswith("&page=1") else [annotation(None)],
                            {"Link": f'<https://api.github.com/repositories/696529789/check-runs/10/annotations?per_page=100&page=2>; rel="next"'}
                            if endpoint.endswith("&page=1") else {})
        api = live.API(read, repository_id=696529789)
        self.assertEqual(101, len(api.pages(path, identity_key=None, max_pages=2, max_bytes=50000)))
        self.assertEqual([path + "?per_page=100&page=1", path + "?per_page=100&page=2"], calls)
        with self.assertRaisesRegex(IncompleteInventory, "identity"):
            live.API(lambda *_: Response([annotation()], {})).pages(path)

    def test_idless_partial_foreign_nonsequential_and_contradictory_paging_rejected(self):
        path = "repos/microsoft/aspire/check-runs/10/annotations"
        for target in (
            "https://api.github.com/repos/foreign/aspire/check-runs/10/annotations?per_page=100&page=2",
            f"https://api.github.com/{path}?per_page=100&page=3",
            f"https://api.github.com/{path}?per_page=100&page=2&extra=1",
        ):
            with self.subTest(target=target), self.assertRaises(IncompleteInventory):
                live.API(lambda *_: Response([annotation()], {"Link": f'<{target}>; rel="next"'})).pages(
                    path, identity_key=None)
        with self.assertRaises(IncompleteInventory):
            live.API(lambda *_: Response([annotation()] * 100, {})).pages(path, identity_key=None)
        with self.assertRaises(IncompleteInventory):
            live.API(lambda *_: Response([], {"Link":
                f'<https://api.github.com/{path}?per_page=100&page=2>; rel="last"'})).pages(path, identity_key=None)


class ResumeTests(WorkspaceTest, unittest.TestCase):
    def fixture(self):
        fixture = fixtures.TrackedOnlyTests()
        api, transport = fixture.api(bindings.UPSTREAM)
        chain, worker, task = fixture.seed_worker(api, transport, completed=True)
        # Old worker disposition, plus two prior fully billed native handoffs.
        for index in (2, 3):
            observation = api.observe(chain)
            observation["feedback"] = [{"id": f"review:old-{index}", "body": "Old"}]
            operation = state.reserve(api.ledger, chain, github.fingerprint(observation) + f":round:{index}",
                                      api.clock(), local=False)
            state.settle_native(operation, 11.34002775)
            operation["sessionId"] = f"native-{index}"
            state.finish(operation, "completed")
            chain["dispositions"][f"review:old-{index}"] = "needs-human"
        chain["dispositions"].update({f"review:older-{index}": "needs-human" for index in range(5)})
        head = transport.values[f"{api.prefix}/pulls/20722"]["head"]["sha"]
        transport.values[f"{api.prefix}/pulls/20722/comments"] = [
            {"id": index, "body": "Current review", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}
            for index in range(40, 43)]
        checks = [check(head, identity) for identity in range(50, 54)]
        transport.values[f"{api.prefix}/commits/{head}/check-runs"] = {"total_count": 4, "check_runs": checks}
        for item in checks:
            transport.values[f"{api.prefix}/check-runs/{item['id']}/annotations"] = [annotation("AssertionError")]
        observation = api.observe(chain)
        latest = state.reserve(api.ledger, chain, github.fingerprint(observation) + ":round:4", api.clock(), local=False)
        state.settle_native(latest, 3.1934)
        latest["sessionId"] = "fresh-native-4"
        state.finish(latest, "completed")
        batch = [item["id"] for item in observation["feedback"]]
        chain["dispositions"].update({item: "needs-human" if index < 5 else "declined"
                                     for index, item in enumerate(batch)})
        chain["state"] = "human"
        chain["reminder"] = {"id": "00000000-0000-4000-8000-000000000001", "head": head, "kind": "native-handoff",
                             "reason": latest["id"], "firstObservedAt": "2026-10-04T00:00:00Z",
                             "sendState": "confirmed", "commentId": 900}
        api.persist()
        return api, transport, chain, latest, head, batch, task

    def test_four_operation_resume_preserves_all_billing_tasks_history_and_only_unmasks_latest_batch(self):
        api, transport, chain, latest, head, batch, _ = self.fixture()
        before = deepcopy(api.ledger)
        transport.reads.clear()
        with patch.object(api, "reconcile_workers", side_effect=AssertionError("resume must not reconcile")):
            result = local.resume(api, latest["id"], head, api.clock())
        expected = deepcopy(before)
        expected["chains"][0]["state"] = "open"
        expected["chains"][0].pop("reminder")
        for item in batch[:5]:
            del expected["chains"][0]["dispositions"][item]
        self.assertEqual(expected, api.ledger)
        self.assertEqual(expected, state.parse(transport.comments[0]["body"]))
        self.assertEqual("resumed; no inference", result["outcome"])
        self.assertEqual([f"agents/repos/{api.repository}/tasks/OWNED20722"],
                         [endpoint for method, endpoint, _ in transport.reads if method == "GET" and "/tasks/" in endpoint])
        self.assertEqual(5, len(api.observe(chain)["feedback"]))
        self.assertEqual(4, chain["rounds"])
        self.assertAlmostEqual(29.3734555, state.chain_spend(chain))
        self.assertEqual(7, sum(value == "needs-human" for value in chain["dispositions"].values()))

    def test_local_resume_entrypoint_uses_authority_lock_and_stop_controls_without_copilot(self):
        api, transport, chain, latest, head, _, _ = self.fixture()
        def command(argv):
            if argv[0] == "copilot":
                self.fail("resume must not require or invoke Copilot CLI")
            if argv[:3] == ["gh", "auth", "token"]:
                return "fixture-token"
            if "rev-parse" in argv:
                return "b" * 40
            if "status" in argv:
                return ""
            self.fail(f"unexpected command: {argv}")
        with patch.object(local, "command", command), \
                patch.object(local, "selected_token", return_value="fixture-token"), \
                patch.object(local, "authority_lock", return_value=nullcontext()) as lock, \
                patch.object(local, "require_source") as source, \
                patch.object(local, "require_idle_actions") as idle, \
                patch.object(local, "LocalGitHub", return_value=api), \
                patch.object(local, "sweep", side_effect=AssertionError("resume must not sweep")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, local.main([
                "resume", "--tracker", str(api.tracker), "--authority", str(api.authority_id),
                "--tracker-node", api.tracker_node, "--workdir", str(self.work),
                "--operation", latest["id"], "--expected-head", head]))
        lock.assert_called_once()
        self.assertEqual(api.authority_id, lock.call_args.args[1])
        source.assert_called_once_with("b" * 40)
        idle.assert_called_once_with("fixture-token")
        self.assertEqual("resumed; no inference", json.loads(output.getvalue())["outcome"])
        self.assertEqual(4, chain["rounds"])
        self.assertEqual([], [write for write in transport.writes if write[0] == "POST"])

    def test_resume_pending_ci_and_infrastructure_wait_do_not_require_repair_guard(self):
        for conclusion in (None, "failure"):
            with self.subTest(conclusion=conclusion):
                api, transport, chain, latest, head, _, _ = self.fixture()
                item = check(head, conclusion=conclusion)
                if conclusion is None:
                    item["status"] = "in_progress"
                transport.values[f"{api.prefix}/commits/{head}/check-runs"] = {"total_count": 1, "check_runs": [item]}
                transport.values[f"{api.prefix}/check-runs/10/annotations"] = [annotation()]
                local.resume(api, latest["id"], head, api.clock())
                self.assertEqual("open", chain["state"])
                self.assertEqual(4, chain["rounds"])
                self.assertEqual(conclusion == "failure", api.observe(chain)["actionable"])

    def test_resume_same_head_recovered_rerun_unmasks_only_authorized_superseded_check_batch(self):
        api, transport, chain, latest, head, batch, _ = self.fixture()
        before = deepcopy(api.ledger)
        recovered = [check(head, identity, conclusion="success", annotations_count=0)
                     for identity in range(60, 64)]
        transport.values[f"{api.prefix}/commits/{head}/check-runs"] = {
            "total_count": len(recovered), "check_runs": recovered}
        observed = api.observe(chain)
        self.assertIsNone(observed["ciWait"])
        self.assertFalse(observed["pendingCI"])
        self.assertEqual([], observed["feedback"])
        with patch.object(api, "guard", wraps=api.guard) as guarded:
            local.resume(api, latest["id"], head, api.clock())
        guarded.assert_called_once()
        fresh = guarded.call_args.args[1]
        self.assertEqual(head, fresh["head"])
        self.assertEqual(["review-comment:40:2026-10-04T00:00:00Z"],
                         [item["id"] for item in fresh["feedback"]])
        self.assertIsNone(fresh["ciWait"])
        expected = deepcopy(before)
        expected["chains"][0]["state"] = "open"
        expected["chains"][0].pop("reminder")
        for item in batch[:5]:
            del expected["chains"][0]["dispositions"][item]
        self.assertEqual(expected, api.ledger)
        self.assertEqual(expected, state.parse(transport.comments[0]["body"]))
        self.assertEqual(4, chain["rounds"])

    def test_resume_ci_wait_does_not_mask_edited_non_ci_feedback(self):
        api, transport, chain, latest, head, _, _ = self.fixture()
        transport.values[f"{api.prefix}/pulls/20722/comments"][0]["updated_at"] = "2026-10-05T00:00:00Z"
        transport.values[f"{api.prefix}/commits/{head}/check-runs"] = {
            "total_count": 1, "check_runs": [check(head, conclusion="cancelled")]}
        self.assertIsNotNone(api.observe(chain)["ciWait"])
        before = deepcopy(api.ledger)
        writes = deepcopy(transport.writes)
        with self.assertRaisesRegex(ValueError, "feedback/source changed"):
            local.resume(api, latest["id"], head, api.clock())
        self.assertEqual(before, api.ledger)
        self.assertEqual(writes, transport.writes)

    def test_existing_all_declined_native_handoff_resumes_without_unmasking_any_dispositions(self):
        api, transport, chain, latest, head, batch, _ = self.fixture()
        chain["dispositions"].update({item: "declined" for item in batch})
        api.persist()
        before = deepcopy(api.ledger)
        with patch.object(api, "reconcile_workers", side_effect=AssertionError("resume must not reconcile")):
            result = local.resume(api, latest["id"], head, api.clock())
        expected = deepcopy(before)
        expected["chains"][0]["state"] = "open"
        expected["chains"][0].pop("reminder")
        self.assertEqual(expected, api.ledger)
        self.assertEqual(expected, state.parse(transport.comments[0]["body"]))
        self.assertEqual("resumed; no inference", result["outcome"])
        self.assertEqual(4, chain["rounds"])
        self.assertEqual(before["chains"][0]["dispositions"], chain["dispositions"])
        self.assertEqual([], [write for write in transport.writes if write[0] == "POST"])
        self.assertEqual([], api.observe(chain)["feedback"])
        transport.values[f"{api.prefix}/pulls/20722/comments"].append({
            "id": 99, "body": "New review", "updated_at": "2026-10-05T00:00:00Z", "user": {"id": 1472, "login": "radical"}})
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertEqual(5, chain["rounds"])
        self.assertEqual(["review-comment:99:2026-10-05T00:00:00Z"],
                         [item["id"] for item in packet["observation"]["feedback"]])
        self.assertEqual(before["chains"][0]["operations"], chain["operations"][:-1])

    def test_zero_unmask_resume_rejects_mixed_addressed_declined_without_human_blocker(self):
        api, transport, chain, latest, head, batch, _ = self.fixture()
        chain["dispositions"].update({item: "declined" for item in batch})
        chain["dispositions"][batch[0]] = "addressed"
        api.persist()
        before = deepcopy(api.ledger)
        writes = deepcopy(transport.writes)
        with self.assertRaisesRegex(ValueError, "not all declined"):
            local.resume(api, latest["id"], head, api.clock())
        self.assertEqual(before, api.ledger)
        self.assertEqual(writes, transport.writes)

    def test_resume_rejects_stale_operation_head_takeover_pending_limits_worker_and_unknown_billing(self):
        for problem in ("operation", "head", "takeover", "closed", "pending", "rounds", "budget", "native",
                        "worker", "missing-child", "task", "task-billing", "overlap", "feedback"):
            with self.subTest(problem=problem):
                api, transport, chain, latest, head, batch, task = self.fixture()
                operation_id = latest["id"]
                if problem == "operation":
                    operation_id = chain["operations"][0]["id"]
                elif problem == "head":
                    head = "b" * 40
                elif problem == "takeover":
                    transport.values[f"{api.prefix}/pulls/20722"]["labels"] = [{"name": "shepherd-hands-off"}]
                elif problem == "closed":
                    transport.values[f"{api.prefix}/pulls/20722"]["state"] = "closed"
                elif problem == "pending":
                    chain["statusPending"] = True
                elif problem == "rounds":
                    chain["state"] = "open"
                    extra = state.reserve(api.ledger, chain, latest["identity"] + "-5", api.clock(), local=False)
                    state.settle_native(extra, 1)
                    state.finish(extra, "completed")
                    chain["state"] = "human"
                elif problem == "budget":
                    latest["nativeActual"] = state.chain_allowance(api.ledger) - 10
                elif problem == "native":
                    latest.update(nativeActual=None, nativeReserved=30)
                elif problem == "worker":
                    latest.update(taskId="worker", workerState="completed")
                elif problem == "missing-child":
                    chain.update(kind="issue")
                elif problem == "task":
                    task["sessions"][0]["state"] = "in_progress"
                elif problem == "task-billing":
                    task["sessions"][0]["usage"] = None
                elif problem == "overlap":
                    basis = json.loads(chain["operations"][0]["identity"].rsplit(":round:", 1)[0])
                    basis["feedback"].append(batch[0])
                    chain["operations"][0]["identity"] = json.dumps(basis) + ":round:1"
                elif problem == "feedback":
                    transport.values[f"{api.prefix}/pulls/20722/comments"][0]["updated_at"] = "2026-10-05T00:00:00Z"
                # Some deliberately malformed states cannot be published; the
                # helper must still reject them without mutating the authority.
                if problem != "missing-child":
                    api.persist()
                before = deepcopy(api.ledger)
                writes = deepcopy(transport.writes)
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    local.resume(api, operation_id, head, api.clock())
                self.assertEqual(before, api.ledger)
                self.assertEqual(writes, transport.writes)

    def test_resume_keeps_nonmatching_reminder_and_rejects_authority_and_source_races(self):
        for problem in ("other-reminder", "authority", "head", "source", "budget"):
            with self.subTest(problem=problem):
                api, transport, chain, latest, head, _, _ = self.fixture()
                if problem == "other-reminder":
                    chain["reminder"]["reason"] = "other-operation"
                    api.persist()
                    reminder = deepcopy(chain["reminder"])
                    local.resume(api, latest["id"], head, api.clock())
                    self.assertEqual(reminder, chain["reminder"])
                    continue
                original = api.guard
                def guard(*args, **kwargs):
                    if problem == "authority":
                        changed = deepcopy(api.expected)
                        changed["cursor"] += 1
                        transport.comments[0]["body"] = state.render(changed)
                    elif problem == "head":
                        transport.values[f"{api.prefix}/pulls/20722"]["head"]["sha"] = "b" * 40
                    elif problem == "source":
                        raise ValueError("controller source changed")
                    elif problem == "budget":
                        latest["nativeActual"] = 499
                    return original(*args, **kwargs)
                before = deepcopy(api.ledger)
                writes = deepcopy(transport.writes)
                with patch.object(api, "guard", guard), self.assertRaises(ValueError):
                    local.resume(api, latest["id"], head, api.clock())
                self.assertEqual(before, api.ledger)
                self.assertEqual(writes, transport.writes)


if __name__ == "__main__":
    unittest.main()
