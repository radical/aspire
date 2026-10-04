from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

from helpers import WorkspaceTest, compiled_environment, compiled_step, reconciliation_decision, reconciliation_evidence, wire_report
from test_live import FakeService, HTTPFixture, archive
from test_rate_limit import WindowOpener
from github import Response
import hosted
import live
import receipts
import round as contracts

try:
    import recovery
except ModuleNotFoundError:
    recovery = None


OLD_SHA = "a9da7a1d90195c255bfd7d91349ff0bdbbb20356"
OBSERVE = "37175895217"
FAILED = "37176266114"
COMMENT = 5976480777
OPERATION = "1d681347-69c8-4fe5-ba17-24aa6cd7f238"
PACKET = "90ed6de9-ece6-412e-bc7a-a2f46c5c23b3"
FEEDBACK = "ci-37170819379-111343238059"
TRIAL = {"trialId": "8ac4f956-3cd7-42b9-bd69-546b220b128f",
         "trialStartedAt": "2026-10-04T04:13:32.988354Z", "expiresAt": "2026-10-05T04:13:32.988354Z"}
JOB_IDS = {
    OBSERVE: [111358307472, 111358357310, 111358399718, 111358682556, 111358753292],
    FAILED: [111359393542, 111359451202, 111359534609, 111359697457, 111359816249],
}


def old_run(run_id):
    return {"repository": live.REPOSITORY, "runId": run_id, "runAttempt": "1", "workflowSha": OLD_SHA}


def historical_packet(run_id):
    feedback = [{"id": FEEDBACK, "revision": live.INITIAL_HEAD + ":1", "state": "open"}]
    snapshot = {"schemaVersion": 1, "root": live.ROOT,
                "complete": {key: True for key in live.issue_pr.INVENTORIES},
                "subjects": [{"subject": live.ROOT, "nodeId": live.PR_NODE, "state": "open", "managed": True,
                              "labels": ["shepherd-adopted"], "revision": live.INITIAL_HEAD, "feedback": feedback}],
                "workers": [], "managedPullRequests": [121], "comments": [],
                "history": {"recordIds": [], "publicationAttempts": [], "associatedOperationIds": []},
                "jobs": [{"subject": live.ROOT, "headSha": live.INITIAL_HEAD, "runId": 37170819379,
                          "jobId": 111343238059, "logicalJob": live.FIXTURE_WORKFLOW + " / " + live.JOB,
                          "state": "completed", "transient": False}]}
    packet_id = PACKET if run_id == FAILED else "7872141c-34e3-423a-98fb-a7833ed307da"
    basis = {"packetId": packet_id, "run": old_run(run_id), "policy": "drive-to-readiness", "root": live.ROOT,
             "nodeId": live.PR_NODE, "revision": live.INITIAL_HEAD, "feedback": feedback}
    return {"schemaVersion": 1, "kind": "reconciliation", "packetId": packet_id, "run": old_run(run_id),
            "preparedAt": "2026-10-04T04:11:35.722795Z", "validUntil": "2026-10-04T04:21:35.722795Z",
            "root": live.ROOT, "subject": live.ROOT, "policy": "drive-to-readiness", "basis": basis,
            "observation": snapshot, "record": {"commentId": None, "value": None},
            "evidence": [{"id": "subject-121", "subject": live.ROOT, "revision": live.INITIAL_HEAD}]}


