from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from helpers import WorkspaceTest, reconciliation_decision, reconciliation_evidence
from github import Response
from test_failed_apply import FailedApplyService, TASK
from test_pinned_recovery import COMMENT, TRIAL
import test_second_round
import hosted
import live
import receipts
import recovery
import round as contracts


SOURCE = "9ee8070c8d5e1f6bb8a8324ad386e0e17b7fa7d5"
SUCCESS_RUN = "37187740001"


class SuccessfulService(FailedApplyService):
    def __init__(self, source=SOURCE):
        super().__init__()
        self.tasks[TASK]["state"] = self.tasks[TASK]["sessions"][0]["state"] = "completed"
        self.pr["head"]["sha"] = self.ci_head = "c" * 40
        github = live.FixtureGitHub(self.transport, self.run, recovery=recovery.PinnedRecovery(self.run))
        run = {"repository": live.REPOSITORY, "runId": SUCCESS_RUN, "runAttempt": "1", "workflowSha": source}
        packet = contracts.prepare_reconciliation(
            github, live.ROOT, live.ROOT, run, lambda: datetime(2026, 10, 4, 7, tzinfo=timezone.utc),
            receipts.TrialScope(live.ROOT, TRIAL))
        context = deepcopy(github.context)
        context.pop("repairScope", None)
        envelope = {"schemaVersion": 1, "packet": packet, "sessionId": None, "mode": "live",
                    "scope": {"root": live.ROOT, "trial": TRIAL}, "context": context,
                    "recovery": recovery.PinnedRecovery(run).descriptor()}
        native = reconciliation_evidence(reconciliation_decision(packet, "wait"))
        self.confirmed = deepcopy(self.consumed)
        self.confirmed["operations"][0].update(state="confirmed", result={"id": TASK, "kind": "worker"})
        receipt = {"schemaVersion": 1, "run": run, "mode": "live", "root": live.ROOT,
                   "packetId": packet["packetId"], "sessionId": native["sessionId"],
                   "outcome": "confirmed", "effects": [], "recovered": True,
                   "operation": self.confirmed["operations"][0], "currentHead": self.ci_head,
                   "tasks": context["tasks"], "gate": context["gate"], "push": context["push"]}
        prepare_audit = {"schemaVersion": 1, "run": run, "root": live.ROOT, "mode": "live", "phase": "complete",
                         "attempts": [], "record": self.consumed, "commentId": COMMENT}
        audit = {**deepcopy(prepare_audit), "record": self.confirmed,
                 "attempts": [{"kind": "status", "record": self.confirmed, "commentId": COMMENT}]}
        self.artifacts[SUCCESS_RUN] = []
        for kind, files in (
            ("prepare", {"packet.json": packet, "envelope.json": envelope}),
            ("prepare-audit", {"audit.json": prepare_audit}),
            ("evidence", {"evidence.json": native}),
            ("receipt", {"audit.json": audit, "receipt.json": receipt}),
        ):
            artifact_id = 700 + len(self.documents)
            self.documents[artifact_id] = json.loads(json.dumps(files))
            self.artifacts[SUCCESS_RUN].append({
                "id": artifact_id, "name": f"ci-shepherd-{kind}-{SUCCESS_RUN}-1", "expired": False,
                "workflow_run": {"id": int(SUCCESS_RUN), "repository_id": live.REPOSITORY_ID,
                                 "head_repository_id": live.REPOSITORY_ID, "head_sha": source, "head_branch": "main"}})
        self.success_run = {**deepcopy(self.failed_run), "id": int(SUCCESS_RUN), "run_number": 7,
                            "head_sha": source, "head_branch": "main", "conclusion": "success"}
        self.success_jobs = [
            {"id": 70000 + index, "name": name, "run_id": int(SUCCESS_RUN), "run_attempt": 1, "head_sha": source,
             "status": "completed", "conclusion": "success",
             "steps": [{"name": "Guarded host apply" if name == "submit_decision" else "job step",
                        "status": "completed", "conclusion": "success"}]}
            for index, name in enumerate(("prepare", "activation", "agent", "submit_decision", "conclusion"))]
        self.job_count_offset = 0
        self.artifact_count_offset = 0
        self.current_number = 8
        self.past.append(self.success_run)
        self.comments[0]["body"] = receipts.render_record(self.confirmed)

    def success_files(self, kind):
        artifact = next(value for value in self.artifacts[SUCCESS_RUN]
                        if value["name"] == f"ci-shepherd-{kind}-{SUCCESS_RUN}-1")
        return self.documents[artifact["id"]]

    def transport(self, method, endpoint, body):
        path = urlparse(endpoint).path
        prefix = f"repos/{live.REPOSITORY}/actions/runs/{SUCCESS_RUN}"
        if path == prefix + "/artifacts":
            value = {"artifacts": deepcopy(self.artifacts[SUCCESS_RUN]),
                     "total_count": len(self.artifacts[SUCCESS_RUN]) + self.artifact_count_offset}
        elif path == prefix + "/attempts/1/jobs":
            value = {"jobs": deepcopy(self.success_jobs), "total_count": len(self.success_jobs) + self.job_count_offset}
        else:
            return super().transport(method, endpoint, body)
        self.calls.append((method, endpoint, deepcopy(body)))
        return Response(value, {})


class SuccessfulHistoryTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = SuccessfulService()
        self.now = datetime(2026, 10, 4, 7, 30, tzinfo=timezone.utc)
        self.policy = recovery.PinnedRecovery(self.service.run)
        self.invocations = 0

    def prepare(self):
        with patch.object(live, "clock", return_value=self.now):
            return hosted.prepare(self.work / "prepared", "live", self.service.run,
                                  transport=self.service.transport, host_check=lambda run: None, recovery=self.policy)

    def test_registered_successful_wait_source_preserves_confirmed_floor(self):
        result, failure = None, None
        try:
            result = self.prepare()
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Reviewed successful WAIT source was rejected: " + str(failure))
        packet, envelope, _ = result
        self.assertEqual(packet["record"], {"commentId": COMMENT, "value": self.service.confirmed})
        self.assertEqual(envelope["scope"]["trial"], TRIAL)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        github = live.FixtureGitHub(self.service.transport, self.service.run, recovery=self.policy)
        github.refresh(live.ROOT)
        self.assertIn(self.service.confirmed, github.history.durable_records)
        self.assertFalse(github.history.resume_allowed)

    def test_successful_history_allows_one_new_head_operation_without_reset(self):
        self.service.commit_shas = ["c" * 40, "d" * 40]
        self.service.pr["head"]["sha"] = self.service.ci_head = "d" * 40
        result, failure = None, None
        try:
            packet, _, _ = self.prepare()
            result = test_second_round.SecondRoundTests.apply(self, packet)
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Successful-source floor prevented second operation: " + str(failure))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)
        current = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(receipts.trial_tuple(current), TRIAL)
        self.assertEqual(current["repairBatches"], 2)
        self.assertEqual(current["operations"][0], self.service.confirmed["operations"][0])
        self.assertEqual(len(current["operations"]), 2)

    def test_unknown_source_does_not_inherit_successful_protocol(self):
        self.service = SuccessfulService(source="e" * 40)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.service.posts(), [])

    def test_rest_actor_metadata_does_not_replace_identity_verification(self):
        self.service.success_run["actor"].update(node_id="actor-node", type="User")
        result, failure = None, None
        try:
            result = self.prepare()
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Authenticated REST actor metadata was rejected: " + str(failure))
        self.assertEqual(result[0]["record"]["value"], self.service.confirmed)

    def test_registered_source_failure_or_rerun_is_not_successful_history(self):
        for index, change in enumerate(({"conclusion": "failure"}, {"run_attempt": 2})):
            with self.subTest(change=change):
                self.service = SuccessfulService()
                self.service.success_run.update(change)
                self.work = self.work / str(index)
                self.work.mkdir()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(self.service.posts(), [])

    def test_changed_native_audit_provenance_or_jobs_cannot_certify_success(self):
        mutations = [
            lambda s: s.success_run["actor"].update(id=999),
            lambda s: s.success_run.update(head_branch="unreviewed"),
            lambda s: setattr(s, "job_count_offset", 1),
            lambda s: setattr(s, "artifact_count_offset", 1),
            lambda s: s.success_jobs.append({**s.success_jobs[0], "id": 999, "name": "extra-writer"}),
            lambda s: s.success_jobs[-2].update(conclusion="failure"),
            lambda s: s.success_jobs[-2]["steps"][0].update(conclusion="skipped"),
            lambda s: s.artifacts[SUCCESS_RUN][0].update(expired=True),
            lambda s: s.artifacts[SUCCESS_RUN][0]["workflow_run"].update(head_sha="e" * 40),
            lambda s: s.success_files("prepare")["envelope.json"]["scope"].update(trial=None),
            lambda s: s.success_files("prepare")["envelope.json"]["scope"]["root"].update(number=121.0),
            lambda s: s.success_files("prepare-audit")["audit.json"].update(phase="failed"),
            lambda s: s.success_files("evidence")["evidence.json"].update(sessionId="forged"),
            lambda s: s.success_files("receipt")["audit.json"]["attempts"].append({"kind": "task"}),
            lambda s: s.success_files("receipt")["audit.json"]["record"].update(repairBatches=2),
            lambda s: s.success_files("receipt")["audit.json"]["attempts"][0].update(commentId=float(COMMENT)),
            lambda s: s.success_files("receipt")["audit.json"]["attempts"][0]["record"].update(repairBatches=True),
            lambda s: s.success_files("receipt")["receipt.json"].update(sessionId="forged"),
            lambda s: s.success_files("receipt")["receipt.json"]["operation"]["identity"]["root"].update(number=121.0),
            lambda s: s.success_files("receipt")["receipt.json"]["operation"]["result"].update(id="forged"),
            lambda s: s.success_files("receipt").update({"unexpected.json": {}}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.service = SuccessfulService()
                mutate(self.service)
                self.work = self.work / str(index)
                self.work.mkdir()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_current_consumption_floor_and_unique_task_remain_mandatory(self):
        mutations = [
            lambda s: s.comments[0].update(body=receipts.render_record(s.consumed)),
            lambda s: s.comments.clear(),
            lambda s: s.tasks.pop(TASK),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.service = SuccessfulService()
                mutate(self.service)
                self.work = self.work / str(index)
                self.work.mkdir()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
