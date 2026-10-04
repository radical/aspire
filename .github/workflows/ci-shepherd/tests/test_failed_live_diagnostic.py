from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest

from helpers import WorkspaceTest, reconciliation_decision
from test_live import FakeService
import live
import receipts
import round as contracts


class FailedLiveDiagnosticTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        service = FakeService()
        service.actor["id"] = 1472
        github = live.FixtureGitHub(service.transport, service.run, write=False)
        scope = receipts.TrialScope(live.ROOT, None)
        now = datetime(2026, 10, 4, 4, 13, tzinfo=timezone.utc)
        packet = contracts.prepare_reconciliation(github, live.ROOT, live.ROOT, service.run, lambda: now, scope)
        decision = reconciliation_decision(packet, "repair-pr", {"feedbackIds": ["ci-10-20"]})
        initial = receipts.new_record(packet["observation"], now, scope)
        prepared = deepcopy(initial)
        prepared["operations"].append({"id": "operation", "identity": receipts.operation_identity(packet, decision),
                                       "action": "repair-pr", "state": "prepared", "result": None,
                                       "run": service.run, "packetId": packet["packetId"]})
        reserved = deepcopy(prepared)
        receipts.reserve(reserved, reserved["operations"][0])
        self.comment = {"id": 501, "user": service.actor, "body": receipts.render_record(prepared)}
        self.audit = {"schemaVersion": 1, "run": service.run, "root": live.ROOT, "mode": "live", "phase": "failed",
                      "attempts": [{"kind": "status", "record": initial, "commentId": None},
                                   {"kind": "status", "record": prepared, "commentId": 501},
                                   {"kind": "status", "record": reserved, "commentId": 501}],
                      "record": reserved, "commentId": 501}
        self.expected = {**receipts.trial_tuple(prepared), "operationId": "operation",
                         "packetId": packet["packetId"], **service.run}

    def run_diagnostic(self):
        audit, comment = self.work / "audit.json", self.work / "comment.json"
        audit.write_text(json.dumps(self.audit))
        comment.write_text(json.dumps(self.comment))
        command = [sys.executable, str(Path(live.__file__).with_name("diagnostics.py")), str(audit), str(comment)]
        for option, key in (("trial-id", "trialId"), ("trial-started-at", "trialStartedAt"), ("expires-at", "expiresAt"),
                            ("operation-id", "operationId"), ("packet-id", "packetId"),
                            ("run-id", "runId"), ("workflow-sha", "workflowSha")):
            command.extend(["--" + option, self.expected[key]])
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def test_prepared_canonical_record_is_not_replaced_by_attempted_reservation(self):
        result = self.run_diagnostic()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["canonicalRepairBatches"], 0)
        self.assertEqual(value["attemptedRepairBatches"], 1)
        self.assertEqual(value["operationState"], "prepared")
        self.assertEqual(value["taskSendAudits"], 0)
        self.assertEqual(value["trial"], {key: self.expected[key] for key in receipts.TRIAL_FIELDS})
        self.assertFalse(value["mayResume"])
        self.assertFalse(value["receiptPresent"])
        self.assertTrue(value["offlineOnly"])

    def test_task_attempt_trial_change_or_wrong_actor_is_refused(self):
        saved = deepcopy((self.audit, self.comment, self.expected))
        for change in ("task", "trial", "actor", "receipt"):
            with self.subTest(change=change):
                self.audit, self.comment, self.expected = deepcopy(saved)
                if change == "task":
                    self.audit["attempts"].append({"kind": "task", "operationId": "operation"})
                elif change == "trial":
                    self.expected["trialId"] = "00000000-0000-4000-8000-000000000000"
                elif change == "actor":
                    self.comment["user"]["id"] = 500
                else:
                    self.expected["trialId"] = self.audit["record"]["trialId"]
                    (self.work / "receipt.json").write_text("{}")
                result = self.run_diagnostic()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("diagnostic refused", result.stderr)