class PinnedService(FakeService):
    """Both archive lanes and authenticated old/current workflow histories."""

    def __init__(self):
        super().__init__()
        self.actor["id"] = 1472
        self.run = {"repository": live.REPOSITORY, "runId": "37180000000", "runAttempt": "1", "workflowSha": "c" * 40}
        self.current_number = 5
        packet = historical_packet(FAILED)
        decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
        operation = {"id": OPERATION, "identity": receipts.operation_identity(packet, decision), "action": "repair-pr",
                     "state": "prepared", "result": None, "run": old_run(FAILED), "packetId": PACKET}
        self.prepared = {"schemaVersion": 1, "root": live.ROOT, "rootNodeId": live.PR_NODE, **TRIAL,
                         "repairBatches": 0, "reruns": [], "operations": [operation]}
        self.comments = [{"id": COMMENT, "user": self.actor, "body": receipts.render_record(self.prepared)}]
        self.documents, self.artifacts, self.jobs = {}, {}, {}
        for run_id in (OBSERVE, FAILED):
            packet = historical_packet(run_id)
            decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
            native = reconciliation_evidence(decision)
            native["sessionId"] = "846fcc95-05bd-4aab-9f14-a4f210ea0f62" if run_id == FAILED else "2c20f226-b2bd-4358-911d-92b8ca0ee6e4"
            for event in native["events"]:
                if event["type"] == "session.start":
                    event["data"]["sessionId"] = native["sessionId"]
                elif event["type"] == "result":
                    event["sessionId"] = native["sessionId"]
            envelope = {"schemaVersion": 1, "packet": packet, "sessionId": None,
                        "mode": "live" if run_id == FAILED else "observe",
                        "scope": {"root": live.ROOT, "trial": None}, "context": {}}
            initial, reserved = deepcopy(self.prepared), deepcopy(self.prepared)
            initial["operations"] = []
            receipts.reserve(reserved, reserved["operations"][0])
            audit = {"schemaVersion": 1, "run": old_run(run_id), "root": live.ROOT, "mode": envelope["mode"],
                     "phase": "failed" if run_id == FAILED else "complete",
                     "attempts": [{"kind": "status", "record": initial, "commentId": None},
                                  {"kind": "status", "record": self.prepared, "commentId": COMMENT},
                                  {"kind": "status", "record": reserved, "commentId": COMMENT}] if run_id == FAILED else [],
                     "record": reserved if run_id == FAILED else None, "commentId": COMMENT if run_id == FAILED else None}
            receipt_files = {"audit.json": audit}
            if run_id == FAILED:
                receipt_files["failure.json"] = {"schemaVersion": 1, "stage": "apply", "error": "GET unavailable: HTTP 403"}
                receipt_files["observation.json"] = {"authority": False, "effects": []}
            else:
                receipt_files["receipt.json"] = {"schemaVersion": 1, "run": old_run(run_id), "mode": "observe",
                                                "root": live.ROOT, "packetId": packet["packetId"],
                                                "sessionId": native["sessionId"], "outcome": "dry-run", "effects": [],
                                                "operation": receipts.operation_identity(packet, decision),
                                                "currentHead": live.INITIAL_HEAD, "tasks": [], "gate": {}, "push": None}
            self.artifacts[run_id] = []
            for kind, files in (("receipt", receipt_files), ("prepare", {"packet.json": packet, "envelope.json": envelope}),
                                ("evidence", {"evidence.json": native})):
                artifact_id = 100 + len(self.documents)
                self.documents[artifact_id] = deepcopy(files)
                self.artifacts[run_id].append({
                    "id": artifact_id, "name": f"ci-shepherd-{kind}-{run_id}-1", "expired": False,
                    "workflow_run": {"id": int(run_id), "repository_id": live.REPOSITORY_ID,
                                     "head_repository_id": live.REPOSITORY_ID, "head_sha": OLD_SHA, "head_branch": "main"},
                })
            self.jobs[run_id] = [
                {"id": job_id, "name": name, "run_id": int(run_id), "run_attempt": 1, "head_sha": OLD_SHA,
                 "status": "completed", "conclusion": "failure" if run_id == FAILED and name == "submit_decision" else "success"}
                for job_id, name in zip(JOB_IDS[run_id], ("prepare", "activation", "agent", "submit_decision", "conclusion"))
            ]
        self.past = [
            {**self.current_run(), "id": int(run_id), "run_number": number, "head_sha": OLD_SHA,
             "status": "completed", "conclusion": conclusion}
            for run_id, number, conclusion in ((OBSERVE, 3, "success"), (FAILED, 4, "failure"))
        ]
        for index in range(8):
            task = self.task_value(f"historic-{index}", "Earlier unrelated work")
            task["state"] = task["sessions"][0]["state"] = "cancelled"
            self.tasks[task["id"]] = task

    def current_run(self):
        return {**super().current_run(), "run_number": self.current_number, "workflow_id": live.WORKFLOW_ID,
                "head_repository": {"full_name": live.REPOSITORY, "id": live.REPOSITORY_ID}}

    def artifact_files(self, run_id, kind):
        item = next(value for value in self.artifacts[run_id] if value["name"] == f"ci-shepherd-{kind}-{run_id}-1")
        return self.documents[item["id"]]

    def transport(self, method, endpoint, body):
        path, query = urlparse(endpoint).path, parse_qs(urlparse(endpoint).query)
        prefix = f"repos/{live.REPOSITORY}/actions"
        value = None
        if path == f"repos/{live.REPOSITORY}/issues/comments/{COMMENT}" and method == "PATCH":
            self.comments[0].update(body)
            value = deepcopy(self.comments[0])
        elif path == prefix + f"/workflows/{live.WORKFLOW_ID}/runs":
            value = {"workflow_runs": [self.current_run()] + self.past + [entry["run"] for entry in self.history]}
        elif path == f"agents/repos/{live.REPOSITORY}/tasks" and method == "GET":
            archived = query["is_archived"] == ["true"]
            value = {"tasks": deepcopy([task for key, task in self.tasks.items()
                                       if (key.startswith("historic-") and int(key[-1]) >= 2) == archived])}
        elif path == prefix + "/workflows/200/runs":
            value = {"workflow_runs": [{"id": 37170819379, "run_attempt": 1, "head_sha": self.ci_head,
                                       "path": live.FIXTURE_WORKFLOW, "event": "pull_request", "pull_requests": [{"number": 121}],
                                       "status": self.ci_state, "conclusion": self.ci_conclusion}]}
        elif path == prefix + "/runs/37170819379/attempts/1/jobs":
            value = {"jobs": [{"id": 111343238059, "run_id": 37170819379, "head_sha": self.ci_head, "name": live.JOB,
                              "status": self.ci_state, "conclusion": self.ci_conclusion}]}
        elif path == prefix + "/jobs/111343238059/logs":
            value = self.logs
        else:
            for run_id in (OBSERVE, FAILED):
                if path == prefix + f"/runs/{run_id}/artifacts":
                    value = {"artifacts": deepcopy(self.artifacts[run_id])}
                elif path == prefix + f"/runs/{run_id}/attempts/1/jobs":
                    value = {"jobs": deepcopy(self.jobs[run_id])}
            if path.startswith(prefix + "/artifacts/"):
                artifact_id = int(path.split("/")[-2])
                if artifact_id in self.documents:
                    value = archive(self.documents[artifact_id])
        if value is not None:
            self.calls.append((method, endpoint, deepcopy(body)))
            if self.before_request:
                self.before_request(self, method, endpoint)
            return Response(value, {})
        return super().transport(method, endpoint, body)


class PinnedRecoveryTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = PinnedService()
        self.opener = WindowOpener()
        self.opener.service = self.service
        self.opener.now = datetime(2026, 10, 4, 4, 46, tzinfo=timezone.utc)
        self.opener.reset = self.opener.now + timedelta(seconds=59)
        self.transport = live.HTTPTransport("credential", write=True, opener=self.opener,
                                            clock_fn=self.opener.clock, sleep_fn=self.opener.sleep)
        self.policy = None if recovery is None else recovery.PinnedRecovery(self.service.run)
        self.saved = deepcopy(self.service.prepared)

    def prepare(self, *, authorized=True, mode="live"):
        kwargs = {"recovery": self.policy} if authorized and self.policy is not None else {}
        with patch.object(live, "clock", side_effect=self.opener.clock):
            return hosted.prepare(self.work / "prepared", mode, self.service.run, transport=self.transport,
                                  host_check=lambda run: None, **kwargs)

    def apply(self, packet, *, authorized=True):
        kwargs = {"recovery": self.policy} if authorized and self.policy is not None else {}
        decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
        contracts.write_json(self.work / "evidence.json", reconciliation_evidence(decision))
        contracts.write_json(self.work / "decision.json", {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}],
                                                         "errors": []})
        with patch.object(live, "clock", side_effect=self.opener.clock):
            return hosted.apply(self.work / "prepared/trusted", self.work / "evidence.json", self.work / "decision.json",
                                self.work / "receipt.json", self.service.run, transport=self.transport,
                                host_check=lambda run: None, **kwargs)

    def test_full_history_quota_resumes_same_prepared_intent_once(self):
        failure, result = None, None
        try:
            packet, _, _ = self.prepare()
            result = self.apply(packet)
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Pinned prepared recovery is blocked: " + str(failure))
        self.assertEqual(result["outcome"], "confirmed")
        record = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(receipts.trial_tuple(record), TRIAL)
        self.assertEqual(record["repairBatches"], 1)
        self.assertEqual(len(record["operations"]), 1)
        operation = record["operations"][0]
        for key in ("id", "identity", "run", "packetId"):
            self.assertEqual(operation[key], self.saved["operations"][0][key])
        self.assertNotEqual(packet["packetId"], PACKET)
        self.assertNotEqual(packet["run"], old_run(FAILED))
        self.assertEqual(len(self.service.posts()), 1)
        self.assertTrue(self.opener.sleeps)
        self.assertLessEqual(sum(self.opener.sleeps), 180)
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual([item["record"]["operations"][0]["state"] for item in audit["attempts"] if item["kind"] == "status"],
                         ["reserved", "consumed", "confirmed"])
        task_call = self.service.posts()[0]
        self.assertEqual(set(task_call[2]), {"prompt", "base_ref", "head_ref", "create_pull_request"})
        self.assertIn(OPERATION, task_call[2]["prompt"])
        for method, endpoint, at in self.opener.requests:
            if method == "POST":
                self.assertLess(at, live.issue_pr.timestamp(packet["validUntil"]), endpoint)

    def test_default_hosted_history_remains_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "immutable|failed|history"):
            self.prepare(authorized=False)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_apply_without_independent_host_authorization_cannot_resume(self):
        packet, _, _ = self.prepare()
        with self.assertRaises(ValueError):
            self.apply(packet, authorized=False)
        self.assertEqual(self.service.posts(), [])

    def test_agent_json_cannot_install_host_recovery_authorization(self):
        with self.assertRaises(ValueError):
            with patch.object(live, "clock", side_effect=self.opener.clock):
                hosted.prepare(self.work / "untrusted", "live", self.service.run, transport=self.transport,
                               host_check=lambda run: None, recovery={"kind": "pinned-prepared-5976480777"})
        packet, _, _ = self.prepare()
        decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK], "recovery": True})
        contracts.write_json(self.work / "evidence.json", reconciliation_evidence(decision))
        contracts.write_json(self.work / "decision.json", {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}],
                                                         "errors": []})
        with self.assertRaises(ValueError):
            with patch.object(live, "clock", side_effect=self.opener.clock):
                hosted.apply(self.work / "prepared/trusted", self.work / "evidence.json", self.work / "decision.json",
                             self.work / "receipt.json", self.service.run, transport=self.transport,
                             host_check=lambda run: None, recovery=self.policy)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_each_pinned_history_boundary_is_rechecked_from_remote_artifacts(self):
        for change in ("source", "actor", "attempt", "workflow", "job-id", "job-source", "job-status",
                       "missing-job", "missing-artifact", "expired-artifact", "artifact-source", "duplicate-artifact",
                       "extra-task-audit", "extra-status-audit", "failure", "fabricated-receipt", "missing-native",
                       "native-tools", "native-duplicate", "native-decision", "native-session", "old-packet", "old-envelope",
                       "observe-effect", "observe-audit", "sequence-gap", "extra-failed-run",
                       "comment-id", "comment-actor", "trial", "operation", "old-run", "old-packet-id",
                       "reserved", "consumed", "uncertain", "new-feedback", "correlated-task", "incomplete-task"):
            with self.subTest(change=change):
                self.setUp()
                self.work = self.work / change
                self.work.mkdir()
                failed = self.service.past[1]
                files = self.service.artifact_files(FAILED, "receipt")
                native = self.service.artifact_files(FAILED, "evidence")["evidence.json"]
                canonical = deepcopy(self.saved)
                if change == "source":
                    failed["head_sha"] = "d" * 40
                elif change == "actor":
                    failed["actor"] = {"id": 999, "login": "radical"}
                elif change == "attempt":
                    failed["run_attempt"] = 2
                elif change == "workflow":
                    failed["workflow_id"] += 1
                elif change.startswith("job-") or change == "missing-job":
                    jobs = self.service.jobs[FAILED]
                    if change == "missing-job":
                        jobs.pop()
                    else:
                        key, value = {"job-id": ("id", 999), "job-source": ("head_sha", "d" * 40),
                                      "job-status": ("conclusion", "cancelled")}[change]
                        jobs[0][key] = value
                elif change in {"missing-artifact", "expired-artifact", "artifact-source", "duplicate-artifact"}:
                    item = self.service.artifacts[FAILED][0]
                    if change == "missing-artifact":
                        self.service.artifacts[FAILED].pop(0)
                    elif change == "expired-artifact":
                        item["expired"] = True
                    elif change == "artifact-source":
                        item["workflow_run"]["head_sha"] = "d" * 40
                    else:
                        self.service.artifacts[FAILED].append({**deepcopy(item), "id": 888})
                elif change in {"extra-task-audit", "extra-status-audit"}:
                    files["audit.json"]["attempts"].append({"kind": "task" if change == "extra-task-audit" else "status"})
                elif change == "failure":
                    files["failure.json"]["stage"] = "prepare"
                elif change == "fabricated-receipt":
                    files["receipt.json"] = {}
                elif change == "missing-native":
                    self.service.artifact_files(FAILED, "evidence").clear()
                elif change == "native-tools":
                    native["debug"] = wire_report(["safeoutputs-submit_decision", "shell"])
                elif change == "native-duplicate":
                    native["events"].insert(-1, deepcopy(next(e for e in native["events"] if e["type"] == "tool.execution_start")))
                elif change == "native-decision":
                    decision = reconciliation_decision(historical_packet(FAILED), "wait")
                    self.service.artifact_files(FAILED, "evidence")["evidence.json"] = reconciliation_evidence(decision)
                elif change == "native-session":
                    native["sessionId"] = "00000000-0000-4000-8000-000000000000"
                    for event in native["events"]:
                        if event["type"] == "session.start":
                            event["data"]["sessionId"] = native["sessionId"]
                        elif event["type"] == "result":
                            event["sessionId"] = native["sessionId"]
                elif change == "old-packet":
                    self.service.artifact_files(FAILED, "prepare")["packet.json"]["packetId"] = "00000000-0000-4000-8000-000000000000"
                elif change == "old-envelope":
                    self.service.artifact_files(FAILED, "prepare")["envelope.json"]["mode"] = "observe"
                elif change == "observe-effect":
                    self.service.artifact_files(OBSERVE, "receipt")["receipt.json"]["effects"] = [{"id": "task", "kind": "worker"}]
                elif change == "observe-audit":
                    self.service.artifact_files(OBSERVE, "receipt")["audit.json"]["attempts"].append({"kind": "task"})
                elif change == "sequence-gap":
                    self.service.past.pop(0)
                elif change == "extra-failed-run":
                    self.service.current_number = 6
                    self.service.past.append({**deepcopy(failed), "id": 37179999999, "run_number": 5, "head_sha": "d" * 40})
                elif change == "comment-id":
                    self.service.comments[0]["id"] += 1
                elif change == "comment-actor":
                    self.service.comments[0]["user"] = {"id": 999, "login": "radical"}
                elif change == "trial":
                    canonical["trialId"] = "00000000-0000-4000-8000-000000000000"
                elif change == "operation":
                    canonical["operations"][0]["id"] = "different-operation"
                elif change == "old-run":
                    canonical["operations"][0]["run"]["runId"] = OBSERVE
                elif change == "old-packet-id":
                    canonical["operations"][0]["packetId"] = "00000000-0000-4000-8000-000000000000"
                elif change in {"reserved", "consumed", "uncertain"}:
                    canonical["operations"][0]["state"] = change
                    canonical["repairBatches"] = 1
                elif change == "new-feedback":
                    self.service.reviews = [{"id": 77, "body": "Additional review", "user": self.service.actor}]
                elif change == "correlated-task":
                    prompt = live.CORRELATION + receipts.canonical({"root": live.ROOT, "trial": TRIAL,
                              "operationId": OPERATION, "sourceHead": live.INITIAL_HEAD})
                    task = self.service.task_value("outcome", prompt)
                    task["state"] = task["sessions"][0]["state"] = "completed"
                    self.service.tasks["outcome"] = task
                else:
                    self.service.tasks["historic-0"].pop("state")
                if canonical != self.saved:
                    self.service.comments[0]["body"] = receipts.render_record(canonical)
                try:
                    packet, _, _ = self.prepare()
                    result = self.apply(packet)
                    self.assertIn(result["outcome"], {"needs-human", "replay"})
                except (ValueError, KeyError):
                    pass
                self.assertEqual(self.service.posts(), [])

    def test_fresh_effect_guards_stop_expiry_rollback_takeover_head_feedback_and_new_outcome(self):
        for change in ("packet-expiry", "trial-expiry", "rollback", "takeover", "head", "feedback", "task", "artifact"):
            with self.subTest(change=change):
                self.setUp()
                self.work = self.work / change
                self.work.mkdir()
                if change == "trial-expiry":
                    self.opener.now = live.issue_pr.timestamp(TRIAL["expiresAt"]) - timedelta(seconds=60)
                    self.opener.reset = self.opener.now + timedelta(seconds=59)
                packet, _, _ = self.prepare()
                if change in {"packet-expiry", "trial-expiry", "rollback"}:
                    def after_sleep(value):
                        if change == "packet-expiry":
                            value.now = live.issue_pr.timestamp(packet["validUntil"])
                        elif change == "trial-expiry":
                            value.now = live.issue_pr.timestamp(TRIAL["expiresAt"])
                        else:
                            value.now -= timedelta(seconds=value.sleeps[-1] + 1)
                    self.opener.after_sleep = after_sleep
                elif change != "trial-expiry":
                    def mutate(service, method, endpoint):
                        record = receipts.parse_body(service.comments[0]["body"])
                        if method != "GET" or not endpoint.endswith("/pulls/121") or record["operations"][0]["state"] != "consumed":
                            return
                        service.before_request = None
                        if change == "takeover":
                            service.pr["labels"].append({"name": "shepherd-hands-off"})
                        elif change == "head":
                            service.pr["head"]["sha"] = "e" * 40
                        elif change == "feedback":
                            service.reviews.append({"id": 77, "body": "New feedback", "user": service.actor})
                        elif change == "artifact":
                            service.artifact_files(FAILED, "receipt")["audit.json"]["attempts"].append({"kind": "task"})
                        else:
                            prompt = live.CORRELATION + receipts.canonical({"root": live.ROOT, "trial": TRIAL,
                                      "operationId": OPERATION, "sourceHead": live.INITIAL_HEAD})
                            service.tasks["unexpected"] = service.task_value("unexpected", prompt)
                    self.service.before_request = mutate
                with self.assertRaises((ValueError, KeyError)):
                    self.apply(packet)
                self.assertEqual(self.service.posts(), [])
                self.assertEqual(receipts.trial_tuple(receipts.parse_body(self.service.comments[0]["body"])), TRIAL)

    def test_completed_current_source_history_cannot_refund_or_redispatch(self):
        packet, _, _ = self.prepare()
        first = self.apply(packet)
        audit = contracts.read_json(self.work / "audit.json")
        self.service.add_history(first, audit)
        self.service.current_number += 1
        self.policy = recovery.PinnedRecovery(self.service.run)
        self.opener.now = self.opener.reset + timedelta(seconds=1)
        self.transport = live.HTTPTransport("credential", write=True, opener=self.opener,
                                            clock_fn=self.opener.clock, sleep_fn=self.opener.sleep)
        self.work = self.work / "next"
        self.work.mkdir()
        packet, _, _ = self.prepare()
        second = self.apply(packet)
        self.assertEqual(second["outcome"], "replay")
        self.assertEqual(len(self.service.posts()), 1)
        self.service.comments[0]["body"] = receipts.render_record(self.saved)
        self.opener.now = self.opener.reset + timedelta(seconds=1)
        self.transport = live.HTTPTransport("credential", write=True, opener=self.opener,
                                            clock_fn=self.opener.clock, sleep_fn=self.opener.sleep)
        self.work = self.work / "refund"
        self.work.mkdir()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(len(self.service.posts()), 1)

    def test_authenticated_completed_audit_budget_cannot_regress_at_prepare(self):
        packet, _, _ = self.prepare()
        first = self.apply(packet)
        self.service.add_history(first, contracts.read_json(self.work / "audit.json"))
        self.service.current_number += 1
        self.policy = recovery.PinnedRecovery(self.service.run)
        self.service.comments[0]["body"] = receipts.render_record(self.saved)
        self.opener.now = self.opener.reset + timedelta(seconds=1)
        self.transport = live.HTTPTransport("credential", write=True, opener=self.opener,
                                            clock_fn=self.opener.clock, sleep_fn=self.opener.sleep)
        self.work = self.work / "regression"
        self.work.mkdir()
        with self.assertRaisesRegex(ValueError, "budget|durable"):
            self.prepare()
        self.assertEqual(len(self.service.posts()), 1)

    def test_unpaced_first_live_failure_leaves_persisted_prepared_not_reserved(self):
        self.service.run = old_run(FAILED)
        self.service.current_number = 4
        self.service.past = self.service.past[:1]
        self.service.comments = []
        self.opener.now = live.issue_pr.timestamp(historical_packet(FAILED)["preparedAt"])
        self.opener.reset = self.opener.now + timedelta(seconds=59)
        packet, _, _ = self.prepare(authorized=False)
        # Apply is a separate authenticated job, after the native reasoning
        # interval. Model a fresh sixty-slot task window at its start.
        self.opener.now = live.issue_pr.timestamp(TRIAL["trialStartedAt"])
        self.opener.remaining = 60
        self.opener.reset = self.opener.now + timedelta(seconds=59)
        self.transport = live.HTTPTransport("credential", write=True, opener=self.opener,
                                            clock_fn=self.opener.clock, sleep_fn=self.opener.sleep)
        with patch.object(self.transport, "_admit_task_request", lambda method, path: None):
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                self.apply(packet, authorized=False)
        canonical = receipts.parse_body(self.service.comments[0]["body"])
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual(canonical, audit["attempts"][1]["record"])
        self.assertEqual(canonical["repairBatches"], 0)
        self.assertEqual(canonical["operations"][0]["state"], "prepared")
        self.assertEqual(audit["attempts"][2]["record"]["repairBatches"], 1)
        self.assertEqual(audit["attempts"][2]["record"]["operations"][0]["state"], "reserved")
        self.assertEqual([item["kind"] for item in audit["attempts"]], ["status"] * 3)
        self.assertEqual(self.service.posts(), [])
        self.assertFalse((self.work / "receipt.json").exists())

    def test_compiled_runtime_requires_explicit_privileged_selector_and_resumes_same_intent(self):
        fixture = HTTPFixture(self.service)
        self.addCleanup(fixture.close)
        source = Path(live.__file__).resolve().parent
        scripts = self.work / ".github/workflows"
        scripts.mkdir(parents=True)
        (scripts / "ci-shepherd").symlink_to(source, target_is_directory=True)
        binaries = self.work / "bin"
        binaries.mkdir()
        shim = binaries / "python3"
        shim.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixture_host.py").read_text())
        shim.chmod(0o700)
        environment = {"PATH": str(binaries.resolve()) + ":" + os.defpath, "TEST_SOURCE": str(source),
                       "TEST_HTTP_PORT": str(fixture.server.server_port), "TEST_CLOCK": self.opener.now.isoformat(),
                       "GITHUB_REPOSITORY": live.REPOSITORY, "GITHUB_RUN_ID": self.service.run["runId"],
                       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW_SHA": self.service.run["workflowSha"],
                       "GITHUB_OUTPUT": str((self.work / "outputs").resolve())}
        pre = compiled_step("Prepare host-owned envelope")
        inputs = {"mode": "live", "resume_prepared": True}
        result = subprocess.run(["bash", "-c", pre["run"]], cwd=self.work,
                                env={**environment, **compiled_environment(pre, self.work, inputs=inputs)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        from test_reasoner_input import inputs as reasoner_inputs
        prompt = "\n".join((self.work / "outputs").read_text().splitlines()[1:-1])
        rendered = reasoner_inputs(prompt)[1]
        self.assertIsNotNone(rendered.get("preparedResumeAdvisory"))
        self.assertEqual(rendered["preparedResumeAdvisory"]["operationId"], OPERATION)
        self.assertLessEqual(len(json.dumps(rendered, ensure_ascii=True).encode()), 4096)
        trusted = self.work / "artifacts/ci-shepherd/prepared/trusted"
        packet = contracts.read_json(trusted / "packet.json")
        decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
        evidence = self.work / "artifacts/ci-shepherd/evidence"
        evidence.mkdir()
        contracts.write_json(evidence / "evidence.json", reconciliation_evidence(decision))
        output = self.work / "decision.json"
        contracts.write_json(output, {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        import shutil
        shutil.copytree(trusted, self.work / "artifacts/ci-shepherd/trusted")
        post = compiled_step("Guarded host apply")
        result = subprocess.run(["bash", "-c", post["run"]], cwd=self.work,
                                env={**environment, **compiled_environment(post, self.work, inputs=inputs),
                                     "GH_AW_AGENT_OUTPUT": str(output.resolve())},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = contracts.read_json(self.work / "artifacts/ci-shepherd/receipt.json")
        self.assertEqual(receipt["outcome"], "confirmed")
        self.assertEqual(receipt["operation"]["id"], OPERATION)
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"])["repairBatches"], 1)
