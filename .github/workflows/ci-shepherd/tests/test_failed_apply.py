from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
import uuid
from urllib.parse import urlparse
from unittest.mock import patch

from helpers import WorkspaceTest, reconciliation_decision, reconciliation_evidence
from github import Response
from test_preapply_abort import AbortService
from test_pinned_recovery import COMMENT, TRIAL, OPERATION, FEEDBACK
import hosted
import live
import receipts
import recovery
import round as contracts


SOURCE = "d1cb65eb4fd8b3efe456b6573c7eb04010439e36"
FAILED_RUN = "37183170001"
TASK = "existing-correlated-worker"


class FailedApplyService(AbortService):
    def __init__(self, source=SOURCE):
        super().__init__()
        self.current_number = 7
        self.failed_run = {**deepcopy(self.abort_run), "id": int(FAILED_RUN), "run_number": 6, "head_sha": source}
        self.past.append(self.failed_run)
        run = {"repository": live.REPOSITORY, "runId": FAILED_RUN, "runAttempt": "1", "workflowSha": source}
        prepared = deepcopy(self.abort_files("prepare"))
        packet, envelope = prepared["packet.json"], prepared["envelope.json"]
        packet.update(run=run, packetId=str(uuid.uuid4()), preparedAt="2026-10-04T06:30:00Z",
                      validUntil="2026-10-04T06:40:00Z")
        packet["basis"].update(run=run, packetId=packet["packetId"])
        envelope["recovery"] = recovery.PinnedRecovery(run).descriptor()
        self.consumed = deepcopy(self.prepared)
        receipts.reserve(self.consumed, self.consumed["operations"][0])
        reserved = deepcopy(self.consumed)
        self.consumed["operations"][0]["state"] = "consumed"
        decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
        audit = {"schemaVersion": 1, "run": run, "root": live.ROOT, "mode": "live", "phase": "failed",
                 "attempts": [{"kind": "status", "record": reserved, "commentId": COMMENT},
                              {"kind": "status", "record": self.consumed, "commentId": COMMENT},
                              {"kind": "task", "operationId": OPERATION, "sourceHead": live.INITIAL_HEAD}],
                 "record": self.consumed, "commentId": COMMENT}
        prepare_audit = {"schemaVersion": 1, "run": run, "root": live.ROOT, "mode": "live",
                         "phase": "complete", "attempts": [], "record": self.prepared, "commentId": COMMENT}
        self.artifacts[FAILED_RUN] = []
        for kind, files in (
            ("prepare", prepared), ("prepare-audit", {"audit.json": prepare_audit}),
            ("evidence", {"evidence.json": reconciliation_evidence(decision)}),
            ("receipt", {"audit.json": audit, "observation.json": {"authority": False, "effects": []},
                         "failure.json": {"schemaVersion": 1, "stage": "apply", "error": "task PR artifact mismatch"}}),
        ):
            artifact_id = 400 + len(self.documents)
            self.documents[artifact_id] = deepcopy(files)
            self.artifacts[FAILED_RUN].append({
                "id": artifact_id, "name": f"ci-shepherd-{kind}-{FAILED_RUN}-1", "expired": False,
                "workflow_run": {"id": int(FAILED_RUN), "repository_id": live.REPOSITORY_ID,
                                 "head_repository_id": live.REPOSITORY_ID, "head_sha": source, "head_branch": "main"},
            })
        self.failed_jobs = [
            {"id": 40000 + index, "name": name, "run_id": int(FAILED_RUN), "run_attempt": 1, "head_sha": source,
             "status": "completed", "conclusion": "failure" if name == "submit_decision" else "success",
             "steps": [{"name": "Guarded host apply" if name == "submit_decision" else "job step",
                        "status": "completed", "conclusion": "failure" if name == "submit_decision" else "success"}]}
            for index, name in enumerate(("prepare", "activation", "agent", "submit_decision", "conclusion"))
        ]
        self.comments[0]["body"] = receipts.render_record(self.consumed)
        correlation = {"root": live.ROOT, "trial": TRIAL, "operationId": OPERATION, "sourceHead": live.INITIAL_HEAD}
        task = self.task_value(TASK, live.CORRELATION + receipts.canonical(correlation))
        task["state"] = task["sessions"][0]["state"] = "in_progress"
        task["artifacts"].append({"provider": "github", "type": "pull", "data": {"id": live.PR_ID, "global_id": ""}})
        self.tasks[TASK] = task

    def failed_files(self, kind):
        item = next(value for value in self.artifacts[FAILED_RUN] if value["name"] == f"ci-shepherd-{kind}-{FAILED_RUN}-1")
        return self.documents[item["id"]]

    def transport(self, method, endpoint, body):
        path = urlparse(endpoint).path
        prefix = f"repos/{live.REPOSITORY}/actions/runs/{FAILED_RUN}"
        if path == prefix + "/artifacts":
            value = {"artifacts": deepcopy(self.artifacts[FAILED_RUN])}
        elif path == prefix + "/attempts/1/jobs":
            value = {"jobs": deepcopy(self.failed_jobs)}
        else:
            return super().transport(method, endpoint, body)
        self.calls.append((method, endpoint, deepcopy(body)))
        if self.before_request:
            self.before_request(self, method, endpoint)
        return Response(value, {})


class FailedApplyTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = FailedApplyService()
        self.now = datetime(2026, 10, 4, 6, 50, tzinfo=timezone.utc)
        self.policy = recovery.PinnedRecovery(self.service.run)

    def prepare(self, authorized=True):
        with patch.object(live, "clock", return_value=self.now):
            return hosted.prepare(self.work / "prepared", "live", self.service.run,
                                  transport=self.service.transport, host_check=lambda run: None,
                                  recovery=self.policy if authorized else None)

    def apply_wait(self, packet):
        return self.apply_decision(packet, reconciliation_decision(packet, "wait"))

    def apply_decision(self, packet, decision):
        contracts.write_json(self.work / "evidence.json", reconciliation_evidence(decision))
        contracts.write_json(self.work / "decision.json", {
            "items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        with patch.object(live, "clock", return_value=self.now):
            return hosted.apply(self.work / "prepared/trusted", self.work / "evidence.json", self.work / "decision.json",
                                self.work / "receipt.json", self.service.run, transport=self.service.transport,
                                host_check=lambda run: None, recovery=self.policy)

    def test_source_qualified_failed_send_retains_floor_and_fresh_wait_recovers_same_task(self):
        failure, prepared = None, None
        try:
            prepared = self.prepare()
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Executed writer failure blocks existing receipt recovery: " + str(failure))
        packet, envelope, _ = prepared
        self.assertEqual(packet["record"], {"commentId": COMMENT, "value": self.service.consumed})
        self.assertEqual(envelope["scope"]["trial"], TRIAL)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        result = self.apply_wait(packet)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertTrue(result["recovered"])
        self.assertEqual(result["effects"], [])
        self.assertEqual(result["operation"]["result"], {"id": TASK, "kind": "worker"})
        canonical = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(canonical["repairBatches"], 1)
        self.assertEqual(receipts.trial_tuple(canonical), TRIAL)
        self.assertEqual(len(canonical["operations"]), 1)
        for key in ("id", "identity", "run", "packetId"):
            self.assertEqual(canonical["operations"][0][key], self.service.prepared["operations"][0][key])
        self.assertEqual(self.service.posts(), [])
        self.assertFalse(result["gate"]["ready"])

    def test_default_selector_still_blocks(self):
        with self.assertRaises(ValueError):
            self.prepare(authorized=False)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_confirmed_current_task_retains_history_floor_without_permanent_wait_rejection(self):
        confirmed = deepcopy(self.service.consumed)
        confirmed["operations"][0].update(state="confirmed", result={"id": TASK, "kind": "worker"})
        self.service.comments[0]["body"] = receipts.render_record(confirmed)
        task = self.service.tasks[TASK]
        task["state"] = task["sessions"][0]["state"] = "completed"
        self.service.pr["head"]["sha"] = self.service.ci_head = "e" * 40
        packet, _, _ = self.prepare()
        self.assertEqual(packet["record"]["value"], confirmed)
        self.assertEqual(self.service.failed_files("receipt")["audit.json"]["record"], self.service.consumed)
        decision = reconciliation_decision(packet, "repair-pr", {"feedbackIds": [FEEDBACK]})
        github = live.FixtureGitHub(self.service.transport, self.service.run, recovery=self.policy)
        result = contracts.apply_reconciliation(
            packet, decision, self.service.run, github, lambda: self.now,
            receipts.TrialScope(live.ROOT, TRIAL), dry_run=True)
        self.assertEqual(result, {"outcome": "dry-run", "effects": [],
                                  "operation": receipts.operation_identity(packet, decision)})
        failure = None
        try:
            self.apply_decision(packet, decision)
        except ValueError as error:
            failure = str(error)
        self.assertEqual(failure, "first gate permits only the original normalization defect; later repairs aren't installed",
                         "Confirmed current authority must reach the unchanged executor cap, not historical WAIT rejection")
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), confirmed)
        self.assertEqual(confirmed["repairBatches"], 1)
        self.assertEqual(receipts.trial_tuple(confirmed), TRIAL)
        self.assertFalse((self.work / "receipt.json").exists())

    def test_completed_worker_approval_blocked_pr_ci_allows_fresh_wait_receipt_recovery(self):
        task = self.service.tasks[TASK]
        task["state"] = task["sessions"][0]["state"] = "completed"
        self.service.pr["head"]["sha"] = self.service.ci_head = "c" * 40
        self.service.ci_conclusion = "action_required"
        self.service.ci_jobs = {"total_count": 0, "jobs": []}
        self.service.ci_extra_runs = [{"id": 37170819380, "run_attempt": 1, "head_sha": self.service.ci_head,
                                      "path": live.FIXTURE_WORKFLOW, "event": "workflow_dispatch",
                                      "pull_requests": [], "status": "completed", "conclusion": "success"}]
        prepared, failure = None, None
        try:
            prepared = self.prepare()
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Approval-blocked PR CI prevented existing receipt recovery: " + str(failure))
        packet, envelope, _ = prepared
        self.assertEqual(packet["record"]["value"], self.service.consumed)
        self.assertEqual(envelope["context"]["feedback"], [])
        self.assertEqual(packet["observation"]["jobs"], [])
        result = self.apply_wait(packet)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["operation"]["result"], {"id": TASK, "kind": "worker"})
        self.assertEqual(result["currentHead"], self.service.ci_head)
        self.assertEqual(result["gate"]["state"], "approval-blocked")
        self.assertEqual(result["gate"]["runId"], 37170819379)
        self.assertFalse(result["gate"]["ciPassed"])
        self.assertFalse(result["gate"]["ready"])
        canonical = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(canonical["repairBatches"], 1)
        self.assertEqual(receipts.trial_tuple(canonical), TRIAL)
        self.assertEqual(len(canonical["operations"]), 1)
        for key in ("id", "identity", "run", "packetId"):
            self.assertEqual(canonical["operations"][0][key], self.service.prepared["operations"][0][key])
        self.assertEqual(self.service.posts(), [])

    def test_absent_task_retains_consumed_uncertainty_without_post(self):
        self.service.tasks.pop(TASK)
        packet, _, _ = self.prepare()
        result = self.apply_wait(packet)
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.consumed)
        self.assertEqual(self.service.posts(), [])

    def test_ambiguous_remote_outcomes_cannot_confirm_or_redispatch(self):
        duplicate = deepcopy(self.service.tasks[TASK])
        duplicate["id"] = "second-task"
        duplicate["sessions"][0]["task_id"] = duplicate["id"]
        self.service.tasks[duplicate["id"]] = duplicate
        packet, _, _ = self.prepare()
        result = self.apply_wait(packet)
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.consumed)
        self.assertEqual(self.service.posts(), [])

    def assert_final_guard_rechecks_task(self, change, action="wait"):
        task = self.service.tasks[TASK]
        task["state"] = task["sessions"][0]["state"] = "completed"
        packet, _, _ = self.prepare()
        changed = False

        def before_request(service, method, endpoint):
            nonlocal changed
            audit_path = self.work / "audit.json"
            if changed or method != "GET" or urlparse(endpoint).path != f"repos/{live.REPOSITORY}/pulls/121" or not audit_path.exists():
                return
            attempts = contracts.read_json(audit_path)["attempts"]
            if not any(attempt["kind"] == "status" and attempt["record"]["operations"][0]["state"] == "confirmed"
                       for attempt in attempts):
                return
            changed = True
            if change in {"disappear", "replace"}:
                service.tasks.pop(TASK)
            if change in {"duplicate", "replace"}:
                second = deepcopy(task)
                second["id"] = "second-task"
                second["sessions"][0].update(id="second-session", task_id=second["id"])
                service.tasks[second["id"]] = second

        self.service.before_request = before_request
        failure = None
        try:
            decision = reconciliation_decision(packet, action, {"feedbackIds": [FEEDBACK]} if action == "repair-pr" else None)
            self.apply_decision(packet, decision)
        except ValueError as error:
            failure = str(error)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [],
                         "Recovery published confirmation after the selected task association changed")
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.consumed)
        if action == "wait":
            self.assertTrue(changed, "Task mutation must occur after candidate reconciliation, inside the final guard")
            self.assertEqual(failure, "recovery task association changed")
        else:
            self.assertFalse(changed, "Non-WAIT must be rejected before a candidate publication is attempted")
            self.assertEqual(failure, "consumed/uncertain recovery requires a fresh WAIT decision")
            self.assertEqual(contracts.safe_output(contracts.read_json(self.work / "decision.json"))["action"], action)
            self.assertFalse((self.work / "receipt.json").exists())

    def test_task_disappearing_in_final_guard_cannot_publish_confirmation(self):
        self.assert_final_guard_rechecks_task("disappear")

    def test_duplicate_completed_match_in_final_guard_cannot_publish_confirmation(self):
        self.assert_final_guard_rechecks_task("duplicate")

    def test_replacement_task_in_final_guard_cannot_publish_original_confirmation(self):
        self.assert_final_guard_rechecks_task("replace")

    def test_non_wait_disappearing_task_route_rejects_before_any_mutation(self):
        self.assert_final_guard_rechecks_task("disappear", "repair-pr")

    def test_non_wait_duplicate_task_route_rejects_before_any_mutation(self):
        self.assert_final_guard_rechecks_task("duplicate", "repair-pr")

    def test_non_wait_replaced_task_route_rejects_before_any_mutation(self):
        self.assert_final_guard_rechecks_task("replace", "repair-pr")

    def test_non_wait_uncertain_task_route_rejects_before_any_mutation(self):
        self.service.consumed["operations"][0]["state"] = "uncertain"
        self.service.comments[0]["body"] = receipts.render_record(self.service.consumed)
        self.assert_final_guard_rechecks_task("disappear", "repair-pr")

    def test_recovery_clock_head_feedback_and_hands_off_guards_still_block(self):
        for change in ("expiry", "rollback", "head", "feedback", "hands-off", "takeover"):
            with self.subTest(change=change):
                self.setUp()
                packet, _, _ = self.prepare()
                if change == "expiry":
                    self.now = live.issue_pr.timestamp(packet["validUntil"])
                elif change == "rollback":
                    self.now = live.issue_pr.timestamp(packet["preparedAt"]).replace(minute=49)
                elif change == "head":
                    self.service.pr["head"]["sha"] = "e" * 40
                elif change == "feedback":
                    self.service.ci_conclusion = "success"
                elif change == "hands-off":
                    self.service.pr["labels"].append({"name": "hands-off"})
                else:
                    self.service.comments[0]["user"] = {"id": 999, "login": "radical"}
                with self.assertRaises(ValueError):
                    self.apply_wait(packet)
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
                self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.consumed)

    def test_malformed_authority_numbers_and_audit_shapes_do_not_qualify(self):
        for change in ("actor-id", "job-run-id", "artifact-id", "scope-number", "audit-comment", "prepare-counter", "attempts"):
            with self.subTest(change=change):
                self.setUp()
                if change == "actor-id":
                    self.service.failed_run["actor"]["id"] = 1472.0
                elif change == "job-run-id":
                    self.service.failed_jobs[0]["run_id"] = float(FAILED_RUN)
                elif change == "artifact-id":
                    self.service.artifacts[FAILED_RUN][0]["id"] = str(self.service.artifacts[FAILED_RUN][0]["id"])
                elif change == "scope-number":
                    scope = self.service.failed_files("prepare")["envelope.json"]["scope"]
                    scope["root"] = {**scope["root"], "number": 121.0}
                elif change == "audit-comment":
                    self.service.failed_files("receipt")["audit.json"]["commentId"] = float(COMMENT)
                elif change == "prepare-counter":
                    self.service.failed_files("prepare-audit")["audit.json"]["record"]["repairBatches"] = False
                else:
                    self.service.failed_files("receipt")["audit.json"]["attempts"] = None
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_each_trusted_history_boundary_blocks_without_effects(self):
        changes = ("source", "actor", "repository", "workflow", "event", "rerun", "extra-job", "native-job",
                   "apply-skipped", "apply-cancelled", "missing-apply-step", "artifact-missing", "artifact-expired",
                   "artifact-source", "artifact-duplicate", "extra-member", "packet", "scope", "descriptor",
                   "prepare-audit", "native-exit", "native-wait", "extra-task-attempt", "wrong-task-attempt",
                   "wrong-status-record", "wrong-audit-record", "successful-receipt", "wrong-failure",
                   "canonical-reset", "canonical-reserved", "canonical-failed", "canonical-takeover",
                   "canonical-fake-confirmation", "task-forged")
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                prepared = self.service.failed_files("prepare")
                packet, envelope = prepared["packet.json"], prepared["envelope.json"]
                native = self.service.failed_files("evidence")["evidence.json"]
                files = self.service.failed_files("receipt")
                audit = files["audit.json"]
                if change == "source":
                    self.service = FailedApplyService("e" * 40)
                elif change == "actor":
                    self.service.failed_run["actor"] = {"id": 999, "login": "radical"}
                elif change == "repository":
                    self.service.failed_run["repository"]["id"] += 1
                elif change == "workflow":
                    self.service.failed_run["workflow_id"] += 1
                elif change == "event":
                    self.service.failed_run["event"] = "pull_request"
                elif change == "rerun":
                    self.service.failed_run["run_attempt"] = 2
                elif change == "extra-job":
                    self.service.failed_jobs.append({**deepcopy(self.service.failed_jobs[0]), "id": 999, "name": "extra-writer"})
                elif change == "native-job":
                    self.service.failed_jobs[2]["conclusion"] = "failure"
                elif change in {"apply-skipped", "apply-cancelled"}:
                    self.service.failed_jobs[3]["conclusion"] = "skipped" if change == "apply-skipped" else "cancelled"
                elif change == "missing-apply-step":
                    self.service.failed_jobs[3]["steps"] = []
                elif change.startswith("artifact-"):
                    artifacts = self.service.artifacts[FAILED_RUN]
                    if change == "artifact-missing":
                        artifacts.pop(0)
                    elif change == "artifact-expired":
                        artifacts[0]["expired"] = True
                    elif change == "artifact-source":
                        artifacts[0]["workflow_run"]["head_sha"] = "e" * 40
                    else:
                        artifacts.append({**deepcopy(artifacts[0]), "id": 999})
                elif change == "extra-member":
                    prepared["claim.json"] = {}
                elif change == "packet":
                    packet["run"]["runId"] = "42"
                elif change == "scope":
                    envelope["scope"]["trial"] = None
                elif change == "descriptor":
                    envelope["recovery"]["kind"] = "agent-installed"
                elif change == "prepare-audit":
                    self.service.failed_files("prepare-audit")["audit.json"]["attempts"] = [{"kind": "status"}]
                elif change == "native-exit":
                    next(item for item in native["events"] if item["type"] == "result")["exitCode"] = 1
                elif change == "native-wait":
                    self.service.failed_files("evidence")["evidence.json"] = reconciliation_evidence(reconciliation_decision(packet, "wait"))
                elif change == "extra-task-attempt":
                    audit["attempts"].append(deepcopy(audit["attempts"][-1]))
                elif change == "wrong-task-attempt":
                    audit["attempts"][-1]["operationId"] = "another-operation"
                elif change == "wrong-status-record":
                    audit["attempts"][0]["record"]["repairBatches"] = 0
                elif change == "wrong-audit-record":
                    audit["record"] = self.service.prepared
                elif change == "successful-receipt":
                    files["receipt.json"] = {"outcome": "confirmed"}
                elif change == "wrong-failure":
                    files["failure.json"]["stage"] = "collect"
                elif change == "canonical-takeover":
                    self.service.comments[0]["user"] = {"id": 999, "login": "radical"}
                elif change == "task-forged":
                    self.service.tasks[TASK]["creator"] = {"id": 999, "login": "radical"}
                else:
                    canonical = deepcopy(self.service.consumed)
                    if change == "canonical-reset":
                        canonical = deepcopy(self.service.prepared)
                    elif change == "canonical-fake-confirmation":
                        canonical["operations"][0].update(state="confirmed", result={"id": "invented-task", "kind": "worker"})
                        self.service.tasks.pop(TASK)
                    else:
                        canonical["operations"][0]["state"] = "reserved" if change == "canonical-reserved" else "failed"
                    self.service.comments[0]["body"] = receipts.render_record(canonical)
                with self.assertRaises((ValueError, KeyError)):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
