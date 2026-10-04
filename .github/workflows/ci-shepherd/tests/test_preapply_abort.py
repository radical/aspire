from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
import uuid
from urllib.parse import urlparse
from unittest.mock import patch

from helpers import WorkspaceTest, reconciliation_decision, reconciliation_evidence
from github import Response
from test_pinned_recovery import PinnedService, historical_packet, FAILED, COMMENT, TRIAL, OPERATION, FEEDBACK
from test_rate_limit import WindowOpener
import hosted
import live
import receipts
import recovery
import round as contracts


REVIEWED_SOURCE = "f16d43ba3de8ac7d299301d763d49236f1954843"
ABORT_RUN = "37179900001"


class AbortService(PinnedService):
    def __init__(self, *, attempts=1, source=REVIEWED_SOURCE):
        super().__init__()
        self.current_number = 6
        self.abort_run = {**self.current_run(), "id": int(ABORT_RUN), "run_number": 5, "run_attempt": attempts,
                          "head_sha": source, "status": "completed", "conclusion": "failure",
                          "repository": {"id": live.REPOSITORY_ID, "full_name": live.REPOSITORY}}
        self.past.append(self.abort_run)
        self.artifacts[ABORT_RUN] = []
        self.abort_jobs = {}
        for attempt in range(1, attempts + 1):
            run = {"repository": live.REPOSITORY, "runId": ABORT_RUN, "runAttempt": str(attempt), "workflowSha": source}
            packet = historical_packet(FAILED)
            packet.update(run=run, packetId=str(uuid.uuid4()), record={"commentId": COMMENT, "value": deepcopy(self.prepared)})
            packet["basis"].update(run=run, packetId=packet["packetId"])
            packet["observation"]["comments"] = deepcopy(self.comments)
            packet["observation"]["history"] = {"recordIds": [COMMENT], "publicationAttempts": [TRIAL["trialId"]],
                                                 "associatedOperationIds": [OPERATION]}
            envelope = {"schemaVersion": 1, "packet": packet, "sessionId": None, "mode": "live",
                        "scope": {"root": live.ROOT, "trial": TRIAL}, "context": {},
                        "recovery": recovery.PinnedRecovery(run).descriptor()}
            audit = {"schemaVersion": 1, "run": run, "root": live.ROOT, "mode": "live",
                     "phase": "complete", "attempts": [], "record": deepcopy(self.prepared), "commentId": COMMENT}
            for kind, files in (
                ("prepare", {"packet.json": packet, "envelope.json": envelope}),
                ("prepare-audit", {"audit.json": audit}),
                ("evidence", {"failure.json": {"schemaVersion": 1, "stage": "collect",
                                               "error": "host engine step did not succeed"}}),
            ):
                artifact_id = 200 + len(self.documents)
                self.documents[artifact_id] = deepcopy(files)
                self.artifacts[ABORT_RUN].append({
                    "id": artifact_id, "name": f"ci-shepherd-{kind}-{ABORT_RUN}-{attempt}", "expired": False,
                    "workflow_run": {"id": int(ABORT_RUN), "repository_id": live.REPOSITORY_ID,
                                     "head_repository_id": live.REPOSITORY_ID, "head_sha": source, "head_branch": "main"},
                })
            self.abort_jobs[attempt] = [
                {"id": 30000 + attempt * 10 + index, "name": name, "run_id": int(ABORT_RUN),
                 "run_attempt": attempt, "head_sha": source, "status": "completed", "conclusion": conclusion,
                 "steps": [] if name == "submit_decision" else [{"name": "job step", "status": "completed"}]}
                for index, (name, conclusion) in enumerate((
                    ("prepare", "success"), ("activation", "success"), ("agent", "failure"),
                    ("submit_decision", "skipped"), ("conclusion", "success"),
                ))
            ]

    def abort_files(self, kind, attempt=1):
        item = next(value for value in self.artifacts[ABORT_RUN]
                    if value["name"] == f"ci-shepherd-{kind}-{ABORT_RUN}-{attempt}")
        return self.documents[item["id"]]

    def transport(self, method, endpoint, body):
        path = urlparse(endpoint).path
        prefix = f"repos/{live.REPOSITORY}/actions/runs/{ABORT_RUN}"
        if path == prefix + "/artifacts":
            value = {"artifacts": deepcopy(self.artifacts[ABORT_RUN])}
        elif path.startswith(prefix + "/attempts/") and path.endswith("/jobs"):
            attempt = int(path.split("/")[-2])
            value = {"jobs": deepcopy(self.abort_jobs.get(attempt, []))}
        else:
            return super().transport(method, endpoint, body)
        self.calls.append((method, endpoint, deepcopy(body)))
        return Response(value, {})


class PreApplyAbortTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = AbortService()
        self.now = datetime(2026, 10, 4, 5, 50, tzinfo=timezone.utc)
        self.policy = recovery.PinnedRecovery(self.service.run)

    def prepare(self):
        with patch.object(live, "clock", return_value=self.now):
            return hosted.prepare(self.work / "prepared", "live", self.service.run,
                                  transport=self.service.transport, host_check=lambda run: None, recovery=self.policy)

    def test_reviewed_source_abort_preserves_same_prepared_authority_without_receipt(self):
        failure, result = None, None
        try:
            result = self.prepare()
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Proven GET-only pre-apply abort blocked: " + str(failure))
        packet, envelope, _ = result
        self.assertEqual(packet["record"], {"commentId": COMMENT, "value": self.service.prepared})
        self.assertEqual(envelope["scope"]["trial"], TRIAL)
        self.assertEqual(packet["record"]["value"]["repairBatches"], 0)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        github = live.FixtureGitHub(self.service.transport, self.service.run, recovery=self.policy)
        github.refresh(live.ROOT)
        self.assertEqual(github.history.aborted_attempts, [{
            "run": self.service.abort_files("prepare")["packet.json"]["run"],
            "disposition": "pre-apply-abort",
        }])
        self.assertIn(self.service.prepared, github.history.durable_records)
        self.assertTrue(github.history.resume_allowed)

    def test_current_authenticated_source_uses_same_single_attempt_protocol(self):
        self.service = AbortService(source=self.service.run["workflowSha"])
        packet, _, _ = self.prepare()
        paths = [urlparse(call[1]).path for call in self.service.calls]
        self.assertIn(f"repos/{live.REPOSITORY}/actions/runs/{ABORT_RUN}/attempts/1/jobs", paths)
        self.assertEqual(packet["record"]["value"], self.service.prepared)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_matching_protocol_and_main_branch_do_not_register_unknown_source(self):
        self.service = AbortService(source="e" * 40)
        self.service.abort_run["head_branch"] = "main"
        with self.assertRaisesRegex(ValueError, "source/run/actor is not trusted"):
            self.prepare()
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.prepared)

    def test_complete_reruns_never_qualify_as_neutral_aborts(self):
        for source in (REVIEWED_SOURCE, self.service.run["workflowSha"]):
            for attempts in (2, 10):
                with self.subTest(source=source, attempts=attempts):
                    self.setUp()
                    self.service = AbortService(attempts=attempts, source=source)
                    with self.assertRaisesRegex(ValueError, "attempt 1"):
                        self.prepare()
                    self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
                    self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.prepared)

    def test_skipped_writer_with_populated_start_time_retains_unexecuted_proof(self):
        writer = next(job for job in self.service.abort_jobs[1] if job["name"] == "submit_decision")
        writer["started_at"] = "2026-10-04T05:45:00Z"
        writer["completed_at"] = "2026-10-04T05:45:00Z"
        packet, _, _ = self.prepare()
        self.assertEqual(packet["record"]["value"], self.service.prepared)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_full_rest_actor_object_uses_login_and_immutable_user_id(self):
        self.service.abort_run["actor"] = {**self.service.actor, "type": "User", "site_admin": False}
        packet, _, _ = self.prepare()
        self.assertEqual(packet["record"]["value"], self.service.prepared)

    def test_malformed_prepare_audit_and_failure_shapes_cannot_qualify(self):
        for change in ("audit-schema", "failure-schema", "audit-record", "context"):
            with self.subTest(change=change):
                self.setUp()
                if change == "audit-schema":
                    self.service.abort_files("prepare-audit")["audit.json"]["schemaVersion"] = True
                elif change == "failure-schema":
                    self.service.abort_files("evidence")["failure.json"]["schemaVersion"] = True
                elif change == "audit-record":
                    self.service.abort_files("prepare-audit")["audit.json"]["record"]["repairBatches"] = False
                else:
                    self.service.abort_files("prepare")["envelope.json"]["context"] = []
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_single_attempt_cannot_hide_an_executed_writer_or_duplicate_job_identity(self):
        for change in ("writer", "job-id"):
            with self.subTest(change=change):
                self.setUp()
                if change == "writer":
                    self.service.abort_jobs[1][3]["steps"].append({"name": "Guarded host apply", "status": "completed"})
                else:
                    self.service.abort_jobs[1][1]["id"] = self.service.abort_jobs[1][0]["id"]
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_skipped_writer_does_not_authorize_another_operation_or_ignore_wait(self):
        packet, _, _ = self.prepare()
        decision = reconciliation_decision(packet, "wait")
        contracts.write_json(self.work / "evidence.json", reconciliation_evidence(decision))
        contracts.write_json(self.work / "decision.json",
                             {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        with patch.object(live, "clock", return_value=self.now):
            result = hosted.apply(self.work / "prepared/trusted", self.work / "evidence.json",
                                  self.work / "decision.json", self.work / "receipt.json", self.service.run,
                                  transport=self.service.transport, host_check=lambda run: None, recovery=self.policy)
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"]), self.service.prepared)

    def test_abort_history_and_quota_resume_same_operation_with_one_reservation_and_send(self):
        opener = WindowOpener()
        opener.service, opener.now = self.service, self.now
        opener.reset = self.now + timedelta(seconds=59)
        transport = live.HTTPTransport("credential", write=True, opener=opener,
                                       clock_fn=opener.clock, sleep_fn=opener.sleep)
        with patch.object(live, "clock", side_effect=opener.clock):
            packet, _, _ = hosted.prepare(self.work / "prepared", "live", self.service.run, transport=transport,
                                           host_check=lambda run: None, recovery=self.policy)
            decision = reconciliation_decision(packet, arguments={"feedbackIds": [FEEDBACK]})
            contracts.write_json(self.work / "evidence.json", reconciliation_evidence(decision))
            contracts.write_json(self.work / "decision.json", {
                "items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
            result = hosted.apply(self.work / "prepared/trusted", self.work / "evidence.json",
                                  self.work / "decision.json", self.work / "receipt.json", self.service.run,
                                  transport=transport, host_check=lambda run: None, recovery=self.policy)
        self.assertEqual(result["outcome"], "confirmed")
        canonical = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(receipts.trial_tuple(canonical), TRIAL)
        self.assertEqual(canonical["repairBatches"], 1)
        self.assertEqual(len(canonical["operations"]), 1)
        for key in ("id", "identity", "run", "packetId"):
            self.assertEqual(canonical["operations"][0][key], self.service.prepared["operations"][0][key])
        self.assertEqual(len(self.service.posts()), 1)
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual([item["record"]["operations"][0]["state"] for item in audit["attempts"] if item["kind"] == "status"],
                         ["reserved", "consumed", "confirmed"])
        self.assertTrue(opener.sleeps)
        self.assertLessEqual(sum(opener.sleeps), 180)
        for method, endpoint, at in opener.requests:
            if method == "POST":
                self.assertLess(at, live.issue_pr.timestamp(packet["validUntil"]), endpoint)

    def test_each_abort_boundary_blocks_without_effects(self):
        changes = ("source", "actor", "actor-id", "repository", "workflow", "event", "incomplete-run",
                   "cancelled-run", "successful-run", "missing-attempt", "duplicate-job", "extra-credential-job", "job-source",
                   "job-attempt", "job-run", "missing-job", "prepare-failed", "activation-failed",
                   "agent-success", "conclusion-failed", "apply-failed", "apply-cancelled", "apply-executed",
                   "missing-steps", "missing-artifact", "expired-artifact", "artifact-source",
                   "artifact-repository", "duplicate-artifact", "receipt-artifact", "extra-zip-member",
                   "prepare-phase", "prepare-task-attempt", "packet", "scope", "recovery", "canonical",
                   "audit-record", "comment", "budget", "native-success", "native-phase")
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                run = self.service.abort_run
                jobs = self.service.abort_jobs[1]
                prepared = self.service.abort_files("prepare")
                packet, envelope = prepared["packet.json"], prepared["envelope.json"]
                audit = self.service.abort_files("prepare-audit")["audit.json"]
                if change in {"source", "actor", "actor-id", "repository", "workflow", "event", "incomplete-run", "cancelled-run", "successful-run"}:
                    if change == "source":
                        run["head_sha"] = "e" * 40
                    elif change == "actor":
                        run["actor"] = {"id": 1472, "login": "someone-else"}
                    elif change == "actor-id":
                        run["actor"] = {"id": 999, "login": "radical"}
                    elif change == "repository":
                        run["repository"]["id"] += 1
                    elif change == "workflow":
                        run["workflow_id"] += 1
                    elif change == "event":
                        run["event"] = "pull_request"
                    elif change == "successful-run":
                        run["conclusion"] = "success"
                    else:
                        run["status" if change == "incomplete-run" else "conclusion"] = "in_progress" if change == "incomplete-run" else "cancelled"
                elif change == "missing-attempt":
                    run["run_attempt"] = 2
                elif change == "duplicate-job":
                    jobs.append(deepcopy(jobs[0]))
                elif change == "extra-credential-job":
                    jobs.append({**deepcopy(jobs[0]), "id": 999, "name": "extra-writer"})
                elif change in {"job-source", "job-attempt", "job-run", "missing-job"}:
                    if change == "missing-job":
                        jobs.pop()
                    else:
                        key, value = {"job-source": ("head_sha", "e" * 40), "job-attempt": ("run_attempt", 2),
                                      "job-run": ("run_id", 123)}[change]
                        jobs[0][key] = value
                elif change.endswith("-failed") or change in {"agent-success", "apply-cancelled"}:
                    name = {"prepare-failed": "prepare", "activation-failed": "activation", "agent-success": "agent",
                            "conclusion-failed": "conclusion", "apply-failed": "submit_decision",
                            "apply-cancelled": "submit_decision"}[change]
                    next(job for job in jobs if job["name"] == name)["conclusion"] = (
                        "success" if change == "agent-success" else "cancelled" if change == "apply-cancelled" else "failure")
                elif change in {"apply-executed", "missing-steps"}:
                    job = next(job for job in jobs if job["name"] == "submit_decision")
                    if change == "apply-executed":
                        job["steps"].append({"name": "Guarded host apply", "status": "completed"})
                    else:
                        job.pop("steps")
                elif change in {"missing-artifact", "expired-artifact", "artifact-source", "artifact-repository", "duplicate-artifact", "receipt-artifact"}:
                    artifacts = self.service.artifacts[ABORT_RUN]
                    if change == "missing-artifact":
                        artifacts.pop(0)
                    elif change == "expired-artifact":
                        artifacts[0]["expired"] = True
                    elif change == "artifact-source":
                        artifacts[0]["workflow_run"]["head_sha"] = "e" * 40
                    elif change == "artifact-repository":
                        artifacts[0]["workflow_run"]["head_repository_id"] += 1
                    else:
                        artifacts.append({**deepcopy(artifacts[0]), "id": 999,
                                          "name": f"ci-shepherd-receipt-{ABORT_RUN}-1" if change == "receipt-artifact" else artifacts[0]["name"]})
                elif change == "extra-zip-member":
                    prepared["receipt.json"] = {}
                elif change == "prepare-phase":
                    audit["phase"] = "failed"
                elif change == "prepare-task-attempt":
                    audit["attempts"].append({"kind": "task"})
                elif change == "packet":
                    packet["run"]["runAttempt"] = "2"
                elif change == "scope":
                    envelope["scope"]["trial"] = None
                elif change == "recovery":
                    envelope["recovery"]["kind"] = "model-installed"
                elif change == "canonical":
                    packet["observation"]["comments"][0]["user"] = {"id": 999, "login": "radical"}
                elif change == "audit-record":
                    audit["record"] = None
                elif change == "comment":
                    audit["commentId"] += 1
                elif change == "budget":
                    record = deepcopy(self.service.prepared)
                    receipts.reserve(record, record["operations"][0])
                    packet["record"]["value"] = deepcopy(record)
                    packet["observation"]["comments"][0]["body"] = receipts.render_record(record)
                    audit["record"] = deepcopy(record)
                elif change == "native-success":
                    files = self.service.abort_files("evidence")
                    files.clear()
                    files["evidence.json"] = reconciliation_evidence(reconciliation_decision(packet, "wait"))
                else:
                    self.service.abort_files("evidence")["failure.json"]["stage"] = "apply"
                with self.assertRaises((ValueError, KeyError)):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
