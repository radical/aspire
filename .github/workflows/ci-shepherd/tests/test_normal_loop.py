"""Normal host entrypoints with fresh clients and serialized remote authority."""

from copy import deepcopy
from datetime import timedelta
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from github import LostResponse, Response
from helpers import FakeClock, reconciliation_evidence
import hosted
import live
import pilot
import pilot_binding as bindings
import pilot_state as state
import result_collector
import round as contracts
import work_item_execution as execution
import pilot_github
import run_report
from test_pilot_github import Transport
from test_work_items import result


RUN = {"repository": "radical/aspire", "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40}
FEED = {"reported_kind": "workflow_failure",
        "issue_url": "https://github.com/microsoft/aspire/issues/20872"}
SESSION = "12345678-1234-4234-8234-123456789abc"


class NormalLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = FakeClock()
        self.remote = Transport()
        self.remote.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        self.prefix = "repos/microsoft/aspire"
        self.repo = {"id": bindings.UPSTREAM.repository_id, "full_name": "microsoft/aspire"}
        self.issue = {"number": 20872, "node_id": "ISSUE20872", "state": "open",
                      "repository_url": "https://api.github.com/" + self.prefix, "labels": [],
                      "title": "Main CI failure", "updated_at": "2026-10-04T00:00:00Z",
                      "body": "\n".join(f"https://github.com/microsoft/aspire/actions/runs/{n}"
                                       for n in range(100, 104))}
        self.remote.values.update({self.prefix: dict(self.repo, default_branch="main"),
                                   self.prefix + "/issues/20872": self.issue})
        for n in range(100, 104):
            run = {"id": n, "run_attempt": 1, "head_sha": "a" * 40,
                   "repository": self.repo, "status": "completed", "conclusion": "failure"}
            job = {"id": n + 100, "run_id": n, "run_attempt": 1, "head_sha": "a" * 40,
                   "status": "completed", "conclusion": "failure", "name": "Unit tests", "steps": []}
            self.remote.values[self.prefix + f"/actions/runs/{n}"] = run
            self.remote.values[self.prefix + f"/actions/runs/{n}/attempts/1"] = run
            self.remote.values[self.prefix + f"/actions/runs/{n}/attempts/1/jobs"] = {
                "total_count": 1, "jobs": [job]}
            self.remote.values[self.prefix + f"/actions/jobs/{n + 100}"] = job
        self.env = {
            "CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_USER_TOKEN": "gho_fixture",
            "SHEPHERD_TARGET": "upstream", "CI_SHEPHERD_UPSTREAM_TRACKER": "99",
            "CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT": "500",
            "CI_SHEPHERD_UPSTREAM_TRACKER_NODE": "TRACKER99", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTIONS": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fixture-oidc",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.actions.githubusercontent.com/token",
        }
        self.posts = []
        self.lost = False
        self.logs = None

        def http(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            if method == "POST" and endpoint == "agents/repos/microsoft/aspire/tasks":
                self.posts.append((deepcopy(body), self.saved()))
                if self.lost:
                    raise LostResponse("lost task receipt")
                return Response({"id": "TASK1"}, {}, 201)
            return self.remote(method, endpoint, body)

        def command(argv, environment, **kwargs):
            if argv == ["gh", "--version"]:
                return 0, "gh version 2.102.0 (2026-09-30)", ""
            if argv[:2] == ["gh", "api"]:
                return 0, json.dumps(self.repo | {"login": "radical", "id": 1472}), ""
            if argv[:2] == ["gh", "auth"]:
                return 0, "gho_fixture\n", ""
            if argv[:3] == ["gh", "agent-task", "view"]:
                self.assertEqual(SESSION, argv[3])
                return 0, self.logs or "No structured result.", ""
            raise AssertionError(argv)

        owner = self
        class OIDC:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def open(self, request, timeout):
                return self
            def read(self, limit):
                now = int(owner.clock().timestamp())
                claims = {"aud": "ci-shepherd-existing-pr", "iss": "https://token.actions.githubusercontent.com",
                          "repository": RUN["repository"], "run_id": RUN["runId"], "run_attempt": "1",
                          "workflow_sha": RUN["workflowSha"], "actor": "radical",
                          "repository_id": str(live.REPOSITORY_ID), "runner_environment": "github-hosted",
                          "event_name": "workflow_dispatch", "ref": "refs/heads/controller",
                          "workflow_ref": live.REPOSITORY + "/" + live.WORKFLOW + "@refs/heads/controller",
                          "nbf": now - 1, "exp": now + 60}
                encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                return json.dumps({"value": "header." + encoded + ".signature"}).encode()

        for mock in (patch.dict(os.environ, self.env, clear=True),
                     patch.object(hosted, "build_opener", return_value=OIDC()),
                     patch.object(live.HTTPTransport, "__call__", http),
                     patch.object(result_collector, "bounded_command", command),
                     patch.object(live, "clock", self.clock)):
            mock.start()
            self.addCleanup(mock.stop)
        self.sequence = 0

    def saved(self):
        return state.parse(self.remote.comments[0]["body"])

    def prepare(self, feed=None):
        self.sequence += 1
        directory = self.root / str(self.sequence)
        if feed is None:
            os.environ.pop("SHEPHERD_ISSUE_FEED", None)
        else:
            os.environ["SHEPHERD_ISSUE_FEED"] = json.dumps(feed)
        return hosted.prepare(directory, "pilot", RUN)[0]

    def settle(self, packet, decision=None, *, usage=2, evidence=True, disabled=False):
        value = reconciliation_evidence(decision) if evidence and decision is not None else None
        return pilot.settle(pilot.hosted_api(RUN, os.environ, billing=True),
                            packet, value, usage, self.clock(), billing_only=disabled)

    def intake(self, packet, action="investigate"):
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "action": action, "route": "workflow_failure" if action == "investigate" else None,
                "evidenceId": packet["context"]["candidates"][0]["id"] if action == "investigate" else None,
                "reason": "Investigate the referenced failed occurrence."}

    def launch(self):
        packet = self.prepare(FEED)
        self.assertIsNotNone(packet, "minimal feed must admit normal intake, not skip upstream issues")
        receipt = self.settle(packet, self.intake(packet))
        self.assertEqual("approval-required", receipt["outcome"])
        self.assertEqual([], self.posts)
        os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = json.dumps(receipt["approval_template"])
        self.prepare()
        self.assertEqual(1, len(self.posts))
        self.assertEqual("sending", self.posts[0][1]["workItems"][0]["assignments"][0]["execution"]["state"])
        self.assertEqual("TASK1", self.saved()["workItems"][0]["assignments"][0]["execution"]["task_id"])
        self.task("in_progress")

    def task(self, status):
        record = self.saved()["workItems"][0]
        assignment = record["assignments"][0]
        api = pilot.hosted_api(RUN, os.environ)
        saved = {"id": "TASK1", "repository": self.repo, "creator": {"id": 1472}, "state": status,
                 "session_count": 1, "artifacts": [], "sessions": [{
                     "id": SESSION, "task_id": "TASK1", "repository": self.repo, "user": {"id": 1472},
                     "state": status, "base_ref": "main", "head_ref": "copilot/diagnose",
                     "prompt": execution.request(api, record, assignment)["prompt"],
                     "usage": {"type": "ai_credits", "amount": 10_000_000_000}}]}
        self.remote.values["agents/repos/microsoft/aspire/tasks/TASK1"] = saved
        return saved

    def terminal(self):
        self.launch()
        self.task("completed")
        assignment = self.saved()["workItems"][0]["assignments"][0]
        claimed = result(assignment)
        claimed.update(item_id=self.saved()["workItems"][0]["id"], worker_id=SESSION,
                       outcome="no_fix", changed_files=[], pr_proposal=None,
                       actual_classification="inconclusive", summary="No justified fix.")
        self.logs = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"

    def assessment(self, packet):
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "action": "assess", "assessment": {"summary": "Worker reported no fix; cause remains unknown.",
                    "evidence": ["Exact session result is available."], "unknowns": ["Root cause is not validated."]}}

    def test_minimal_feed_launch_and_fresh_no_feed_terminal_assessment(self):
        self.terminal()
        packet = self.prepare()
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertEqual("no_fix", packet["context"]["result"]["outcome"])
        self.assertEqual("assessed", self.settle(packet, self.assessment(packet))["outcome"])
        self.assertIsNone(self.prepare(FEED))
        self.assertEqual(1, len(self.posts))
        self.assertEqual("open", self.issue["state"])
        self.assertTrue(all(method == "PATCH" for method, endpoint, _ in self.remote.writes))

    def test_absent_feed_retains_legacy_empty_sweep(self):
        self.assertIsNone(self.prepare())
        self.assertEqual(state.new_ledger("microsoft/aspire"), self.saved())

    def test_foreign_candidate_rejected_and_usage_saved(self):
        packet = self.prepare(FEED)
        self.assertIsNotNone(packet)
        decision = self.intake(packet) | {"evidenceId": "foreign"}
        self.assertEqual("rejected", self.settle(packet, decision)["outcome"])
        self.assertEqual(2, self.saved()["normalRuns"][0]["nativeActual"])
        self.assertEqual([], self.posts)

    def test_closed_source_settles_native_bill_without_admission(self):
        packet = self.prepare(FEED)
        self.assertIsNotNone(packet)
        self.issue["state"] = "closed"
        self.assertEqual("rejected", self.settle(packet, self.intake(packet))["outcome"])
        self.assertEqual(2, self.saved()["normalRuns"][0]["nativeActual"])
        self.assertEqual([], self.posts)

    def test_active_resumed_and_closed_sources_do_not_assess_or_replace(self):
        self.launch()
        self.assertIsNone(self.prepare(FEED))
        self.task("completed")
        self.logs = "no result"
        packet = self.prepare()
        self.task("in_progress")
        self.assertEqual("rejected", self.settle(packet, self.assessment(packet))["outcome"])
        self.issue["state"] = "closed"
        self.assertIsNone(self.prepare(FEED))
        self.assertEqual(1, len(self.posts))
        self.assertEqual("TASK1", self.saved()["workItems"][0]["assignments"][0]["execution"]["task_id"])

    def test_missing_result_is_assessed_without_synthetic_worker_claims(self):
        self.launch()
        self.task("completed")
        packet = self.prepare()
        self.assertIsNone(packet["context"]["result"])
        self.assertEqual("unavailable", packet["context"]["followup"]["status"])
        self.assertEqual("assessed", self.settle(packet, self.assessment(packet))["outcome"])

    def test_uncertain_send_replay_never_posts_twice(self):
        packet = self.prepare(FEED)
        self.assertIsNotNone(packet)
        receipt = self.settle(packet, self.intake(packet))
        os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = json.dumps(receipt["approval_template"])
        self.lost = True
        self.prepare()
        self.prepare(FEED)
        self.assertEqual(1, len(self.posts))
        self.assertEqual("uncertain", self.saved()["workItems"][0]["assignments"][0]["execution"]["state"])

    def test_normal_cli_settlement_does_not_look_up_legacy_chain(self):
        packet = self.prepare(FEED)
        trusted = self.root / "trusted"
        trusted.mkdir()
        contracts.write_json(trusted / "packet.json", packet)
        contracts.write_json(self.root / "evidence.json", reconciliation_evidence(self.intake(packet)))
        contracts.write_json(self.root / "usage.json", {"ai_credits": 2})
        os.environ.update(GITHUB_REPOSITORY=RUN["repository"], GITHUB_RUN_ID=RUN["runId"],
                          GITHUB_RUN_ATTEMPT="1", GITHUB_WORKFLOW_SHA=RUN["workflowSha"],
                          GITHUB_OUTPUT=str(self.root / "output"))
        self.assertEqual(0, pilot.main(["settle", "--trusted", str(trusted),
            "--evidence", str(self.root / "evidence.json"), "--usage", str(self.root / "usage.json"),
            "--result", str(self.root / "receipt.json")]))
        self.assertEqual("approval-required", contracts.read_json(self.root / "receipt.json")["outcome"])

    def test_safe_output_handler_accepts_normal_schema_without_effects(self):
        packet = self.prepare(FEED)
        decision = self.intake(packet)
        directory = self.root / str(self.sequence) / "trusted"
        contracts.write_json(self.root / "evidence.json", reconciliation_evidence(decision))
        contracts.write_json(self.root / "decision.json", {
            "items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        hosted.apply(directory, self.root / "evidence.json", self.root / "decision.json",
                     self.root / "receipt.json", RUN)
        self.assertEqual("pilot-decision-verified", contracts.read_json(self.root / "receipt.json")["outcome"])
        self.assertEqual([], self.posts)

    def test_all_four_references_are_primary_candidates_and_extra_feed_fields_are_denied(self):
        packet = self.prepare(FEED)
        self.assertEqual([100, 101, 102, 103],
                         [value["occurrence"]["run_id"] for value in packet["context"]["candidates"]])
        with self.assertRaises(ValueError):
            self.prepare(FEED | {"requested_route": "workflow_failure"})
        with self.assertRaises(ValueError):
            self.prepare(FEED | {"issue_url": "https://github.com/radical/aspire/issues/20872"})

    def test_replayed_and_missing_evidence_preserve_billing_and_unknown_holds(self):
        packet = self.prepare(FEED)
        self.assertEqual("rejected", self.settle(packet, evidence=False, usage=None)["outcome"])
        self.assertEqual(30, self.saved()["normalRuns"][0]["nativeReserved"])
        self.assertEqual("replay", self.settle(packet, evidence=False, usage=2)["outcome"])
        self.assertEqual(0, self.saved()["normalRuns"][0]["nativeReserved"])
        self.assertIsNone(self.prepare(FEED))

    def test_disabled_expired_and_edited_packets_do_not_admit_workers(self):
        for change in ("disabled", "expired", "edited"):
            with self.subTest(change=change):
                self.remote.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
                packet = self.prepare(FEED)
                if change == "expired":
                    self.clock.advance(seconds=601)
                if change == "edited":
                    self.issue["title"] += " edited"
                self.assertEqual("rejected", self.settle(packet, self.intake(packet),
                                 disabled=change == "disabled")["outcome"])
                self.assertEqual(2, self.saved()["normalRuns"][0]["nativeActual"])
        self.assertEqual([], self.posts)

    def test_paused_stale_and_hands_off_controls_retain_receipts_without_collection(self):
        self.launch()
        original = self.saved()
        for change in ("pause", "revision", "hands-off"):
            with self.subTest(change=change):
                saved = deepcopy(original)
                if change == "pause":
                    saved["workItems"][0]["control"]["action"] = "pause"
                elif change == "revision":
                    saved["workItems"][0]["control"]["revision"] = 2
                else:
                    self.issue["labels"] = [{"name": "shepherd-hands-off"}]
                self.remote.comments[0]["body"] = state.render(saved)
                self.task("completed")
                self.assertIsNone(self.prepare(FEED))
                self.assertEqual("TASK1", self.saved()["workItems"][0]["assignments"][0]["execution"]["task_id"])
                self.issue["labels"] = []
        self.assertEqual(1, len(self.posts))

    def test_foreign_task_and_result_are_not_assessed_as_success(self):
        self.launch()
        task = self.task("completed")
        task["creator"]["id"] = 999
        self.assertIsNone(self.prepare())
        self.assertIsNone(self.saved()["workItems"][0]["assignments"][0]["execution"]["task_state"])
        self.task("completed")
        self.logs = "CSWORKITEMBEGIN" + base64.b64encode(b'{"schema_version":1}').decode() + "CSWORKITEMEND"
        packet = self.prepare()
        self.assertIsNone(packet["context"]["result"])
        self.assertEqual("unavailable", packet["context"]["followup"]["status"])

    def test_unmappable_draft_does_not_discard_exact_result(self):
        self.terminal()
        self.remote.values["agents/repos/microsoft/aspire/tasks/TASK1"]["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": 1234}},
            {"provider": "github", "type": "branch", "data": {
                "base_ref": "main", "head_ref": "copilot/diagnose"}}]
        packet = self.prepare()
        self.assertEqual("no_fix", packet["context"]["result"]["outcome"])
        self.assertIsNone(packet["context"]["mapping"]["pr"])
        self.assertTrue(packet["context"]["mapping"]["reason"].startswith("PR mapping denied"))

    def test_item_native_unknown_holds_survive_age_and_bound_worker_allowance(self):
        self.launch()
        saved = self.saved()
        receipt = saved["normalRuns"][0]
        receipt.update(nativeActual=None, nativeReserved=30, at="2026-09-01T00:00:00Z")
        self.assertGreaterEqual(state.repository_spend(saved, self.clock()), 30)
        import normal_loop
        self.assertEqual(30 + saved["workItems"][0]["assignments"][0]["execution"]["worker_reserved"],
                         normal_loop.item_spend(saved, saved["workItems"][0]["id"]))

    def test_denied_cloud_approval_and_transport_capability_never_post(self):
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        approval = receipt["approval_template"] | {"allow_draft_pr": False}
        os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = json.dumps(approval)
        with self.assertRaisesRegex(ValueError, "approval"):
            self.prepare()
        transport = pilot_github.PilotTransport("gho_fixture", write=True, binding=bindings.UPSTREAM_ALL,
                                                tracker=99, authority=500)
        with self.assertRaisesRegex(ValueError, "capability"):
            transport.validate_endpoint("POST", "agents/repos/microsoft/aspire/tasks", receipt["request"])
        self.assertEqual([], self.posts)

    def test_fresh_exact_approval_cannot_send_after_source_or_occurrence_changes(self):
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = json.dumps(receipt["approval_template"])
        self.remote.values[self.prefix + "/actions/jobs/200"]["head_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "occurrence"):
            self.prepare()
        self.assertEqual([], self.posts)

    def test_missing_results_consume_at_most_three_log_attempts(self):
        self.launch()
        self.task("completed")
        for _ in range(3):
            packet = self.prepare()
            self.assertEqual("unavailable", packet["context"]["followup"]["status"])
            self.settle(packet, self.assessment(packet))
        self.assertIsNone(self.prepare())
        self.assertEqual(3, self.saved()["workItems"][0]["assignments"][0]["execution"]["followup"]["attempts"])
        self.assertEqual(1, len(self.posts))

    def test_interrupted_intake_preparation_recovers_without_new_native_decision(self):
        packet = self.prepare(FEED)
        self.settle(packet, self.intake(packet))
        saved = self.saved()
        saved["workItems"][0]["assignments"] = []
        self.remote.comments[0]["body"] = state.render(saved)
        self.prepare()
        self.assertEqual(1, len(self.saved()["workItems"][0]["assignments"]))
        self.assertEqual(1, len(self.saved()["normalRuns"]))
        self.assertEqual([], self.posts)

    def test_local_report_handles_normal_packet_and_counts_native_and_worker_spend(self):
        self.launch()
        saved = self.saved()
        packet = self.prepare(FEED)
        self.assertIsNone(packet)
        intake = self.saved()["normalRuns"][0]
        descriptor = {"normal": "issue-intake", "context": intake["context"]}
        rendered = run_report.render(RUN, "upstream", state.new_ledger("microsoft/aspire"), saved,
                                     {"outcome": "approval-required"}, descriptor,
                                     "2026-10-04T00:00:00Z", "2026-10-04T00:00:00Z")
        self.assertIn("Normal decision:", rendered)
        actual, held, unknown = run_report.accounting(saved)
        self.assertEqual(2, actual)
        self.assertGreater(held, 0)
        self.assertTrue(unknown)

    def test_unimported_guard_prevents_normal_result_assessment_after_transfer(self):
        self.terminal()
        packet = self.prepare()
        saved = self.saved()
        worker = saved["workItems"][0]["assignments"][0]["execution"]
        worker["pr"] = {"repository": "microsoft/aspire", "id": 1007, "issue_id": 5007,
                        "number": 7, "node_id": "NODE7", "base": "main",
                        "head": "copilot/diagnose", "sha": "a" * 40}
        chain = state.adopt(saved, 7, "pr", "NODE7")
        worker["imported_chain_id"] = chain["id"]
        chain["sourceWorkItem"] = {"item_id": saved["workItems"][0]["id"],
                                  "assignment_id": saved["workItems"][0]["assignments"][0]["id"]}
        chain["handoff"] = {"id": chain["id"], "phase": "handoff_needed", "responsible": "radical",
                           "head": "a" * 40, "progressAt": "2026-10-04T00:00:00Z", "confirmedAt": None,
                           "sendState": "idle", "taskId": None, "attention": None, "mergeLabel": "none"}
        self.remote.comments[0]["body"] = state.render(saved)
        self.assertEqual("rejected", self.settle(packet, self.assessment(packet))["outcome"])
        self.assertIsNone(self.saved()["workItems"][0]["assignments"][0]["execution"]["followup"]["assessment"])

    def test_assessment_native_session_is_persisted_and_cannot_be_reused(self):
        self.terminal()
        packet = self.prepare()
        evidence = reconciliation_evidence(self.assessment(packet))
        self.assertEqual("assessed", pilot.settle(pilot.hosted_api(RUN, os.environ, billing=True),
                         packet, evidence, 2, self.clock())["outcome"])
        self.assertEqual(evidence["sessionId"], self.saved()["normalRuns"][-1]["sessionId"])
        self.remote.comments[0]["body"] = state.render(self.saved())
        # A second, distinct missing-result attempt cannot reuse that native session.
        saved = self.saved()
        assignment = saved["workItems"][0]["assignments"][0]
        assignment["result"] = None
        assignment["execution"]["followup"].update(status="unavailable", assessment=None)
        self.remote.comments[0]["body"] = state.render(saved)
        self.logs = None
        packet = self.prepare()
        reused = reconciliation_evidence(self.assessment(packet))
        reused["sessionId"] = evidence["sessionId"]
        # Regenerate host events for the same prohibited identity.
        from helpers import host_events
        decision = self.assessment(packet)
        call = {"toolCallId": "decision-call", "toolName": "safeoutputs-submit_decision",
                "arguments": {"decision": json.dumps(decision)}}
        reused["events"] = host_events(evidence["sessionId"], decision, [call])
        self.assertEqual("rejected", pilot.settle(pilot.hosted_api(RUN, os.environ, billing=True),
                         packet, reused, 2, self.clock())["outcome"])

    def test_pre_send_hold_interruption_reuses_reserved_budget_on_fresh_run(self):
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = json.dumps(receipt["approval_template"])
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        original = api.persist
        def interrupted():
            original()
            worker = api.ledger["workItems"][0]["assignments"][0]["execution"]
            if worker["state"] == "reserved" and worker["worker_reserved"] > 0:
                raise KeyboardInterrupt()
        import normal_loop
        with patch.object(api, "persist", interrupted), self.assertRaises(KeyboardInterrupt):
            normal_loop.dispatch_reserved(api, api.ledger["workItems"][0]["id"])
        self.assertEqual([], self.posts)
        self.assertEqual("reserved", self.saved()["workItems"][0]["assignments"][0]["execution"]["state"])
        self.prepare()
        self.assertEqual(1, len(self.posts))

    def test_full_valid_large_result_reaches_bounded_normal_assessment(self):
        self.terminal()
        assignment = self.saved()["workItems"][0]["assignments"][0]
        claimed = result(assignment) | {"item_id": self.saved()["workItems"][0]["id"],
            "worker_id": SESSION, "outcome": "no_fix", "changed_files": [], "pr_proposal": None,
            "summary": "x" * 1400, "evidence": ["e" * 500] * 18}
        self.logs = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"
        packet = self.prepare()
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertLessEqual(len(json.dumps(packet["context"]).encode()), 12000)
        self.assertTrue(packet["context"]["excerpted"])
        self.assertEqual(18, packet["context"]["resultCounts"]["evidence"])
        self.assertEqual(claimed, self.saved()["workItems"][0]["assignments"][0]["result"])
        self.assertEqual("assessed", self.settle(packet, self.assessment(packet))["outcome"])

    def test_changed_omitted_result_claim_rejects_bounded_assessment(self):
        self.terminal()
        assignment = self.saved()["workItems"][0]["assignments"][0]
        claimed = result(assignment) | {"item_id": self.saved()["workItems"][0]["id"],
            "worker_id": SESSION, "outcome": "no_fix", "changed_files": [], "pr_proposal": None,
            "summary": "x" * 1400, "evidence": ["e" * 500] * 18}
        self.logs = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"
        packet = self.prepare()
        saved = self.saved()
        saved["workItems"][0]["assignments"][0]["result"]["evidence"][-1] = "changed omitted claim"
        self.remote.comments[0]["body"] = state.render(saved)
        receipt = self.settle(packet, self.assessment(packet))
        self.assertEqual("rejected", receipt["outcome"])
        self.assertEqual("assessment result version changed", receipt["reason"])

    def test_malformed_pr_mapping_denies_import_but_keeps_normal_result_assessment(self):
        self.terminal()
        task = self.remote.values["agents/repos/microsoft/aspire/tasks/TASK1"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": 1007}},
            {"provider": "github", "type": "branch",
             "data": {"base_ref": "main", "head_ref": "copilot/diagnose"}}]
        self.remote.values[self.prefix + "/pulls"] = [{"id": 1007, "number": 7}]
        self.remote.values[self.prefix + "/pulls/7"] = {"id": 1007, "number": 7, "node_id": "NODE7"}
        packet = self.prepare()
        self.assertEqual("no_fix", packet["context"]["result"]["outcome"])
        self.assertTrue(packet["context"]["mapping"]["reason"].startswith("PR mapping denied"))

    def test_separate_fresh_processes_use_only_serialized_authority_for_run_b(self):
        canonical = self.root / "canonical.json"
        process = Path(__file__).with_name("normal_fixture_process.py")
        def run(command):
            completed = subprocess.run([sys.executable, "-B", str(process), str(canonical), command],
                env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(0, completed.returncode, completed.stderr)
            return json.loads(completed.stdout)
        self.assertEqual("issue-intake", run("prepare-feed")["normal"])
        self.assertEqual("approval-required", run("settle-intake")["outcome"])
        run("approve")
        run("prepare")
        run("terminal")
        self.assertEqual("issue-assessment", run("prepare")["normal"])
        self.assertEqual("assessed", run("settle-assessment")["outcome"])
        saved = json.loads(canonical.read_text())
        self.assertEqual(1, len(saved["posts"]))
        self.assertEqual("TASK1", state.parse(saved["authority"])["workItems"][0]["assignments"][0]
                         ["execution"]["task_id"])


class HostedCollectorTests(unittest.TestCase):
    def test_real_gh_reads_private_config_without_keyring_or_environment_override(self):
        original = result_collector.bounded_command
        paths = []
        def command(argv, environment, **kwargs):
            self.assertNotIn("GH_TOKEN", environment)
            self.assertNotIn("GITHUB_TOKEN", environment)
            config = Path(environment["GH_CONFIG_DIR"]) / "hosts.yml"
            self.assertEqual(str(config.parent), environment["HOME"])
            self.assertEqual(str(config.parent / "state"), environment["XDG_STATE_HOME"])
            self.assertEqual(0o600, config.stat().st_mode & 0o777)
            paths.append(config)
            if argv[:2] == ["gh", "api"]:
                return 0, '{"id":1472,"login":"radical"}', ""
            if argv[:3] == ["gh", "agent-task", "view"]:
                return 0, "exact-session output", ""
            return original(argv, environment, **kwargs)
        collector = result_collector.HostedCollector("gho_fixture", command=command,
            environment={"PATH": os.environ["PATH"], "GH_TOKEN": "wrong", "GITHUB_TOKEN": "wrong"})
        self.assertEqual("exact-session output", collector("microsoft/aspire", SESSION))
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_hosted_reader_denies_non_oauth_identity_renderer_and_wrong_user(self):
        def command(argv, environment, **kwargs):
            if argv == ["gh", "--version"]:
                return 0, "gh version 2.102.0 (fixture)", ""
            if argv[:2] == ["gh", "auth"]:
                return 0, "gho_fixture", ""
            return 0, '{"id":999,"login":"foreign"}', ""
        collector = result_collector.HostedCollector("gho_fixture", command=command)
        self.assertFalse(collector.capable)
        self.assertFalse(result_collector.HostedCollector("github_pat_fixture", command=command).capable)
        collector.command = lambda *args, **kwargs: (0, "gh version 2.103.0 (fixture)", "")
        self.assertFalse(collector.capable)


if __name__ == "__main__":
    unittest.main()
