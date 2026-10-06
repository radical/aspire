from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import unittest

import pilot_state as state
import test_pilot


class RunReportTests(unittest.TestCase):
    def test_failed_persistence_does_not_claim_returned_task_is_saved(self):
        before = self.ledger()
        after = deepcopy(before)
        after["chains"][0]["operations"][0].update(
            taskId="returned-task", workerState="queued", state="waiting")
        report = self.render(before, after, {"outcome": "failed", "error": "authority write uncertain"})
        self.assertIn("New observed repair task ID", report)
        self.assertIn("persistence is unconfirmed", report)
        self.assertNotIn("New saved repair task", report)

    def render(self, before, after, result, packet=None):
        self.assertIsNotNone(importlib.util.find_spec("run_report"), "Human run report renderer is missing")
        import run_report
        return run_report.render(test_pilot.RUN, "upstream", before, after, result,
                                 packet, "2026-10-06T20:00:00Z", "2026-10-06T20:01:00Z")

    def ledger(self):
        ledger = state.new_ledger("microsoft/aspire")
        chain = state.adopt(ledger, 20722, "pr", "PR_NODE")
        state.reserve(ledger, chain, "fixture", datetime(2026, 10, 6, tzinfo=timezone.utc), local=False)
        return ledger

    def test_existing_worker_wait_is_not_reported_as_a_new_dispatch_or_fix(self):
        before = self.ledger()
        operation = before["chains"][0]["operations"][0]
        operation.update(taskId="saved-task", workerState="in_progress", state="waiting",
                         nativeActual=2, nativeReserved=0, workerReserved=998)
        report = self.render(before, deepcopy(before), {"outcome": "observed; no inference"})
        self.assertIn("No new saved repair task", report)
        self.assertIn("saved-task", report)
        self.assertIn(r"in\_progress", report)
        self.assertIn("New action rounds: 0", report)
        self.assertIn("Newly recorded credits: 0", report)
        self.assertIn("not proof of a fix or green CI", report)

    def test_dispatch_reason_and_unknown_costs_are_reported_without_claiming_spending(self):
        before = state.new_ledger("microsoft/aspire")
        after = self.ledger()
        operation = after["chains"][0]["operations"][0]
        operation.update(taskId="new-task", workerState="queued", state="waiting",
                         nativeActual=2, nativeReserved=0, workerReserved=998)
        packet = {"observation": {"kind": "pr", "number": 20722, "head": "a" * 40,
                                 "feedback": [{"id": "review-comment:31",
                                               "body": "Please fix failing CLI tests. " + "details " * 100}]}}
        report = self.render(before, after, {"outcome": "waiting", "taskId": "new-task"}, packet)
        self.assertIn("New saved repair task", report)
        self.assertIn("https://github.com/microsoft/aspire/tasks/new-task", report)
        self.assertIn("review-comment:31", report)
        self.assertIn("Outstanding reservations: 998", report)
        self.assertIn("unknown", report)
        self.assertIn("Please fix failing CLI tests.", report)
        self.assertNotIn("details " * 100, report)

    def test_new_review_and_confirmed_reminder_are_visible(self):
        before = self.ledger()
        after = deepcopy(before)
        chain = after["chains"][0]
        chain["reviews"] = [{"id": "review-request", "state": "waiting", "head": "b" * 40,
                             "actual": None, "reserved": 30}]
        chain["reminder"] = {"id": "reminder", "sendState": "confirmed", "commentId": 42}
        report = self.render(before, after, {"outcome": "observed; no inference"})
        self.assertIn("New Copilot review intent: waiting", report)
        self.assertIn("Reminder: confirmed", report)
        self.assertIn("#issuecomment-42", report)

    def test_partial_worker_usage_still_reports_unknown_outstanding_cost(self):
        ledger = self.ledger()
        ledger["chains"][0]["operations"][0].update(
            nativeActual=2, nativeReserved=0, workerActual=0.5, workerReserved=997.5,
            taskId="running-task", workerState="in_progress", state="waiting")
        report = self.render(ledger, deepcopy(ledger), {"outcome": "observed; no inference"})
        self.assertIn("billing: unknown amounts remain", report)
        self.assertIn("Outstanding reservations: 997.5", report)

    def test_task_completion_shows_before_after_state_and_link(self):
        before = self.ledger()
        previous = before["chains"][0]["operations"][0]
        previous.update(taskId="saved-task", workerState="in_progress", state="waiting")
        after = deepcopy(before)
        after["chains"][0]["operations"][0].update(workerState="completed", state="completed",
                                                  nativeActual=2, nativeReserved=0, workerActual=3)
        report = self.render(before, after, {"outcome": "observed; no inference"})
        self.assertIn("Task state: in\\_progress -> completed", report)
        self.assertIn("Operation state: waiting -> completed", report)
        self.assertIn("Found completed task", report)
        self.assertIn("https://github.com/microsoft/aspire/tasks/saved-task", report)

    def test_new_task_reports_scheduled_vs_observed_started(self):
        after = self.ledger()
        after["chains"][0]["operations"][0].update(
            taskId="new-task", workerState="queued", state="waiting")
        report = self.render(state.new_ledger("microsoft/aspire"), after, {"outcome": "waiting"})
        self.assertIn("Task scheduled", report)
        self.assertNotIn("Task started", report)
        before = deepcopy(after)
        after["chains"][0]["operations"][0]["workerState"] = "in_progress"
        report = self.render(before, after, {"outcome": "observed; no inference"})
        self.assertIn("Task started (observed)", report)
        self.assertIn("Task state: queued -> in\\_progress", report)

    def test_initiation_explains_remaining_feedback_and_observed_admission_state(self):
        after = self.ledger()
        after["chains"][0]["operations"][0].update(taskId="new-task", state="waiting", workerState="queued")
        packet = {"observation": {"kind": "pr", "number": 20722, "head": "a" * 40,
                                 "managed": True, "pendingCI": False, "ready": False,
                                 "feedback": [{"id": "check:31", "body": "CLI tests: failure"}]}}
        report = self.render(state.new_ledger("microsoft/aspire"), after, {"outcome": "waiting"}, packet)
        self.assertIn("Why a decision was admitted: 1 remaining feedback items", report)
        self.assertIn("managed: True; CI pending: False; ready: False", report)
        self.assertIn("Why a task was initiated: native decision authorized cloud repair", report)

    def test_failure_and_uncertain_send_preserve_honest_report(self):
        before = self.ledger()
        after = deepcopy(before)
        after["chains"][0]["operations"][0].update(state="uncertain", workerReserved=970)
        report = self.render(before, after, {"outcome": "failed", "error": "authority write uncertain\n## forged"})
        self.assertIn("Outcome: **failed**", report)
        self.assertIn("uncertain", report)
        self.assertIn("No new saved repair task", report)
        self.assertIn("does not prove no send occurred", report)
        self.assertNotIn("\n## forged", report)
