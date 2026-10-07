"""Public restarted coordination boundaries; all service effects are simulated."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch
import json

from helpers import reconciliation_evidence, WorkspaceTest
import test_pilot_lifecycle as lifecycle
from test_pilot_lifecycle import RUN, PREFIX, decision
from test_pilot_github import Transport, pr
from helpers import FakeClock, result_capable
from github import Response
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_state as state
import local
import hosted
import live
import round as contracts
import test_local as local_fixtures


class WaitTests(WorkspaceTest, unittest.TestCase):
    until = "2026-10-04T01:00:00Z"
    fresh = lifecycle.LifecycleTests.fresh
    ledger = lifecycle.LifecycleTests.ledger
    prepare = lifecycle.LifecycleTests.prepare
    settle = lifecycle.LifecycleTests.settle
    task_writes = lifecycle.LifecycleTests.task_writes
    check = lifecycle.LifecycleTests.check
    start = lifecycle.LifecycleTests.start
    finish = lifecycle.LifecycleTests.finish
    observed = lifecycle.LifecycleTests.observed

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.transport = lifecycle.LifecycleTransport()
        self.transport.values[PREFIX + "/issues"] = [dict(pr(), pull_request={})]
        self.transport.values[PREFIX + "/pulls/7"] = pr()
        self.transport.values[PREFIX + "/issues/7/comments"] = [{
            "id": 20, "body": "Reassess at " + self.until, "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]

    def wait(self, packet):
        value = decision(packet)
        value.update(action="wait", wait={"until": self.until, "reason": "External service reassessment."},
                     dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        api = self.fresh()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = pilot.settle(api, packet, reconciliation_evidence(value), 2, self.clock())
        self.assertEqual(api.ledger, self.ledger())
        return result

    def test_wait_survives_restart_without_repeated_paid_admission(self):
        self.transport.values[PREFIX + "/issues/7/comments"][0]["body"] = (
            "[automated] External service unavailable; reassess at " + self.until)
        packet = self.prepare()
        self.assertEqual({"outcome": "deferred", "until": self.until}, self.wait(packet))
        before = deepcopy(self.ledger())
        self.assertEqual({}, before["chains"][0]["dispositions"])
        self.assertEqual((2, 0, None), tuple(before["chains"][0]["operations"][0][key]
                         for key in ("nativeActual", "workerReserved", "taskId")))
        self.assertIsNone(self.prepare())
        self.assertEqual(before, self.ledger())
        self.assertEqual([], self.task_writes())
        self.assertIn(self.until, self.logs)

    def legacy_completed_worker(self, *, report_is_legacy):
        if not report_is_legacy:
            self.transport.values[PREFIX + "/issues/7/comments"][0]["body"] = "Legacy repair request."
        first = self.prepare()
        self.assertEqual("waiting", self.settle(first)["outcome"])
        self.finish(usage=1)
        api = self.fresh()
        api.read_authority()
        api.reconcile_workers()
        api.persist()
        ledger = self.ledger()
        chain = ledger["chains"][0]
        operation = chain["operations"][0]
        operation.pop("feedbackDecisions")
        identities = json.loads(operation["identity"].rsplit(":round:", 1)[0])["feedback"]
        chain["dispositions"].update({identity: "needs-human" for identity in identities})
        self.transport.comments[0]["body"] = state.render(ledger)
        if not report_is_legacy:
            self.transport.values[PREFIX + "/issues/7/comments"].append({
                "id": 92, "body": "[automated] Reassess at " + self.until,
                "updated_at": "2026-10-04T00:01:00Z", "user": {"id": 1472, "login": "radical"}})
        return deepcopy(operation), deepcopy(chain["dispositions"])

    def test_wait_deferral_preserves_legacy_worker_feedback_through_restart_and_expiry(self):
        for report_is_legacy in (False, True):
            with self.subTest(report_is_legacy=report_is_legacy):
                self.setUp()
                worker, dispositions = self.legacy_completed_worker(report_is_legacy=report_is_legacy)
                self.transport.values[PREFIX + "/issues/7/comments"].append({
                    "id": 999, "body": "New human reassessment request at " + self.until,
                    "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}})
                packet = self.prepare()
                identities = [item["id"] for item in packet["observation"]["feedback"]]
                self.assertFalse(set(dispositions) & set(identities))
                self.assertEqual("deferred", self.wait(packet)["outcome"])
                before = deepcopy(self.ledger())
                self.assertIsNone(self.prepare(), "an unchanged wait must not admit another paid packet")
                self.assertEqual(identities, [item["id"] for item in self.observed()["feedback"]])
                self.assertEqual(before, self.ledger())
                self.clock.advance(hours=1)
                due = self.prepare()
                self.assertIsNotNone(due, "legacy deadline feedback must still wake at exact expiry")
                self.assertEqual(identities, [item["id"] for item in due["observation"]["feedback"]])
                chain = self.ledger()["chains"][0]
                self.assertEqual(worker["id"], chain["operations"][0]["id"])
                self.assertEqual(dispositions, chain["dispositions"])
                self.assertEqual(3, chain["rounds"])
                self.assertEqual(1, len(self.task_writes()))

    def test_same_sha_branch_drift_rejects_prepared_dispatch(self):
        packet = self.prepare()
        self.transport.values[PREFIX + "/pulls/7"]["head"]["ref"] = "another-branch"
        result = self.settle(packet)
        self.assertEqual("failed", result["outcome"])
        self.assertIn("basis changed", result["error"])
        self.assertEqual([], self.task_writes())

    def test_same_id_raw_diagnostic_change_supersedes_wait_before_truncation(self):
        self.transport.values[PREFIX + "/issues/7/comments"][0]["body"] = "Reassess at " + self.until
        self.check("a" * 40, "failure")
        check = self.transport.values[PREFIX + "/commits/" + "a" * 40 + "/check-runs"]["check_runs"][0]
        check["output"]["summary"] = "x" * 3000 + "old diagnosis"
        packet = self.prepare()
        self.assertEqual("deferred", self.wait(packet)["outcome"])
        self.assertIsNone(self.prepare())
        check["output"]["summary"] = "x" * 3000 + "new diagnosis"
        next_packet = self.prepare()
        self.assertIsNotNone(next_packet)
        self.assertEqual(2, self.ledger()["chains"][0]["rounds"])
        self.assertFalse(next_packet["observation"]["ready"])

    def test_changed_saved_worker_error_does_not_causally_supersede_wait(self):
        self.start()
        self.finish("failed", error="Original failure")
        self.transport.values[PREFIX + "/issues/7/comments"] = [{
            "id": 91, "body": "Reassess at " + self.until, "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        packet = self.prepare()
        self.assertEqual("deferred", self.wait(packet)["outcome"])
        self.assertIsNone(self.prepare())
        self.transport.values["agents/repos/radical/aspire/tasks/TASK1"]["sessions"][0]["error"]["message"] = "Revised failure"
        self.assertIsNone(self.prepare())

    def test_fresh_source_and_approved_feedback_changes_wake_wait_before_deadline(self):
        for change in ("head", "branch", "body", "same-comment-body", "new-comment", "resolved-review"):
            with self.subTest(change=change):
                self.setUp()
                if change == "resolved-review":
                    comment = deepcopy(self.transport.values[PREFIX + "/issues/7/comments"][0])
                    comment["id"] = 92
                    self.transport.values[PREFIX + "/pulls/7/comments"] = [comment]
                first = self.prepare()
                self.assertEqual("deferred", self.wait(first)["outcome"])
                self.assertIsNone(self.prepare())
                value = self.transport.values[PREFIX + "/pulls/7"]
                comments = self.transport.values[PREFIX + "/issues/7/comments"]
                if change == "head":
                    value["head"]["sha"] = "c" * 40
                elif change == "branch":
                    value["head"]["ref"] = "different-branch"
                elif change == "body":
                    value["body"] += " New source scope."
                elif change == "same-comment-body":
                    comments[0]["body"] += " Additional objection."
                elif change == "new-comment":
                    comments.append({**deepcopy(comments[0]), "id": 93, "body": "New human objection."})
                else:
                    self.transport.resolved_reviews.add(92)
                next_packet = self.prepare()
                self.assertIsNotNone(next_packet)
                self.assertEqual(2, self.ledger()["chains"][0]["rounds"])
                self.assertEqual([], self.task_writes())

    def test_pr_exact_expiry_reobserves_red_ci_without_worker_or_rerun(self):
        self.check("a" * 40, "failure")
        packet = self.prepare()
        self.assertEqual("deferred", self.wait(packet)["outcome"])
        history = deepcopy(self.ledger()["chains"][0]["operations"][0])
        self.clock.advance(minutes=59, seconds=59)
        self.assertIsNone(self.prepare())
        self.clock.advance(seconds=1)
        next_packet = self.prepare()
        self.assertIsNotNone(next_packet)
        self.assertFalse(next_packet["observation"]["ready"])
        self.assertEqual("failure", next_packet["observation"]["feedback"][0]["body"].split(": ")[-1])
        self.assertEqual(history, self.ledger()["chains"][0]["operations"][0])
        self.assertEqual([], self.task_writes())
        self.assertFalse(any("/rerun" in endpoint for _, endpoint, _ in self.transport.writes))

    def issue(self):
        value = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "External service wait",
                 "body": "Reassess at " + self.until}
        self.transport.values[PREFIX + "/issues"] = [value]
        self.transport.values[PREFIX + "/issues/8"] = value
        self.transport.values[PREFIX + "/issues/8/comments"] = []
        return value

    def test_empty_feedback_issue_wait_restores_initial_due_at_exact_deadline(self):
        self.issue()
        first = self.prepare()
        self.assertEqual([], first["observation"]["feedback"])
        self.assertEqual("deferred", self.wait(first)["outcome"])
        self.assertIsNone(self.prepare())
        self.clock.advance(hours=1)
        second = self.prepare()
        self.assertIsNotNone(second)
        self.assertEqual("issue", second["observation"]["kind"])
        self.assertEqual(2, self.ledger()["chains"][0]["rounds"])
        self.assertEqual([], self.task_writes())

    def test_issue_body_edit_supersedes_wait_without_reopening_handoff(self):
        value = self.issue()
        self.assertEqual("deferred", self.wait(self.prepare())["outcome"])
        value["body"] += " New implementation requirements."
        packet = self.prepare()
        self.assertIsNotNone(packet)
        native = decision(packet)
        native["action"] = "human"
        self.assertEqual("human", pilot.settle(self.fresh(), packet, reconciliation_evidence(native), 2, self.clock())["outcome"])
        value["body"] += " Further changes."
        self.assertIsNone(self.prepare())

    def child_wait(self):
        self.issue()
        first = self.prepare()
        self.assertEqual("waiting", self.settle(first)["outcome"])
        self.finish(usage=1)
        child = pr(9)
        self.transport.values[PREFIX + "/pulls"] = [child]
        self.transport.values[PREFIX + "/pulls/9"] = child
        self.transport.values[PREFIX + "/git/ref/heads/fix-9"] = {
            "ref": "refs/heads/fix-9", "object": {"sha": "a" * 40}}
        self.transport.values[PREFIX + "/issues/9/comments"] = [{
            "id": 91, "body": "Reassess at " + self.until, "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        self.transport.values[PREFIX + "/issues"].append(dict(child, pull_request={}))
        task = self.transport.values["agents/repos/radical/aspire/tasks/TASK1"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": child["id"], "global_id": child["node_id"]}},
            {"provider": "github", "type": "branch", "data": {"base_ref": "main", "head_ref": "fix-9"}}]
        packet = self.prepare()
        self.assertEqual((9, "pr"), (packet["observation"]["number"], packet["observation"]["kind"]))
        self.assertEqual("deferred", self.wait(packet)["outcome"])
        return packet

    def test_issue_child_wait_uses_child_identity_and_keeps_history(self):
        self.child_wait()
        before = deepcopy(self.ledger())
        self.assertIsNone(self.prepare())
        self.assertEqual(before, self.ledger())
        self.clock.advance(hours=1)
        self.assertEqual(9, self.prepare()["observation"]["number"])
        chain = self.ledger()["chains"][0]
        self.assertEqual((8, 9, 3), (chain["origin"], chain["child"], chain["rounds"]))

    def test_confirmed_child_adoption_clears_adoption_pause_not_a_genuine_wait(self):
        self.child_wait()
        ledger = self.ledger()
        chain = ledger["chains"][0]
        chain.update(state="human", childAdoption="uncertain")
        self.transport.comments[0]["body"] = state.render(ledger)
        self.transport.values[PREFIX + "/pulls/9"]["labels"] = []
        operations = deepcopy(chain["operations"])
        self.assertIsNone(self.prepare())
        self.assertEqual(("human", "uncertain"), (
            self.ledger()["chains"][0]["state"], self.ledger()["chains"][0]["childAdoption"]))
        self.transport.values[PREFIX + "/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual(("open", "confirmed"), (chain["state"], chain["childAdoption"]))
        self.assertEqual(operations, chain["operations"])
        self.assertNotIn("reminder", chain)

    def test_ordinary_handoffs_and_declines_do_not_gain_timed_wait_expiry(self):
        for disposition, outcome in (("needs-human", "human"), ("declined", "declined")):
            with self.subTest(disposition=disposition):
                self.setUp()
                packet = self.prepare()
                native = decision(packet)
                native.update(action="human", dispositions={item["id"]: disposition
                              for item in packet["observation"]["feedback"]})
                self.assertEqual(outcome, pilot.settle(
                    self.fresh(), packet, reconciliation_evidence(native), 2, self.clock())["outcome"])
                before = deepcopy(self.ledger()["chains"][0])
                self.assertNotIn("wait", before["operations"][0])
                self.clock.advance(hours=2)
                self.assertIsNone(self.prepare())
                self.assertEqual(before, self.ledger()["chains"][0])
                self.assertEqual([], self.task_writes())

    def test_invalid_waits_fail_closed_after_billing_and_preserve_no_worker(self):
        invalid = [
            {"until": "2026-10-04T01:00:00", "reason": "No zone"},
            {"until": "2026-10-04T01:00:00+00:00", "reason": "Not canonical"},
            {"until": "2026-10-04T00:00:00Z", "reason": "Expired"},
            {"until": "2026-10-04T02:00:00Z", "reason": "Invented"},
            {"until": self.until, "reason": ""},
            {"until": self.until, "reason": "x" * 501},
            {"until": self.until, "reason": "line\nbreak"},
            {"until": self.until, "reason": []},
            [],
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.setUp()
                packet = self.prepare()
                native = decision(packet)
                native.update(action="wait", wait=value,
                              dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
                result = pilot.settle(self.fresh(), packet, reconciliation_evidence(native), None, self.clock())
                self.assertEqual("failed", result["outcome"])
                self.assertTrue(result["error"])
                operation = self.ledger()["chains"][0]["operations"][0]
                self.assertEqual((None, 30, 0), tuple(operation[key]
                                 for key in ("nativeActual", "nativeReserved", "workerReserved")))
                self.assertNotIn("wait", operation)
                self.assertEqual([], self.task_writes())

    def test_deferrals_and_wait_extra_fields_are_closed_per_variant(self):
        for action, dispositions, extra in (
                ("wait", "addressed", {"wait": {"until": self.until, "reason": "External"}}),
                ("wait", "needs-human", {"wait": {"until": self.until, "reason": "External"}}),
                ("cloud", "deferred", {}),
                ("human", "declined", {"wait": {"until": self.until, "reason": "External"}})):
            with self.subTest(action=action, dispositions=dispositions):
                self.setUp()
                packet = self.prepare()
                value = decision(packet)
                value.update(action=action, dispositions={item["id"]: dispositions
                             for item in packet["observation"]["feedback"]}, **extra)
                result = pilot.settle(self.fresh(), packet, reconciliation_evidence(value), 2, self.clock())
                self.assertEqual("failed", result["outcome"])
                self.assertEqual(2, self.ledger()["chains"][0]["operations"][0]["nativeActual"])
                self.assertEqual([], self.task_writes())

    def test_visible_deadline_cannot_be_taken_from_comment_metadata_or_foreign_author(self):
        for user in ({"id": 1472, "login": "radical"}, {"id": 999, "login": "unapproved"}):
            with self.subTest(user=user):
                self.setUp()
                comment = self.transport.values[PREFIX + "/issues/7/comments"][0]
                comment.update(body="No deadline", updated_at=self.until, user=user)
                self.check("a" * 40, "failure")
                self.assertEqual("failed", self.wait(self.prepare())["outcome"])
                self.assertEqual([], self.task_writes())

    def test_synthetic_ci_dates_cannot_authorize_wait_without_approved_reports(self):
        for source in ("check", "status", "workflow"):
            with self.subTest(source=source):
                self.setUp()
                self.transport.values[PREFIX + "/issues/7/comments"] = []
                name = "test-" + self.until
                if source == "check":
                    self.check("a" * 40, "failure")
                    checks = self.transport.values[PREFIX + "/commits/" + "a" * 40 + "/check-runs"]
                    checks["check_runs"][0]["name"] = name
                elif source == "status":
                    self.transport.values[PREFIX + "/commits/" + "a" * 40 + "/status"] = {
                        "state": "failure", "statuses": [{
                            "id": 90, "context": name, "state": "failure", "target_url": ""}]}
                else:
                    # Unexpected external CI text is still synthetic evidence,
                    # never a report from an approved comment/review author.
                    self.transport.values[PREFIX + "/actions/runs"] = {
                        "total_count": 1, "workflow_runs": [{
                            "id": 10, "run_attempt": 1, "head_sha": "a" * 40, "status": "completed",
                            "conclusion": "failure; reassess at " + self.until, "name": name,
                            "repository": {"id": 746880239, "full_name": "radical/aspire"},
                            "html_url": "https://github.com/radical/aspire/actions/runs/10"}]}
                packet = self.prepare()
                self.assertEqual(1, len(packet["observation"]["feedback"]))
                self.assertTrue(packet["observation"]["feedback"][0]["id"].startswith(source + ":"))
                self.assertIn(self.until, packet["observation"]["feedback"][0]["body"])
                result = self.wait(packet)
                self.assertEqual("failed", result["outcome"])
                self.assertIn("approved feedback", result["error"])
                operation = self.ledger()["chains"][0]["operations"][0]
                self.assertEqual(2, operation["nativeActual"])
                self.assertNotIn("wait", operation)
                self.assertEqual({}, self.ledger()["chains"][0]["dispositions"])
                self.assertEqual([], self.task_writes())

    def test_approved_inline_and_review_bodies_remain_valid_wait_witnesses(self):
        for source in ("review-comment", "review"):
            with self.subTest(source=source):
                self.setUp()
                self.transport.values[PREFIX + "/issues/7/comments"] = []
                report = {"id": 92, "body": "[automated] Reassess at " + self.until,
                          "user": {"id": 1472, "login": "radical"}}
                if source == "review-comment":
                    report["updated_at"] = "2026-10-04T00:00:00Z"
                    self.transport.values[PREFIX + "/pulls/7/comments"] = [report]
                else:
                    report.update(state="COMMENTED", commit_id="a" * 40,
                                  submitted_at="2026-10-04T00:00:00Z")
                    self.transport.values[PREFIX + "/pulls/7/reviews"] = [report]
                packet = self.prepare()
                self.assertEqual(1, len(packet["observation"]["feedback"]))
                self.assertTrue(packet["observation"]["feedback"][0]["id"].startswith(source + ":"))
                self.assertEqual("deferred", self.wait(packet)["outcome"])
                before = deepcopy(self.ledger())
                self.assertIsNone(self.prepare())
                self.assertEqual(before, self.ledger())
                self.assertEqual([], self.task_writes())

    def test_wait_replay_keeps_charge_and_original_history(self):
        packet = self.prepare()
        self.assertEqual("deferred", self.wait(packet)["outcome"])
        before = deepcopy(self.ledger())
        self.assertEqual("replay", self.wait(packet)["outcome"])
        self.assertEqual(before, self.ledger())

    def test_wait_final_guard_rejects_elapsed_deadline_and_management_races(self):
        for race in ("deadline", "hands-off", "closed", "body", "branch", "diagnostics"):
            with self.subTest(race=race):
                self.setUp()
                packet = self.prepare()
                api = self.fresh()
                guard = api.guard

                def racing_guard(chain, observed):
                    value = self.transport.values[PREFIX + "/pulls/7"]
                    if race == "deadline":
                        self.clock.advance(hours=1)
                    elif race == "hands-off":
                        value["labels"].append({"name": "shepherd-hands-off"})
                    elif race == "closed":
                        value["state"] = "closed"
                    elif race == "body":
                        value["body"] += " Changed"
                    elif race == "branch":
                        value["head"]["ref"] = "different"
                    else:
                        self.check("a" * 40, "failure")
                    return guard(chain, observed)

                api.guard = racing_guard
                value = decision(packet)
                value.update(action="wait", wait={"until": self.until, "reason": "External"},
                             dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
                result = pilot.settle(api, packet, reconciliation_evidence(value), 2, self.clock())
                self.assertEqual("failed", result["outcome"])
                self.assertEqual([], self.task_writes())
                self.assertNotIn("wait", self.ledger()["chains"][0]["operations"][0])

    def test_unknown_ci_and_pending_worker_races_block_wait_without_refunding_usage(self):
        for race in ("unknown-ci", "pending-worker"):
            with self.subTest(race=race):
                self.setUp()
                self.start()
                self.finish("failed")
                self.transport.values[PREFIX + "/issues/7/comments"] = [{
                    "id": 91, "body": "Reassess at " + self.until, "updated_at": "2026-10-04T00:00:00Z",
                    "user": {"id": 1472, "login": "radical"}}]
                packet = self.prepare()
                if race == "pending-worker":
                    task = self.transport.values["agents/repos/radical/aspire/tasks/TASK1"]
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
                else:
                    # The service boundary returns an incomplete run inventory,
                    # rather than silently converting missing evidence to green.
                    self.transport.values[PREFIX + "/actions/runs"] = {"workflow_runs": [], "total_count": 1}
                result = self.wait(packet)
                self.assertEqual("failed", result["outcome"])
                operations = self.ledger()["chains"][0]["operations"]
                self.assertEqual(2, operations[-1]["nativeActual"])
                self.assertNotIn("wait", operations[-1])
                self.assertEqual(1, len(self.task_writes()))

    def test_disabled_settlement_accounts_native_only_and_sweeps_respect_stop_labels(self):
        packet = self.prepare()
        value = decision(packet)
        value.update(action="wait", wait={"until": self.until, "reason": "External"},
                     dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        self.assertEqual({"outcome": "billing-only"}, pilot.settle(
            self.fresh(), packet, reconciliation_evidence(value), 2, self.clock(), billing_only=True))
        operation = self.ledger()["chains"][0]["operations"][0]
        self.assertEqual((2, "failed"), (operation["nativeActual"], operation["state"]))
        self.assertNotIn("wait", operation)
        for stop in ("closed", "unadopted", "hands-off"):
            with self.subTest(stop=stop):
                self.setUp()
                self.assertEqual("deferred", self.wait(self.prepare())["outcome"])
                before = deepcopy(self.ledger()["chains"][0]["operations"])
                value = self.transport.values[PREFIX + "/pulls/7"]
                if stop == "closed":
                    value["state"] = "closed"
                elif stop == "unadopted":
                    value["labels"] = []
                else:
                    value["labels"].append({"name": "shepherd-hands-off"})
                self.clock.advance(hours=1)
                self.assertIsNone(self.prepare())
                self.assertEqual(before, self.ledger()["chains"][0]["operations"])

    def test_stale_native_session_and_expired_packet_cannot_save_wait(self):
        self.assertEqual("deferred", self.wait(self.prepare())["outcome"])
        self.transport.values[PREFIX + "/pulls/7"]["body"] += " New evidence."
        packet = self.prepare()
        prior_session = self.ledger()["chains"][0]["operations"][0]["sessionId"]
        value = decision(packet)
        value.update(action="wait", wait={"until": self.until, "reason": "External"},
                     dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        evidence = reconciliation_evidence(value)
        original_session = evidence["sessionId"]
        evidence = json.loads(json.dumps(evidence).replace(original_session, prior_session))
        result = pilot.settle(self.fresh(), packet, evidence, 2, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertIn("session reused", result["error"])
        self.setUp()
        packet = self.prepare()
        self.clock.advance(minutes=10)
        self.assertEqual("failed", self.wait(packet)["outcome"])

    def test_wait_persistence_uncertainty_is_not_masked_or_compensated(self):
        packet = self.prepare()
        api = self.fresh()
        persist = api.persist
        calls = []

        def uncertain():
            calls.append(deepcopy(api.ledger))
            if len(calls) == 2:
                raise github.AuthorityUncertain("wait authority receipt unknown")
            return persist()

        api.persist = uncertain
        value = decision(packet)
        value.update(action="wait", wait={"until": self.until, "reason": "External"},
                     dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        with self.assertRaisesRegex(github.AuthorityUncertain, "receipt unknown"):
            pilot.settle(api, packet, reconciliation_evidence(value), 2, self.clock())
        self.assertEqual(2, len(calls))
        self.assertEqual(2, self.ledger()["chains"][0]["operations"][0]["nativeActual"])
        self.assertEqual("completed", api.ledger["chains"][0]["operations"][0]["state"])
        self.assertEqual([], self.task_writes())

    def test_rejected_wait_persistence_preserves_billing_and_valid_history(self):
        packet = self.prepare()
        api = self.fresh()
        persist = api.persist
        calls = []

        def rejected():
            calls.append(deepcopy(api.ledger))
            if len(calls) == 2:
                raise ValueError("authority body bound exhausted")
            return persist()

        api.persist = rejected
        value = decision(packet)
        value.update(action="wait", wait={"until": self.until, "reason": "External"},
                     dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        result = pilot.settle(api, packet, reconciliation_evidence(value), 2, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertIn("body bound", result["error"])
        operation = self.ledger()["chains"][0]["operations"][0]
        self.assertEqual(2, operation["nativeActual"])
        self.assertNotIn("wait", operation)
        self.assertEqual([], self.task_writes())

    def test_unchanged_wait_does_not_grow_historical_unknown_hold_when_spend_ages_out(self):
        self.until = "2026-10-06T01:00:00Z"
        self.start()
        self.finish("failed", usage=None)
        # Seed an existing bounded unknown hold and known spending in the fake
        # authority. These are historical receipts, not a new admission refund.
        ledger = self.ledger()
        ledger["chains"][0]["operations"][0]["workerReserved"] = 100
        other = state.adopt(ledger, 8, "pr", "NODE8")
        prior = state.reserve(ledger, other, "historical-spend", self.clock(), local=False)
        state.settle_native(prior, 850)
        state.finish(prior, "completed")
        other["state"] = "human"
        self.transport.comments[0]["body"] = state.render(ledger)
        self.transport.values[PREFIX + "/pulls/8"] = pr(8)
        self.transport.values[PREFIX + "/issues/7/comments"] = [{
            "id": 91, "body": "Reassess at " + self.until, "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        self.assertEqual("deferred", self.wait(self.prepare())["outcome"])
        history = deepcopy(self.ledger()["chains"][0]["operations"])
        self.clock.advance(hours=25)
        self.assertIsNone(self.prepare())
        self.assertEqual(history, self.ledger()["chains"][0]["operations"])


class UpstreamTransport(Transport):
    target = "repos/microsoft/aspire"
    tasks = "agents/repos/microsoft/aspire/tasks"

    def __init__(self):
        super().__init__()
        self.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        self.values[self.target] = {"id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        for number in (7, 8):
            value = pr(number)
            value["head"]["repo"] = value["base"]["repo"] = deepcopy(self.values[self.target])
            self.values[self.target + "/pulls/" + str(number)] = value
            self.values[self.target + "/issues/" + str(number) + "/comments"] = [{
                "id": 20 + number, "body": "Repair this PR", "updated_at": "2026-10-04T00:00:00Z",
                "user": {"id": 1472, "login": "radical"}}]
        self.values[self.target + "/issues"] = [
            dict(self.values[self.target + "/pulls/" + str(number)], pull_request={}) for number in (7, 8)]

    def __call__(self, method, endpoint, body):
        if method == "POST" and endpoint == self.tasks:
            self.writes.append((method, endpoint, deepcopy(body)))
            identity = "TASK" + str(sum(write[1] == self.tasks for write in self.writes))
            self.values[self.tasks + "/" + identity] = {
                "id": identity, "state": "queued", "creator": {"id": 1472}, "repository": {"id": 696529789},
                "session_count": 1, "artifacts": [], "updated_at": "2026-10-04T00:00:00Z",
                "sessions": [{"id": "SESSION" + identity, "task_id": identity, "state": "queued",
                              "repository": {"id": 696529789}, "user": {"id": 1472},
                              "base_ref": body["base_ref"], "head_ref": body["head_ref"], "prompt": body["prompt"]}]}
            return Response({"id": identity}, {}, 201)
        if method == "POST" and endpoint.startswith(self.target + "/issues/") and endpoint.endswith("/comments"):
            self.writes.append((method, endpoint, deepcopy(body)))
            comments = self.values.setdefault(endpoint, [])
            value = {"id": 600 + len(self.writes), "body": body["body"], "user": deepcopy(self.values["user"]),
                     "updated_at": "2026-10-04T00:00:00Z"}
            comments.append(value)
            return Response(deepcopy(value), {}, 201)
        return super().__call__(method, endpoint, body)


class UpstreamTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.transport = UpstreamTransport()
        self.clock = FakeClock()

    def fresh(self):
        api = github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True,
                                 binding=bindings.select("upstream"))
        result_capable(api)
        api.clock = self.clock
        return api

    def prepare(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pilot.prepare(self.fresh(), RUN, self.clock(), present=False)

    def settle(self, packet, *, action="cloud", usage=2):
        value = decision(packet)
        if action == "human":
            value.update(action=action, dispositions={item["id"]: "needs-human"
                         for item in packet["observation"]["feedback"]})
        elif action == "wait":
            value.update(action=action, wait={"until": "2026-10-04T01:00:00Z", "reason": "External evidence"},
                         dispositions={item["id"]: "deferred" for item in packet["observation"]["feedback"]})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pilot.settle(self.fresh(), packet, reconciliation_evidence(value), usage, self.clock())

    def test_two_adopted_upstream_branches_dispatch_without_fixed_trial_subject(self):
        first = self.prepare()
        self.assertEqual(("upstream", 7, None),
                         (first["target"], first["observation"]["number"], first["trialBrief"]))
        self.assertEqual("waiting", self.settle(first)["outcome"])
        task = self.transport.values[self.transport.tasks + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 10000000000}
        second = self.prepare()
        self.assertEqual(8, second["observation"]["number"])
        self.assertEqual("waiting", self.settle(second)["outcome"])
        bodies = [body for method, endpoint, body in self.transport.writes if endpoint == self.transport.tasks]
        self.assertEqual(["fix-7", "fix-8"], [body["head_ref"] for body in bodies])
        ledger = state.parse(self.transport.comments[0]["body"])
        self.assertEqual([7, 8], [chain["origin"] for chain in ledger["chains"]])
        self.assertLessEqual(state.repository_spend(ledger, self.clock()), 1000)

    def test_infeasible_worker_headroom_spends_no_native_round(self):
        self.transport.values[self.transport.target + "/issues/7/comments"][0]["body"] = (
            "Reassess at 2026-10-04T01:00:00Z")
        first = self.prepare()
        self.assertEqual("deferred", self.settle(first, action="wait", usage=970)["outcome"])
        before = deepcopy(state.parse(self.transport.comments[0]["body"]))
        self.assertIsNone(self.prepare())
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual([chain["operations"] for chain in before["chains"]],
                         [chain["operations"] for chain in after["chains"]])
        self.assertEqual([1, 0], [chain["rounds"] for chain in after["chains"]])

    def test_unknown_worker_historical_hold_is_not_shrunk_to_admit_another_chain(self):
        first = self.prepare()
        self.assertEqual("waiting", self.settle(first)["outcome"])
        before = state.parse(self.transport.comments[0]["body"])
        hold = before["chains"][0]["operations"][0]["workerReserved"]
        self.clock.advance(hours=25)
        self.assertIsNone(self.prepare())
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(hold, after["chains"][0]["operations"][0]["workerReserved"])
        self.assertEqual(0, after["chains"][1]["rounds"])
        self.assertEqual(1, sum(endpoint == self.transport.tasks for _, endpoint, _ in self.transport.writes))

    def test_deferred_chain_does_not_starve_another_upstream_pr(self):
        self.transport.values[self.transport.target + "/issues/7/comments"][0]["body"] = (
            "Reassess at 2026-10-04T01:00:00Z")
        first = self.prepare()
        self.assertEqual("deferred", self.settle(first, action="wait")["outcome"])
        waiting = deepcopy(state.parse(self.transport.comments[0]["body"])["chains"][0]["operations"])
        second = self.prepare()
        self.assertEqual(8, second["observation"]["number"])
        self.assertEqual("waiting", self.settle(second)["outcome"])
        ledger = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(waiting, ledger["chains"][0]["operations"])
        self.assertEqual("fix-8", self.transport.values[self.transport.tasks + "/TASK1"]["sessions"][0]["head_ref"])

    def test_wait_provenance_never_creates_native_handoff_reminders_or_manual_resume(self):
        self.transport.values[self.transport.target + "/issues"][:] = [
            self.transport.values[self.transport.target + "/issues"][0]]
        self.transport.values[self.transport.target + "/issues/7/comments"][0]["body"] = (
            "Reassess at 2026-10-04T01:00:00Z")
        packet = self.prepare()
        self.assertEqual("deferred", self.settle(packet, action="wait")["outcome"])
        ledger = state.parse(self.transport.comments[0]["body"])
        # A separately paused chain does not convert native wait provenance
        # into a human-handoff operation.
        ledger["chains"][0]["state"] = "human"
        self.transport.comments[0]["body"] = state.render(ledger)
        before = deepcopy(ledger)
        with self.assertRaisesRegex(ValueError, "completed native handoff"):
            local.resume(self.fresh(), packet["operation"], "a" * 40, self.clock())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.fresh(), RUN, self.clock(), present=True))
            self.clock.advance(seconds=60)
            self.assertIsNone(pilot.prepare(self.fresh(), RUN, self.clock(), present=True))
        self.assertEqual(before, state.parse(self.transport.comments[0]["body"]))
        self.assertEqual([], [write for write in self.transport.writes
                             if write[0] == "POST" and write[1].endswith("/comments")])

    def test_upstream_issue_unadopted_and_foreign_sources_cannot_dispatch(self):
        issue = {"id": 1009, "number": 9, "node_id": "NODE9", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}]}
        intake = self.transport.values[self.transport.target + "/issues"]
        intake[:] = [issue]
        with redirect_stderr(io.StringIO()) as output, redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.fresh(), RUN, self.clock(), present=False))
        self.assertIn("PR-only intake", output.getvalue())
        self.assertEqual([], state.parse(self.transport.comments[0]["body"])["chains"])
        for invalid in ("unadopted", "foreign-head", "foreign-base", "base-ref", "node"):
            with self.subTest(invalid=invalid):
                self.setUp()
                value = self.transport.values[self.transport.target + "/pulls/7"]
                self.transport.values[self.transport.target + "/issues"][:] = [
                    dict(value, pull_request={})]
                if invalid == "unadopted":
                    self.transport.values[self.transport.target + "/issues"][0]["labels"] = []
                    self.assertIsNone(self.prepare())
                else:
                    if invalid == "foreign-head":
                        value["head"]["repo"] = {"id": 42, "full_name": "foreign/aspire"}
                    elif invalid == "foreign-base":
                        value["base"]["repo"] = {"id": 42, "full_name": "foreign/aspire"}
                    elif invalid == "base-ref":
                        value["base"]["ref"] = "release"
                    else:
                        value["node_id"] = "REPLACED"
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertEqual([], [write for write in self.transport.writes if write[1] == self.transport.tasks])

    def test_closed_transport_drives_both_pr_branches_and_denies_extra_writers(self):
        validator = github.PilotTransport("fixture", write=True, binding=bindings.select("upstream"),
                                          tracker=99, authority=500)
        service = self.transport

        def guarded(method, endpoint, body):
            validator.validate_endpoint(method, endpoint, body)
            return service(method, endpoint, body)

        self.transport = guarded
        try:
            first = self.prepare()
            self.assertEqual("waiting", self.settle(first)["outcome"])
        finally:
            self.transport = service
        for method, endpoint, body in (
                ("GET", self.transport.tasks, None),
                ("GET", "repos/foreign/aspire/pulls/7", None),
                ("GET", self.transport.target + "/issues", None),
                ("POST", self.transport.target + "/issues/7/comments", {"body": "[automated] arbitrary"}),
                ("PATCH", self.transport.target + "/issues/comments/27", {"body": "[automated] arbitrary"}),
                ("POST", self.transport.target + "/issues/9/labels", {"labels": ["shepherd-adopted"]}),
                ("POST", self.transport.target + "/git/blobs", {"content": "arbitrary"}),
                ("POST", self.transport.tasks, {"prompt": "text", "base_ref": "main", "create_pull_request": True})):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validator.validate_endpoint(method, endpoint, body)

    def test_two_pr_reviewer_and_reminder_routes_use_current_source_numbers(self):
        first = self.prepare()
        self.assertEqual("human", self.settle(first, action="human")["outcome"])
        second = self.prepare()
        self.assertEqual("human", self.settle(second, action="human")["outcome"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pilot.prepare(self.fresh(), RUN, self.clock(), present=True)
            self.clock.advance(seconds=60)
            pilot.prepare(self.fresh(), RUN, self.clock(), present=True)
        posts = [endpoint for method, endpoint, _ in self.transport.writes
                 if method == "POST" and endpoint.endswith("/comments")]
        self.assertEqual([self.transport.target + "/issues/7/comments",
                          self.transport.target + "/issues/8/comments"], posts)
        self.setUp()
        for number in (7, 8):
            self.transport.values[self.transport.target + "/issues/" + str(number) + "/comments"] = []
        self.transport.values[self.transport.target + "/commits/" + "a" * 40 + "/check-runs"] = {
            "total_count": 1, "check_runs": [{
                "id": 45, "head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                "name": "Tests", "html_url": ""}]}
        self.assertIsNone(self.prepare())
        self.assertIsNone(self.prepare())
        posts = [endpoint for method, endpoint, _ in self.transport.writes
                 if method == "POST" and endpoint.endswith("/requested_reviewers")]
        self.assertEqual([self.transport.target + "/pulls/7/requested_reviewers",
                          self.transport.target + "/pulls/8/requested_reviewers"], posts)

    def test_generic_resume_selects_exact_latest_operation_without_paid_work(self):
        first = self.prepare()
        self.assertEqual("human", self.settle(first, action="human")["outcome"])
        second = self.prepare()
        self.assertEqual("human", self.settle(second, action="human")["outcome"])
        before = state.parse(self.transport.comments[0]["body"])
        result = local.resume(self.fresh(), second["operation"], "a" * 40, self.clock())
        self.assertEqual("resumed; no inference", result["outcome"])
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(["human", "open"], [chain["state"] for chain in after["chains"]])
        self.assertEqual([chain["operations"] for chain in before["chains"]],
                         [chain["operations"] for chain in after["chains"]])
        legacy = github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True, binding=bindings.UPSTREAM)
        with self.assertRaisesRegex(ValueError, "trial subject"):
            legacy.read_authority()

    def test_hosted_and_local_cli_select_same_existing_upstream_authority(self):
        environment = {"CI_SHEPHERD_ENABLE": "true", "SHEPHERD_TARGET": "upstream",
                       "CI_SHEPHERD_UPSTREAM_TRACKER": "99", "CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT": "500",
                       "CI_SHEPHERD_UPSTREAM_TRACKER_NODE": "TRACKER99", "SHEPHERD_MODE": "pilot"}
        environment["GITHUB_OUTPUT"] = str(self.work / "output")
        directory = self.work / "hosted"
        with patch.dict(pilot.os.environ, environment, clear=True), \
                patch.object(github.PilotGitHub, "result_collector", staticmethod(lambda *_: "")), \
                patch.object(contracts, "host_run", return_value=RUN), \
                patch.object(hosted, "require_host"), patch.object(live, "clock", self.clock), \
                patch.object(github, "PilotTransport", return_value=self.transport), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(0, hosted.main(["prepare", "--workdir", str(directory)]))
        self.assertEqual("upstream", contracts.read_json(directory / "trusted/packet.json")["target"])
        self.assertEqual(bindings.FORK, bindings.select("upstream", "schedule"))
        fixture = local_fixtures.LocalTests()
        with patch.object(local, "command", fixture.command), \
                patch.object(local, "LocalGitHub", return_value=self.fresh()) as selected, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(0, local.main(["observe", "--target", "upstream", "--tracker", "99",
                                          "--authority", "500", "--tracker-node", "TRACKER99",
                                          "--workdir", str(self.work / "local")]))
        self.assertEqual(bindings.select("upstream"), selected.call_args.kwargs["binding"])
