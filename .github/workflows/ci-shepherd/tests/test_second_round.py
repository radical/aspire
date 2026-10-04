from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from helpers import WorkspaceTest, reconciliation_decision, reconciliation_evidence
from github import Response
from test_failed_apply import FailedApplyService, TASK
from test_pinned_recovery import TRIAL
from test_rate_limit import WindowOpener
import hosted
import live
import receipts
import recovery
import round as contracts


LABELS = {"filename": ".ci-shepherd-fixture/labels.py", "status": "modified"}


class SecondRoundTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = FailedApplyService()
        self.invocations = 0
        self.now = datetime(2026, 10, 4, 7, tzinfo=timezone.utc)
        self.policy = recovery.PinnedRecovery(self.service.run)
        self.confirmed = deepcopy(self.service.consumed)
        self.confirmed["operations"][0].update(state="confirmed", result={"id": TASK, "kind": "worker"})
        self.service.comments[0]["body"] = receipts.render_record(self.confirmed)
        self.service.tasks[TASK]["state"] = self.service.tasks[TASK]["sessions"][0]["state"] = "completed"
        self.service.commit_shas = ["c" * 40, "d" * 40]
        self.service.pr["head"]["sha"] = self.service.ci_head = self.service.commit_shas[-1]

    def prepare(self, *, transport=None):
        with patch.object(live, "clock", return_value=self.now):
            return hosted.prepare(self.work / "prepared", "live", self.service.run,
                                  transport=transport or self.service.transport, host_check=lambda run: None, recovery=self.policy)

    def apply(self, packet, context=None, *, clock_fn=None, transport=None):
        if context is not None:
            path = self.work / "prepared/trusted/envelope.json"
            envelope = contracts.read_json(path)
            envelope["context"] = context
            path.write_text(json.dumps(envelope))
        feedback = [item["id"] for item in packet["basis"]["feedback"] if item["state"] == "open"]
        decision = reconciliation_decision(packet, "repair-pr", {"feedbackIds": feedback})
        self.invocations += 1
        evidence = self.work / f"evidence-{self.invocations}.json"
        output = self.work / f"decision-{self.invocations}.json"
        contracts.write_json(evidence, reconciliation_evidence(decision))
        contracts.write_json(output, {
            "items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        with patch.object(live, "clock", side_effect=clock_fn or (lambda: self.now)):
            return hosted.apply(self.work / "prepared/trusted", evidence,
                                output, self.work / "receipt.json", self.service.run,
                                transport=transport or self.service.transport, host_check=lambda run: None, recovery=self.policy)

    def assert_unchanged(self):
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.confirmed)

    def scope(self, context):
        self.assertIn("repairScope", context, "Produced host context lacks verified current-head repair admission")
        return context["repairScope"]

    def test_second_head_operation_spends_batch_two_once_under_original_trial(self):
        packet, envelope, prompt = self.prepare()
        result, error = None, None
        try:
            result = self.apply(packet)
        except ValueError as caught:
            error = str(caught)
        self.assertIsNone(error, "Verified second normalization round was rejected: " + str(error))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)
        current = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(receipts.trial_tuple(current), TRIAL)
        self.assertEqual(current["repairBatches"], 2)
        self.assertEqual(current["operations"][0], self.confirmed["operations"][0])
        self.assertEqual(len(current["operations"]), 2)
        new = current["operations"][1]
        self.assertNotEqual(new["id"], current["operations"][0]["id"])
        self.assertEqual(new["identity"]["revision"], self.service.ci_head)
        self.assertEqual(new["run"], self.service.run)
        self.assertEqual(new["packetId"], packet["packetId"])
        scope = self.scope(envelope["context"])
        self.assertEqual(scope["commitsAhead"], 2)
        self.assertEqual(scope["commitRoom"], 1)
        self.assertTrue(scope["scopeVerified"])
        self.assertIn('"commitRoom": 1', prompt)
        body = self.service.posts()[0][2]
        self.assertEqual(set(body), {"prompt", "base_ref", "head_ref", "create_pull_request"})
        self.assertFalse(body["create_pull_request"])
        self.assertIn("exactly one", body["prompt"])

    def test_three_commits_allow_observation_but_no_repair_spending(self):
        self.service.commit_shas.append("e" * 40)
        self.service.pr["head"]["sha"] = self.service.ci_head = self.service.commit_shas[-1]
        packet, envelope, _ = self.prepare()
        self.assertEqual(self.scope(envelope["context"])["commitRoom"], 0)
        with self.assertRaisesRegex(ValueError, "scope|commit room"):
            self.apply(packet)
        self.assert_unchanged()

    def test_labels_only_chain_with_empty_cumulative_diff_still_has_room(self):
        self.service.comparison_override = {"files": []}
        packet, envelope, _ = self.prepare()
        self.assertTrue(self.scope(envelope["context"])["scopeVerified"])
        self.assertEqual(envelope["context"]["push"]["changedFiles"], [])
        result = self.apply(packet)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)

    def test_every_commit_requires_a_modified_labels_file(self):
        for index, files in enumerate(([], [{"filename": "labels.py", "status": "modified"}],
                                       [{"filename": LABELS["filename"], "status": "added"}],
                                       [{**LABELS, "previous_filename": "tests.py"}])):
            with self.subTest(files=files):
                self.work = self.work / str(index)
                self.work.mkdir()
                self.service.commit_files["c" * 40] = files
                packet, envelope, _ = self.prepare()
                self.assertFalse(self.scope(envelope["context"])["scopeVerified"])
                with self.assertRaisesRegex(ValueError, "scope"):
                    self.apply(packet)
                self.assert_unchanged()

    def test_changed_then_reverted_tests_or_workflow_cannot_hide_in_net_diff(self):
        for index, path in enumerate((".ci-shepherd-fixture/test_labels.py", ".github/workflows/ci-shepherd-fixture.yml")):
            with self.subTest(path=path):
                self.service.commit_files = {
                    sha: [LABELS, {"filename": path, "status": "modified"}] for sha in self.service.commit_shas}
                self.work = self.work / str(index)
                self.work.mkdir()
                packet, envelope, _ = self.prepare()
                self.assertFalse(self.scope(envelope["context"])["scopeVerified"])
                with self.assertRaisesRegex(ValueError, "scope"):
                    self.apply(packet)
                self.assert_unchanged()

    def test_incomplete_or_non_linear_scope_is_never_dispatch_admission(self):
        mutations = [
            {"commits": [{"sha": "d" * 40}]},
            {"commits": [{"sha": "c" * 40}, {"sha": "c" * 40}]},
            {"merge_base_commit": {"sha": "a" * 40}},
            {"base_commit": {"sha": "a" * 40}},
            {"total_commits": 3},
            {"ahead_by": True},
            {"behind_by": 1},
            {"status": "diverged"},
        ]
        for index, change in enumerate(mutations):
            with self.subTest(change=change):
                self.service.comparison_override = change
                self.work = self.work / str(index)
                self.work.mkdir()
                packet, envelope, _ = self.prepare()
                self.assertFalse(self.scope(envelope["context"])["scopeVerified"])
                with self.assertRaisesRegex(ValueError, "scope"):
                    self.apply(packet)
                self.assert_unchanged()

    def test_unproven_commit_file_pagination_is_not_complete_scope(self):
        self.service.commit_headers["c" * 40] = {"Link": '<https://api.github.com/next>; rel="next"'}
        with self.assertRaisesRegex(ValueError, "commit|scope|pagination"):
            self.prepare()
        self.assert_unchanged()

    def test_prepared_scope_source_or_head_mismatch_cannot_authorize(self):
        service = deepcopy(self.service)
        for index, (key, wrong) in enumerate((
            ("workflowSha", "a" * 40), ("headSha", live.INITIAL_HEAD), ("commitRoom", 3),
            ("commitRoom", True), ("commitsAhead", 2.0),
        )):
            with self.subTest(key=key):
                self.work = self.work / str(index)
                self.work.mkdir()
                self.service = deepcopy(service)
                packet, envelope, _ = self.prepare()
                context = deepcopy(envelope["context"])
                self.scope(context)
                context["repairScope"][key] = wrong
                with self.assertRaisesRegex(ValueError, "scope"):
                    self.apply(packet, context)
                self.assert_unchanged()

    def test_fresh_scope_ineligibility_before_first_mutation_spends_nothing(self):
        packet, _, _ = self.prepare()
        self.service.commit_files["c" * 40] = [LABELS, {"filename": "tests.py", "status": "modified"}]
        with self.assertRaisesRegex(ValueError, "scope"):
            self.apply(packet)
        self.assert_unchanged()

    def test_pending_worker_never_dispatches_second_round(self):
        packet, _, _ = self.prepare()
        self.service.tasks[TASK]["state"] = self.service.tasks[TASK]["sessions"][0]["state"] = "in_progress"
        with self.assertRaisesRegex(ValueError, "active|capacity|worker"):
            self.apply(packet)
        self.assert_unchanged()

    def test_approval_blocked_or_manual_only_failure_has_no_repair_evidence(self):
        for index, approval in enumerate((True, False)):
            with self.subTest(approval=approval):
                self.work = self.work / str(index)
                self.work.mkdir()
                if approval:
                    self.service.ci_conclusion = "action_required"
                    self.service.ci_jobs = {"total_count": 0, "jobs": []}
                else:
                    self.service.ci_extra_runs = [{
                        "id": 11, "run_attempt": 1, "head_sha": self.service.pr["head"]["sha"],
                        "path": live.FIXTURE_WORKFLOW, "event": "workflow_dispatch", "pull_requests": [],
                        "status": "completed", "conclusion": "failure"}]
                    original = self.service.transport

                    def manual_only(method, endpoint, body):
                        response = original(method, endpoint, body)
                        if urlparse(endpoint).path.endswith("/workflows/200/runs"):
                            return Response({"workflow_runs": self.service.ci_extra_runs}, {})
                        return response

                    self.service.transport = manual_only
                packet, envelope, _ = self.prepare()
                self.assertEqual(packet["basis"]["feedback"], [])
                self.assertFalse(envelope["context"]["gate"]["ciPassed"])
                self.assertFalse(envelope["context"]["gate"]["ready"])
                with self.assertRaises(ValueError):
                    self.apply(packet)
                self.assert_unchanged()

    def test_scope_loss_during_prepared_publication_is_rechecked_before_reservation(self):
        packet, _, _ = self.prepare()

        def changed(service, method, endpoint):
            if method == "PATCH":
                service.commit_files["c" * 40] = [{"filename": "tests.py", "status": "modified"}]

        self.service.before_request = changed
        with self.assertRaisesRegex(ValueError, "scope"):
            self.apply(packet)
        current = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(current["repairBatches"], 1)
        self.assertEqual(current["operations"][-1]["state"], "prepared")
        self.assertEqual(self.service.posts(), [])

    def test_scope_loss_at_final_send_guard_holds_consumed_capacity_without_post(self):
        packet, _, _ = self.prepare()
        original = live.ExistingPRExecutor.prompt

        def render(executor, operation, trial):
            result = original(executor, operation, trial)
            self.service.commit_files["c" * 40] = [{"filename": "tests.py", "status": "modified"}]
            return result

        with patch.object(live.ExistingPRExecutor, "prompt", render):
            with self.assertRaisesRegex(ValueError, "scope"):
                self.apply(packet)
        current = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(current["repairBatches"], 2)
        self.assertEqual(current["operations"][-1]["state"], "consumed")
        self.assertEqual(self.service.posts(), [])
        self.assertEqual(receipts.trial_tuple(current), TRIAL)

    def test_final_send_guard_rechecks_head_and_feedback(self):
        original = live.ExistingPRExecutor.prompt
        for index, change_head in enumerate((True, False)):
            with self.subTest(change_head=change_head):
                self.work = self.work / str(index)
                self.work.mkdir()
                packet, _, _ = self.prepare()

                def render(executor, operation, trial):
                    result = original(executor, operation, trial)
                    if change_head:
                        self.service.pr["head"]["sha"] = self.service.ci_head = "e" * 40
                        self.service.commit_shas.append("e" * 40)
                    else:
                        self.service.ci_conclusion = "success"
                    return result

                with patch.object(live.ExistingPRExecutor, "prompt", render):
                    with self.assertRaisesRegex(ValueError, "head|feedback|basis"):
                        self.apply(packet)
                self.assertEqual(self.service.posts(), [])
                self.assertEqual(receipts.parse_body(self.service.comments[0]["body"])["repairBatches"], 2)
                if change_head:
                    self.service.comments[0]["body"] = receipts.render_record(self.confirmed)
                    self.service.pr["head"]["sha"] = self.service.ci_head = "d" * 40
                    self.service.commit_shas.pop()

    def test_second_dispatch_has_no_get_after_final_clock_check(self):
        packet, _, _ = self.prepare()
        trace = []
        self.service.before_request = lambda service, method, endpoint: trace.append((method, endpoint))

        def clock():
            trace.append(("clock", None))
            return self.now

        result = self.apply(packet, clock_fn=clock)
        self.assertEqual(result["outcome"], "confirmed")
        send = next(index for index, event in enumerate(trace) if event == (
            "POST", f"agents/repos/{live.REPOSITORY}/tasks"))
        self.assertEqual(trace[send - 1], ("clock", None))

    def test_second_round_commit_reads_pass_real_transport_allowlist(self):
        opener = WindowOpener()
        opener.service = self.service
        opener.now = self.now
        opener.reset = self.now + timedelta(seconds=60)
        transport = live.HTTPTransport("fixture-only", write=True, opener=opener,
                                       clock_fn=opener.clock, sleep_fn=opener.sleep)
        packet, _, _ = self.prepare(transport=transport)
        # Prepare and apply are separate hosted jobs, with fresh native reasoning
        # between them. Exercise apply in a new quota window, still within packet expiry.
        opener.now += timedelta(seconds=61)
        transport = live.HTTPTransport("fixture-only", write=True, opener=opener,
                                       clock_fn=opener.clock, sleep_fn=opener.sleep)
        result = self.apply(packet, transport=transport, clock_fn=opener.clock)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)
        for sha in self.service.commit_shas:
            self.assertTrue(any(f"/commits/{sha}?" in endpoint for method, endpoint, _ in self.service.calls
                                if method == "GET"))

    def test_initial_and_one_commit_scopes_report_exact_room(self):
        for index, count in enumerate((0, 1)):
            with self.subTest(count=count):
                self.work = self.work / str(index)
                self.work.mkdir()
                self.service.commit_shas = ["c" * 40] * count
                self.service.pr["head"]["sha"] = self.service.ci_head = (
                    self.service.commit_shas[-1] if count else live.INITIAL_HEAD)
                _, envelope, _ = self.prepare()
                scope = self.scope(envelope["context"])
                self.assertTrue(scope["scopeVerified"])
                self.assertEqual(scope["commitsAhead"], count)
                self.assertEqual(scope["commitRoom"], 3 - count)
