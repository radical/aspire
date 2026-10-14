"""Normal host entrypoints with fresh clients and serialized remote authority."""

from copy import deepcopy
from datetime import timedelta
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from github import LostResponse, Response
from helpers import FakeClock, WorkspaceTest, reconciliation_evidence
import hosted
import live
import normal_loop
import pilot
import pilot_binding as bindings
import pilot_state as state
import result_collector
import round as contracts
import work_item_execution as execution
import work_item_followup as followup
import pilot_github
import run_report
import work_item_receiver as receiver
from test_pilot_github import Transport, pr
from test_work_item_receiver import Authority
from test_work_items import control, result


RUN = {"repository": "radical/aspire", "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40}
FEED = {"reported_kind": "workflow_failure",
        "issue_url": "https://github.com/microsoft/aspire/issues/20872"}
SESSION = "12345678-1234-4234-8234-123456789abc"


class NormalLoopTests(unittest.TestCase):
    def setUp(self):
        checkout = Path(__file__).resolve().parents[4]
        inherited = os.environ.get("TMPDIR")
        if inherited is None:
            scratch = checkout / "artifacts/ci-shepherd/tests" / str(uuid.uuid4())
            scratch.mkdir(parents=True)
            self.addCleanup(shutil.rmtree, scratch)
            self.tmpdir = str(scratch)
        else:
            self.tmpdir = inherited
        self.validate_tmpdir()
        environment = patch.dict(os.environ, {"TMPDIR": self.tmpdir})
        environment.start()
        self.addCleanup(environment.stop)
        # tempfile caches its default path. Route the real collector explicitly,
        # without resetting process-global state or changing production allocation.
        collector_tempfile = patch.object(result_collector, "tempfile", SimpleNamespace(
            TemporaryDirectory=lambda **kwargs: tempfile.TemporaryDirectory(dir=self.tmpdir, **kwargs)))
        collector_tempfile.start()
        self.addCleanup(collector_tempfile.stop)
        self.directory = tempfile.TemporaryDirectory(dir=self.tmpdir)
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
                                       for n in range(100, 111))}
        self.remote.values.update({self.prefix: dict(self.repo, default_branch="main"),
                                   self.prefix + "/issues/20872": self.issue})
        for n in range(100, 111):
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
            "TMPDIR": self.tmpdir,
            "CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_USER_TOKEN": "gho_fixture",
            "SHEPHERD_TARGET": "upstream", "CI_SHEPHERD_UPSTREAM_TRACKER": "99",
            "CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT": "500",
            "CI_SHEPHERD_UPSTREAM_TRACKER_NODE": "TRACKER99", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTIONS": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fixture-oidc",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.actions.githubusercontent.com/token",
        }
        self.posts = []
        self.issue_posts = []
        self.issue_patches = []
        self.patch_lost = False
        self.lost = False
        self.logs = None

        def http(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            if method == "POST" and endpoint == "agents/repos/microsoft/aspire/tasks":
                self.posts.append((deepcopy(body), self.saved()))
                if self.lost:
                    raise LostResponse("lost task receipt")
                return Response({"id": "TASK1"}, {}, 201)
            if method == "POST" and endpoint == self.prefix + "/issues/20872/comments":
                self.issue_posts.append((deepcopy(body), self.saved()))
                comment = {"id": 8000, "user": {"id": 1472, "login": "radical"}, "body": body["body"]}
                self.remote.values.setdefault(endpoint, []).append(comment)
                return Response(comment, {}, 201)
            if method == "PATCH" and endpoint == self.prefix + "/issues/comments/8000":
                self.issue_patches.append((deepcopy(body), self.saved()))
                comment = self.remote.values[self.prefix + "/issues/20872/comments"][0]
                comment["body"] = body["body"]
                if self.patch_lost:
                    raise LostResponse("lost replacement receipt")
                return Response(deepcopy(comment), {}, 200)
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

    def validate_tmpdir(self):
        path = Path(self.tmpdir)
        checkout = Path(__file__).resolve().parents[4]
        if (not path.is_absolute() or not path.is_dir()
                or path.resolve() == checkout or checkout not in path.resolve().parents):
            raise ValueError("TMPDIR must be an existing directory strictly beneath the checkout")

    def saved(self):
        return state.parse(self.remote.comments[0]["body"])

    def process(self, canonical, command):
        self.validate_tmpdir()
        completed = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).with_name("normal_fixture_process.py")),
             str(canonical), command],
            env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                 "TMPDIR": self.tmpdir}, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        journal = json.loads(canonical.read_text())
        self.assertEqual(self.tmpdir, journal["scratch"]["tmpdir"])
        self.assertIn(Path(self.tmpdir).resolve(), Path(journal["scratch"]["root"]).parents)
        return json.loads(completed.stdout)

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
                "evidenceId": packet["context"]["candidates"][0][0] if action == "investigate" else None,
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

    def policy(self, **permissions):
        value = {"schema_version": 1, "issue": {"repository": "microsoft/aspire",
                 "number": 20872, "node_id": "ISSUE20872"}, "allow_cloud_task": True,
                 "allow_branch_writes": True, "allow_draft_pr": True, "track_linked_pr": True,
                 "issue_comment": True} | permissions
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(value)
        return value

    def add_pr(self, changes):
        task = self.remote.values["agents/repos/microsoft/aspire/tasks/TASK1"]
        pull = pr(7)
        for key in ("base", "head"):
            pull[key]["repo"] = self.repo
        pull.update(draft=True, labels=[], issue_url=f"https://api.github.com/{self.prefix}/issues/7",
                    body="Refs microsoft/aspire#20872", merged=False, merged_at=None, commits=1,
                    changed_files=changes)
        issue = {"id": 5007, "node_id": pull["node_id"], "number": 7,
                 "repository_url": f"https://api.github.com/{self.prefix}",
                 "pull_request": {"url": f"https://api.github.com/{self.prefix}/pulls/7"}}
        task["sessions"][0]["head_ref"] = pull["head"]["ref"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": pull["id"], "global_id": pull["node_id"]}},
            {"provider": "github", "type": "branch",
             "data": {"base_ref": "main", "head_ref": pull["head"]["ref"]}},
        ]
        self.remote.values.update({
            f"{self.prefix}/pulls": [pull],
            f"{self.prefix}/pulls/7": pull,
            f"{self.prefix}/issues/7": issue,
            f"{self.prefix}/pulls/7/commits": [
                {"sha": pull["head"]["sha"], "commit": {"message": "Refs microsoft/aspire#20872"}}],
            f"{self.prefix}/git/ref/heads/{pull['head']['ref']}": {
                "ref": "refs/heads/" + pull["head"]["ref"], "object": {"sha": pull["head"]["sha"]}},
            f"{self.prefix}/issues/20872/timeline": [
                {"event": "cross-referenced", "source": {"type": "issue", "issue": issue}}],
        })
        return pull

    def external_candidate(self):
        pull = pr(8)
        pull["base"].update(ref="main", repo=self.repo)
        pull.update(state="open", merged=False, body="Fixes #20872")
        pull["head"]["repo"] = {"full_name": "another/aspire"}
        self.remote.values[f"{self.prefix}/pulls/8"] = pull
        self.remote.values[f"{self.prefix}/pulls/8/closing-issues"] = [
            {"number": 20872, "repository": {"nameWithOwner": "microsoft/aspire"}}]
        self.remote.values.setdefault(f"{self.prefix}/issues/20872/timeline", []).append(
            {"event": "cross-referenced", "source": {"issue": {
                "number": 8, "node_id": pull["node_id"],
                "url": f"https://api.github.com/{self.prefix}/issues/8", "pull_request": {}}}})
        return pull

    def test_existing_candidate_suppresses_launch_and_reports_once_across_fresh_clients(self):
        self.policy()
        self.external_candidate()
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        self.assertEqual("deferred", receipt["outcome"])
        self.assertEqual([], self.posts)
        self.prepare()
        self.prepare()
        self.assertEqual([], self.posts)
        self.assertEqual(1, len(self.issue_posts))
        self.assertIn("/pull/8", self.issue_posts[0][0]["body"])
        self.assertEqual([], self.saved()["chains"])
        self.assertEqual(0, state.worker_slots(self.saved()))

    def test_late_candidate_tracks_both_without_cancelling_or_adopting_external_pr(self):
        self.launch()
        self.policy()
        self.add_pr(0)
        self.external_candidate()
        self.prepare()
        self.prepare()
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))
        record = self.saved()["workItems"][0]
        self.assertEqual([(7, True), (8, False)],
                         [(pull["number"], pull["own"]) for pull in record["candidate_fixes"]["pulls"]])
        self.assertEqual("sent", record["assignments"][0]["execution"]["state"])
        self.assertEqual("in_progress", record["assignments"][0]["execution"]["task_state"])
        self.assertEqual([], self.saved()["chains"])
        self.assertEqual([], [write for write in self.remote.writes if write[1].endswith("/labels")])
        self.assertIn("/pull/7", self.issue_posts[0][0]["body"])
        self.assertIn("/pull/8", self.issue_posts[0][0]["body"])

    def test_incomplete_candidate_inventory_does_not_stop_existing_task_observation(self):
        self.launch()
        self.external_candidate()
        self.remote.paged_responses[f"{self.prefix}/issues/20872/timeline?per_page=100&page=1"] = Response(
            [], {"Link": "<https://untrusted.example/timeline?page=2>; rel=\"next\""})
        self.prepare()
        record = self.saved()["workItems"][0]
        self.assertFalse(record["candidate_fixes"]["complete"])
        self.assertEqual("in_progress", record["assignments"][0]["execution"]["task_state"])
        self.assertEqual(1, len(self.posts))

    def test_external_candidate_is_observed_after_our_pr_is_imported(self):
        self.launch()
        self.task("completed")
        self.add_pr(1)
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        normal_loop.prepare(api, RUN, self.clock())
        self.assertEqual(1, len(self.saved()["chains"]))
        imported = deepcopy(self.saved()["chains"])
        self.policy()
        self.external_candidate()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        normal_loop.prepare(api, RUN, self.clock())
        record = self.saved()["workItems"][0]
        self.assertEqual([(7, True), (8, False)],
                         [(pull["number"], pull["own"]) for pull in record["candidate_fixes"]["pulls"]])
        self.assertEqual(imported, self.saved()["chains"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))

    def test_manual_cloud_item_is_observed_without_normal_intake_or_automatic_dispatch(self):
        self.launch()
        manual = self.saved()
        manual.pop("normalRuns")
        self.remote.comments[0]["body"] = state.render(manual)
        self.policy()
        self.add_pr(0)
        self.external_candidate()
        self.prepare()
        self.prepare()
        record = self.saved()["workItems"][0]
        self.assertEqual([(7, True), (8, False)],
                         [(pull["number"], pull["own"]) for pull in record["candidate_fixes"]["pulls"]])
        self.assertEqual("in_progress", record["assignments"][0]["execution"]["task_state"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual([], self.saved()["chains"])
        self.assertEqual(1, len(self.issue_posts))

    def test_sibling_notices_refresh_shared_source_before_terminal_followup(self):
        self.terminal()
        self.add_pr(0)["state"] = "closed"
        self.policy()
        self.external_candidate()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        retry = deepcopy(api.ledger["workItems"][0]["control"])
        retry["id"] = "manual-retry"
        execution.prepare_retry(api, lambda: retry, "TASK1")
        original = live.HTTPTransport.__call__

        def comments_update_issue(transport, method, endpoint, body):
            response = original(transport, method, endpoint, body)
            if method == "POST" and endpoint == self.prefix + "/issues/20872/comments":
                self.issue["updated_at"] = f"2026-10-05T00:00:{len(self.issue_posts):02d}Z"
            return response

        with patch.object(live.HTTPTransport, "__call__", comments_update_issue):
            packet = self.prepare()
            self.assertEqual("worklist", packet["normal"])
            self.assertEqual(1, len(packet["packets"]))
            child = packet["packets"][0]
            self.assertEqual("issue-assessment", child["normal"])
            self.assertEqual(self.issue["updated_at"], child["context"]["issue"]["updatedAt"])
            decision = {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                        "decisions": [self.assessment(child)]}
            receipt = self.settle(packet, decision)
            self.assertEqual("processed", receipt["outcome"])
            self.assertEqual(["assessed"], [item["outcome"] for item in receipt["items"]])
            self.prepare()
        notices = [body["body"] for body, _ in self.issue_posts if body["body"].startswith(
            "[automated] **CI Shepherd: tracking candidate fixes**")]
        self.assertEqual(1, len(notices))
        self.assertIn("/pull/7", notices[0])
        self.assertIn("/pull/8", notices[0])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(2, len(self.saved()["workItems"]))

    def test_hosted_import_and_candidate_rerun_have_fresh_chain_observations(self):
        self.launch()
        self.task("completed")
        self.add_pr(1)
        self.prepare()
        self.assertEqual(1, len(self.saved()["chains"]))
        imported = self.saved()["chains"][0]["id"]
        self.policy()
        self.external_candidate()
        self.prepare()
        self.prepare()
        saved = self.saved()
        self.assertEqual([imported], [chain["id"] for chain in saved["chains"]])
        self.assertEqual([(7, True), (8, False)],
                         [(pull["number"], pull["own"]) for pull in saved["workItems"][0]["candidate_fixes"]["pulls"]])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))

    def test_issue_policy_launches_during_intake_without_second_approval_turn(self):
        self.policy()
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        self.assertEqual("sent", receipt["outcome"])
        self.assertEqual(1, len(self.posts))
        record = self.saved()["workItems"][0]
        self.assertTrue(record["control"]["authority"]["issue_comment"])
        self.assertEqual("TASK1", record["assignments"][0]["execution"]["task_id"])

    def test_policy_for_different_issue_does_not_authorize_launch_or_comments(self):
        value = self.policy()
        value["issue"]["number"] = 20873
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(value)
        packet = self.prepare(FEED)
        receipt = self.settle(packet, self.intake(packet))
        self.assertEqual("approval-required", receipt["outcome"])
        self.assertEqual([], self.posts)
        self.assertFalse(self.saved()["workItems"][0]["control"]["authority"]["issue_comment"])

    def test_policy_rejects_nonboolean_permission_and_denies_incomplete_cloud_grant(self):
        self.policy(allow_cloud_task=False)
        packet = self.prepare(FEED)
        self.assertEqual("approval-required", self.settle(packet, self.intake(packet))["outcome"])
        self.policy(allow_cloud_task=1)
        with self.assertRaisesRegex(ValueError, "normal policy"):
            self.prepare()
        self.assertEqual([], self.posts)

    def test_failed_task_without_structured_result_posts_one_authorized_report(self):
        self.policy()
        packet = self.prepare(FEED)
        self.assertEqual("sent", self.settle(packet, self.intake(packet))["outcome"])
        self.task("failed")
        self.logs = "The task could not establish a fix."
        packet = self.prepare()
        self.assertIsNone(packet["context"]["result"])
        receipt = self.settle(packet, self.assessment(packet))
        self.assertEqual("commented", receipt["report"]["outcome"])
        self.assertEqual(1, len(self.issue_posts))
        body, authority = self.issue_posts[0]
        self.assertEqual("delivering", authority["workItems"][0]["comment"]["delivery"])
        self.assertIn("without a verified fix", body["body"])
        self.assertLessEqual(len(json.dumps(body["body"]).encode()), 4000)
        for _ in range(2):
            self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual(1, len(self.posts))
        self.assertEqual("open", self.issue["state"])

    def delivered_report(self):
        self.terminal()
        self.logs = "No structured result."
        self.policy()
        packet = self.prepare()
        self.settle(packet, self.assessment(packet))
        return pilot.hosted_api(RUN, os.environ)

    def test_explicit_report_refresh_replaces_owned_body_once_without_new_task_or_inference(self):
        api = self.delivered_report()
        record = self.saved()["workItems"][0]
        claimed = result(record["assignments"][0])
        claimed.update(item_id=record["id"], worker_id=SESSION, outcome="no_fix",
                       changed_files=[], pr_proposal=None, summary="A 500 ms budget expired before requests.")
        self.logs = ("print('CSWORKITEMBEGIN\\n'+b64+'\\nCSWORKITEMEND')\n"
                     + followup.BEGIN + base64.b64encode(json.dumps(claimed).encode()).decode()
                     + followup.END) * 2
        before = self.saved()
        receipt = normal_loop.refresh_report(api, 20872, 8000)
        self.assertEqual({"outcome": "comment_replaced", "comment_id": 8000}, receipt)
        body = self.issue_patches[0][0]["body"]
        self.assertIn("A 500 ms budget expired before requests.", body)
        self.assertIn("/tasks/TASK1", body)
        self.assertIn("/actions/runs/100/attempts/1/job/200", body)
        self.assertEqual(before["normalRuns"], self.saved()["normalRuns"])
        self.assertEqual(before["workItems"][0]["assignments"][0]["result"],
                         self.saved()["workItems"][0]["assignments"][0]["result"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual({"outcome": "unchanged", "comment_id": 8000},
                         normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, 8000))
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_patches))

    def test_report_refresh_preserves_human_edits_and_denies_wrong_identity_or_revoked_policy(self):
        self.delivered_report()
        for change in ("body", "owner", "identity", "policy"):
            with self.subTest(change=change):
                comments = deepcopy(self.remote.values[self.prefix + "/issues/20872/comments"])
                original_policy = os.environ["CI_SHEPHERD_NORMAL_POLICY"]
                identity = 8000
                if change == "body":
                    self.remote.values[self.prefix + "/issues/20872/comments"][0]["body"] += " Human edit."
                elif change == "owner":
                    self.remote.values[self.prefix + "/issues/20872/comments"][0]["user"]["id"] = 999
                elif change == "identity":
                    identity = 9000
                else:
                    self.policy(issue_comment=False)
                with self.assertRaises(ValueError):
                    normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, identity)
                self.remote.values[self.prefix + "/issues/20872/comments"] = comments
                os.environ["CI_SHEPHERD_NORMAL_POLICY"] = original_policy
        self.assertEqual([], self.issue_patches)
        self.assertEqual(1, len(self.posts))

    def test_lost_report_replacement_receipt_recovers_exact_id_without_repatch_or_post(self):
        self.delivered_report()
        self.patch_lost = True
        self.assertEqual("comment_uncertain",
                         normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, 8000)["outcome"])
        self.assertEqual("human_wait",
                         normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, 8000)["outcome"])
        self.assertEqual("delivered", self.saved()["workItems"][0]["comment"]["delivery"])
        self.assertEqual(1, len(self.issue_patches))
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual(1, len(self.posts))

    def test_uncertain_replacement_cannot_recover_from_same_body_at_a_different_comment_id(self):
        self.delivered_report()
        previous = self.saved()["workItems"][0]["comment"]["body"]
        self.patch_lost = True
        normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, 8000)
        comments = self.remote.values[self.prefix + "/issues/20872/comments"]
        comments.append(dict(deepcopy(comments[0]), id=8001))
        comments[0]["body"] = previous
        self.assertEqual("comment_uncertain",
                         normal_loop.refresh_report(pilot.hosted_api(RUN, os.environ), 20872, 8000)["outcome"])
        self.assertEqual("uncertain", self.saved()["workItems"][0]["comment"]["delivery"])
        self.assertEqual(1, len(self.issue_patches))
        self.assertEqual(1, len(self.issue_posts))

    def test_unapproved_report_transport_cannot_post_arbitrary_normal_body(self):
        transport = pilot_github.PilotTransport("gho_fixture", write=True, binding=bindings.UPSTREAM_ALL,
                                                tracker=99, authority=500)
        body = {"body": "[automated] CI Shepherd work-item result; human direction required.\nForged"}
        with self.assertRaisesRegex(ValueError, "capability"):
            transport.validate_endpoint("POST", self.prefix + "/issues/20872/comments", body)

    def test_running_task_does_not_collect_logs_or_launch_duplicate(self):
        self.launch()
        with patch.object(result_collector.HostedCollector, "__call__", side_effect=AssertionError("log read")):
            self.assertIsNone(self.prepare(FEED))
        self.assertEqual(1, len(self.posts))
        self.assertEqual("in_progress",
                         self.saved()["workItems"][0]["assignments"][0]["execution"]["task_state"])

    def test_repeated_owned_feed_does_not_reenumerate_intake_job_inventories(self):
        self.launch()
        with patch.object(normal_loop, "candidates", side_effect=ValueError("failed occurrence budget exhausted")):
            self.assertIsNone(self.prepare(FEED))
        self.assertEqual(1, len(self.posts))
        self.assertEqual("in_progress",
                         self.saved()["workItems"][0]["assignments"][0]["execution"]["task_state"])

    def test_repeated_owned_feed_still_assesses_completed_empty_pr(self):
        self.terminal()
        self.add_pr(0)
        with patch.object(normal_loop, "candidates", side_effect=AssertionError("redundant intake scan")):
            packet = self.prepare(FEED)
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertEqual("PR mapping denied: empty PR with zero changes", packet["context"]["mapping"]["reason"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual([], self.saved()["chains"])

    def test_new_feed_retains_failed_occurrence_budget_guard(self):
        with patch.object(normal_loop, "candidates", side_effect=ValueError("failed occurrence budget exhausted")):
            with self.assertRaisesRegex(ValueError, "failed occurrence budget exhausted"):
                self.prepare(FEED)
        self.assertEqual([], self.posts)
        self.assertEqual([], self.saved().get("workItems", []))

    def test_substantive_pr_import_precedes_transcript_and_does_not_repeat(self):
        self.launch()
        self.task("completed")
        self.add_pr(1)
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        with patch.object(result_collector.HostedCollector, "__call__", side_effect=AssertionError("log read")):
            self.assertIsNone(normal_loop.prepare(api, RUN, self.clock(), FEED))
        saved = self.saved()
        self.assertEqual(1, len(saved["chains"]))
        self.assertEqual("handoff_needed", saved["chains"][0]["handoff"]["phase"])
        self.assertIsNone(saved["workItems"][0]["assignments"][0]["result"])
        self.assertEqual(saved["chains"][0]["id"],
                         saved["workItems"][0]["assignments"][0]["execution"]["imported_chain_id"])
        self.assertIsNone(normal_loop.prepare(api, RUN, self.clock(), FEED))
        self.assertEqual(1, len(self.saved()["chains"]))

    def test_empty_pr_is_inspected_and_assessed_without_import(self):
        self.terminal()
        self.add_pr(0)
        packet = self.prepare()
        self.assertEqual("issue-assessment", packet["normal"])
        self.assertEqual("no_fix", packet["context"]["result"]["outcome"])
        self.assertEqual("PR mapping denied: empty PR with zero changes", packet["context"]["mapping"]["reason"])
        self.assertEqual([], self.saved()["chains"])

    def test_changed_empty_pr_description_rejects_stale_assessment(self):
        self.terminal()
        pull = self.add_pr(0)
        packet = self.prepare()
        pull["body"] = "Changed diagnosis."
        receipt = self.settle(packet, self.assessment(packet))
        self.assertEqual("rejected", receipt["outcome"])
        self.assertEqual("assessment PR evidence changed", receipt["reason"])
        self.assertIsNone(self.saved()["workItems"][0]["comment"])

    def test_missing_json_session_excerpts_keep_ending_and_complete_local_audit(self):
        self.launch()
        self.task("completed")
        self.logs = "setup\n" + "unrelated tool output\n" * 200 + "\nNo fix was established."
        audit = []
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        api.normal_log_audit = lambda session, text: audit.append((session, text))
        packet = normal_loop.prepare(api, RUN, self.clock())
        self.assertIsNone(packet["context"]["result"])
        self.assertIn("No fix was established.", packet["context"]["followup"]["log_excerpt"])
        self.assertEqual([(SESSION, self.logs)], audit)

    def test_settled_assessment_is_not_reused_after_empty_pr_description_changes(self):
        self.terminal()
        pull = self.add_pr(0)
        packet = self.prepare()
        self.settle(packet, self.assessment(packet))
        pull["body"] = "Updated explanation from the worker."
        updated = self.prepare()
        self.assertEqual("issue-assessment", updated["normal"])
        self.assertNotEqual(packet["operation"], updated["operation"])
        self.assertEqual(2, sum(entry["phase"] == "issue-assessment"
                                for entry in self.saved()["normalRuns"]))

    def test_receipt_digest_prevents_changed_packet_and_duplicate_inference(self):
        packet = self.prepare(FEED)
        saved = self.saved()["normalRuns"][0]
        self.assertEqual(normal_loop.digest(packet["context"]), saved["context"]["contextDigest"])
        self.assertLess(len(json.dumps(saved["context"]).encode()), 500)
        changed = deepcopy(packet)
        changed["context"]["issue"]["title"] = "tampered"
        with self.assertRaisesRegex(ValueError, "canonical reservation"):
            self.settle(changed, self.intake(changed))
        self.assertIsNone(self.prepare(FEED))
        self.assertEqual(1, len(self.saved()["normalRuns"]))

    def test_small_remaining_authority_capacity_accepts_assessment_and_preview(self):
        self.terminal()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        followup.collect(api, normal_loop.read_control(api, api.ledger["workItems"][0]["id"]),
                         api.normal_collector)
        with patch.object(state, "MAX_BODY", len(state.render(api.ledger).encode()) + 12000):
            packet = normal_loop.prepare(api, RUN, self.clock())
            self.assertEqual("issue-assessment", packet["normal"])
            receipt = self.settle(packet, self.assessment(packet))
            self.assertEqual("assessed", receipt["outcome"])
            self.assertEqual("human_wait", receipt["report"]["outcome"])
            self.assertLessEqual(len(self.remote.comments[0]["body"].encode()), state.MAX_BODY)

    def test_oversized_assessment_rejected_before_publication(self):
        self.terminal()
        packet = self.prepare()
        decision = self.assessment(packet)
        decision["assessment"]["evidence"] = ["x" * 1000] * 3
        receipt = self.settle(packet, decision)
        self.assertEqual("rejected", receipt["outcome"])
        self.assertEqual("normal assessment exceeds storage bound", receipt["reason"])
        self.assertIsNone(self.saved()["workItems"][0]["comment"])

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

    def test_more_than_ten_references_are_primary_candidates_and_extra_feed_fields_are_denied(self):
        packet = self.prepare(FEED)
        self.assertLessEqual(len(json.dumps(packet["context"]).encode()), normal_loop.MAX_INTAKE_CONTEXT)
        self.assertEqual(list(range(100, 111)),
                         [int(value[0].split(":", 1)[0]) for value in packet["context"]["candidates"]])
        with self.assertRaises(ValueError):
            self.prepare(FEED | {"requested_route": "workflow_failure"})
        with self.assertRaises(ValueError):
            self.prepare(FEED | {"issue_url": "https://github.com/radical/aspire/issues/20872"})

    def test_persisted_manual_items_are_status_only_during_fork_intake(self):
        os.environ.update(SHEPHERD_TARGET="fork", CI_SHEPHERD_TRACKER="99",
                          CI_SHEPHERD_AUTHORITY_COMMENT="500", CI_SHEPHERD_TRACKER_NODE="TRACKER99")
        self.prefix = "repos/radical/aspire"
        self.repo = {"id": bindings.FORK.repository_id, "full_name": "radical/aspire"}
        self.remote.values[self.prefix] = dict(self.repo, default_branch="main")
        self.remote.values[self.prefix + "/issues/20872"] = dict(
            self.issue, repository_url="https://api.github.com/" + self.prefix,
            body="https://github.com/radical/aspire/actions/runs/100")
        run = deepcopy(self.remote.values["repos/microsoft/aspire/actions/runs/100"])
        run["repository"] = self.repo
        self.remote.values[self.prefix + "/actions/runs/100"] = run
        self.remote.values[self.prefix + "/actions/runs/100/attempts/1/jobs"] = deepcopy(
            self.remote.values["repos/microsoft/aspire/actions/runs/100/attempts/1/jobs"])
        feed = {"reported_kind": "workflow_failure",
                "issue_url": "https://github.com/radical/aspire/issues/20872"}
        for repository in ("microsoft/aspire", "radical/aspire"):
            with self.subTest(repository=repository):
                manual = control()
                manual["issue"]["repository"] = repository
                manual["occurrence"]["repository"] = repository
                authority = Authority()
                receiver.receive(authority, lambda: deepcopy(manual), lambda packet: None)
                before = deepcopy(authority.saved["workItems"])
                self.remote.comments[0]["body"] = state.render(authority.saved)
                self.remote.reads.clear()
                packet = self.prepare(feed)
                self.assertEqual("worklist", packet["normal"])
                self.assertEqual("issue-intake", packet["packets"][0]["normal"])
                row = packet["worklist"][0]
                self.assertEqual(manual["issue"], row["issue"])
                self.assertEqual("delivered", row["state"])
                self.assertEqual(
                    "Issue is outside the authority namespace." if repository != "radical/aspire"
                    else "No settled normal intake receipt.",
                    row["statusOnly"])
                issue_reads = [endpoint.split("?")[0] for method, endpoint, _ in self.remote.reads
                               if method == "GET" and re.fullmatch(r"repos/[^/]+/[^/]+/issues/900", endpoint)]
                self.assertEqual([], issue_reads)
                self.assertEqual(before, self.saved()["workItems"])

    def test_owned_normal_item_source_failure_still_aborts_prepare(self):
        packet = self.prepare(FEED)
        self.assertEqual("approval-required", self.settle(packet, self.intake(packet))["outcome"])
        before = self.saved()
        self.remote.values[self.prefix + "/issues/20872"]["repository_url"] = (
            "https://api.github.com/repos/radical/aspire")
        with self.assertRaisesRegex(ValueError, "exact source issue identity unavailable"):
            self.prepare()
        self.assertEqual(before, self.saved())
        self.assertEqual([], self.posts)

    def test_large_intake_context_keeps_full_source_and_occurrence_digests(self):
        observed = normal_loop.source(SimpleNamespace(
            api=live.API(self.remote, repository_id=bindings.UPSTREAM.repository_id),
            prefix=self.prefix, repository="microsoft/aspire", token=self.env["CI_SHEPHERD_USER_TOKEN"]),
            20872)
        observed["body"] = "Detailed issue description. " * 100
        candidates = []
        for index in range(58):
            run_id = 38000000000 + index // 7
            job_id = 2000000000 + index
            attempt = 1
            candidates.append({
                "id": f"{run_id}:{attempt}:{job_id}",
                "occurrence": {"repository": "microsoft/aspire", "run_id": run_id,
                               "run_attempt": attempt, "head_sha": "a" * 40,
                               "job_id": job_id, "artifact_id": None},
                "job": "Run workflow integration tests - Ubuntu x64 Release",
                "conclusion": "failure",
                "failedSteps": ["Run end-to-end test suite for the app host"]})

        context = normal_loop.intake_context(SimpleNamespace(token=self.env["CI_SHEPHERD_USER_TOKEN"]),
                                             FEED, observed, candidates)

        self.assertEqual(normal_loop.digest(observed), context["issueDigest"])
        self.assertEqual(normal_loop.digest(candidates), context["candidateDigest"])
        self.assertLessEqual(len(context["issue"]["body"]), normal_loop.MAX_INTAKE_ISSUE_BODY)
        self.assertTrue(all(len(candidate) == 4 for candidate in context["candidates"]))
        self.assertLessEqual(len(json.dumps(context).encode()), normal_loop.MAX_CONTEXT)

    def test_more_than_one_hundred_run_references_exceed_the_budget(self):
        self.issue["body"] += "\n" + "\n".join(
            f"https://github.com/microsoft/aspire/actions/runs/{n}"
            for n in range(1, normal_loop.MAX_RUN_REFERENCES - 10 + 1))
        with self.assertRaisesRegex(ValueError, "issue run reference budget exhausted"):
            normal_loop.source(SimpleNamespace(
                api=live.API(self.remote, repository_id=bindings.UPSTREAM.repository_id),
                prefix=self.prefix, repository="microsoft/aspire", token=self.env["CI_SHEPHERD_USER_TOKEN"]),
                20872)

    def test_paginated_job_inventory_finds_failed_occurrence_on_later_page(self):
        path = self.prefix + "/actions/runs/100/attempts/1/jobs"
        self.issue["body"] = "https://github.com/microsoft/aspire/actions/runs/100"
        successful_jobs = [
            {"id": 1000 + n, "run_id": 100, "run_attempt": 1, "head_sha": "a" * 40,
             "status": "completed", "conclusion": "success", "name": f"Successful job {n}", "steps": []}
            for n in range(100)
        ]
        failed_jobs = [
            {"id": 200 + n, "run_id": 100, "run_attempt": 1, "head_sha": "a" * 40,
             "status": "completed", "conclusion": "failure", "name": f"Failed job {n}", "steps": []}
            for n in range(11)
        ]
        self.remote.paged_responses[path + "?per_page=100&page=1"] = Response(
            {"total_count": 111, "jobs": successful_jobs},
            {"Link": f'<https://api.github.com/{path}?per_page=100&page=2>; rel="next"'})
        self.remote.paged_responses[path + "?per_page=100&page=2"] = Response(
            {"total_count": 111, "jobs": failed_jobs}, {})

        packet = self.prepare(FEED)

        self.assertEqual([f"100:1:{200 + n}" for n in range(11)],
                         [value[0] for value in packet["context"]["candidates"]])

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

    def test_missing_results_are_assessed_once_and_repeated_runs_keep_preview(self):
        self.launch()
        self.task("completed")
        packet = self.prepare()
        self.assertEqual("unavailable", packet["context"]["followup"]["status"])
        self.assertEqual("assessed", self.settle(packet, self.assessment(packet))["outcome"])
        for _ in range(3):
            self.assertIsNone(self.prepare())
        record = self.saved()["workItems"][0]
        self.assertEqual(1, record["assignments"][0]["execution"]["followup"]["attempts"])
        self.assertEqual("reserved", record["comment"]["delivery"])
        self.assertEqual(2, len(self.saved()["normalRuns"]))
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
        self.assertLessEqual(len(json.dumps(packet["context"]).encode()), normal_loop.MAX_CONTEXT)
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
            self.validate_tmpdir()
            completed = subprocess.run([sys.executable, "-B", str(process), str(canonical), command],
                env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                     "TMPDIR": self.tmpdir},
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

    def test_restart_edited_issue_late_report_grant_preserves_original_assignment(self):
        self.terminal()
        original = self.saved()["workItems"][0]
        pull = self.add_pr(0)
        pull["state"] = "closed"
        del self.remote.values[f"{self.prefix}/git/ref/heads/{pull['head']['ref']}"]
        self.issue["body"] += "\nAnother occurrence was reported."
        self.issue["updated_at"] = "2026-10-05T00:00:00Z"
        self.policy(allow_cloud_task=False, allow_branch_writes=False, allow_draft_pr=False,
                    track_linked_pr=False)
        packet = self.prepare()
        self.assertIsNotNone(packet, "edited source must not hide an existing terminal task")
        self.assertEqual("closed", packet["context"]["mapping"]["observation"]["state"])
        self.assertEqual(0, packet["context"]["mapping"]["observation"]["changedFiles"])
        self.assertTrue(packet["context"]["sourceChangedSinceDispatch"])
        self.assertEqual("commented", self.settle(packet, self.assessment(packet))["report"]["outcome"])
        self.assertIsNone(self.prepare())
        self.assertIsNone(self.prepare())
        after = self.saved()["workItems"][0]
        self.assertEqual(original["control"], after["control"])
        self.assertEqual(original["assignments"][0]["basis"], after["assignments"][0]["basis"])
        self.assertEqual(original["assignments"][0]["id"], after["assignments"][0]["id"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual([], self.saved()["chains"])

    def test_restart_stale_accepted_assessment_interrupted_replacement_reports_unknown(self):
        self.terminal()
        packet = self.prepare()
        self.assertEqual("assessed", self.settle(packet, self.assessment(packet))["outcome"])
        self.issue["body"] += "\nA new occurrence."
        replacement = self.prepare()
        self.assertIsNotNone(replacement)
        self.policy(allow_cloud_task=False, allow_branch_writes=False, allow_draft_pr=False,
                    track_linked_pr=False)
        before = self.saved()["normalRuns"]
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_posts))
        body = self.issue_posts[0][0]["body"]
        self.assertIn("the cause is not independently established", body)
        self.assertNotIn("Shepherd assessment", body)
        self.assertEqual(before, self.saved()["normalRuns"])
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))

    def test_independent_process_edited_closed_empty_reports_once_and_recovers_lost_receipt(self):
        for crash in (False, True):
            with self.subTest(crash=crash):
                canonical = self.root / f"process-{crash}.json"
                run = lambda command: self.process(canonical, command)
                run("prepare-feed")
                run("settle-intake")
                run("approve")
                run("prepare")
                run("terminal")
                run("closed-empty-edit")
                run("grant-report")
                self.assertEqual("issue-assessment", run("prepare")["normal"])
                if crash:
                    self.assertEqual("interrupted-after-post", run("settle-crash-after-post")["outcome"])
                    run("source-edit")
                else:
                    self.assertEqual("commented", run("settle-assessment")["report"]["outcome"])
                self.assertIsNone(run("prepare"))
                self.assertIsNone(run("prepare"))
                journal = json.loads(canonical.read_text())
                record = state.parse(journal["authority"])["workItems"][0]
                self.assertEqual(1, len(journal["posts"]))
                self.assertEqual(1, len(journal["issue_posts"]))
                self.assertEqual("delivered", record["comment"]["delivery"])
                self.assertEqual(8000, record["comment"]["comment_id"])
                self.assertEqual(1, len(record["assignments"]))
                self.assertFalse(record["control"]["authority"]["issue_comment"])
                self.assertIn("is closed with **0 changed files**", record["comment"]["body"])

    def test_independent_process_stale_assessment_interruption_reports_unknown(self):
        canonical = self.root / "stale-process.json"
        run = lambda command: self.process(canonical, command)
        for command in ("prepare-feed", "settle-intake", "approve", "prepare", "terminal",
                        "closed-empty-edit", "prepare", "settle-assessment", "source-edit"):
            run(command)
        self.assertEqual("issue-assessment", run("prepare")["normal"])
        before = state.parse(json.loads(canonical.read_text())["authority"])["normalRuns"]
        run("grant-report")
        self.assertIsNone(run("prepare"))
        self.assertIsNone(run("prepare"))
        journal = json.loads(canonical.read_text())
        saved = state.parse(journal["authority"])
        self.assertEqual(before, saved["normalRuns"])
        self.assertEqual(1, len(journal["issue_posts"]))
        self.assertEqual(1, len(journal["posts"]))
        self.assertIn("the cause is not independently established", journal["issue_posts"][0][0]["body"])
        self.assertNotIn("Shepherd assessment", journal["issue_posts"][0][0]["body"])

    def test_source_changes_after_prepare_reject_without_new_task_or_assessment(self):
        self.terminal()
        packet = self.prepare()
        self.issue["title"] += " changed"
        self.policy()
        receipt = self.settle(packet, self.assessment(packet))
        self.assertEqual("rejected", receipt["outcome"])
        self.assertIsNone(self.saved()["workItems"][0]["assignments"][0]["execution"]["followup"]["assessment"])
        self.assertEqual([], self.issue_posts)
        self.assertEqual(1, len(self.posts))
        next_packet = self.prepare()
        self.assertEqual("issue-assessment", next_packet["normal"])
        self.assertTrue(next_packet["context"]["sourceChangedSinceDispatch"])

    def test_source_changes_during_collection_reject_new_claims(self):
        self.terminal()
        original = result_collector.HostedCollector.__call__
        def change(collector, *args):
            text = original(collector, *args)
            self.issue["body"] += "\nEdited during collection."
            return text
        with patch.object(result_collector.HostedCollector, "__call__", change):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.prepare()
        record = self.saved()["workItems"][0]
        self.assertIsNone(record["assignments"][0]["result"])
        self.assertEqual("collecting", record["assignments"][0]["execution"]["followup"]["status"])
        self.assertEqual([], self.issue_posts)
        self.assertEqual(1, len(self.posts))
        self.assertEqual("issue-assessment", self.prepare()["normal"])

    def test_scope_rejects_reload_replacement_without_repinning(self):
        self.terminal()
        self.prepare()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        record = api.ledger["workItems"][0]
        control, assignment = record["control"], record["assignments"][0]
        observed = normal_loop.source(api, 20872)
        original = deepcopy(api.ledger)
        for field in ("id", "basis", "task_id", "session_id"):
            with self.subTest(field=field):
                self.remote.comments[0]["body"] = state.render(original)
                api.expected, api.ledger = deepcopy(original), deepcopy(original)
                with api.followup_scope(control, assignment, observed):
                    replaced = deepcopy(original)
                    current = replaced["workItems"][0]["assignments"][0]
                    if field == "id":
                        current["id"] = "replaced-assignment"
                        current["execution"]["approval"]["assignment_id"] = current["id"]
                        current["result"]["assignment_id"] = current["id"]
                    elif field == "basis":
                        current["basis"]["scope"] += " replaced"
                    else:
                        current["execution"][field] = "different-" + field
                        current["execution"]["followup"][field] = current["execution"][field]
                        if field == "session_id":
                            current["result"]["worker_id"] = current["execution"][field]
                    self.remote.comments[0]["body"] = state.render(replaced)
                    with self.assertRaisesRegex(ValueError, "ownership changed"):
                        receiver.load(api, lambda: deepcopy(control))
                self.assertIsNone(api._normal_followup)
        self.assertEqual([], self.issue_posts)
        self.assertEqual(1, len(self.posts))

    def test_observational_scope_forbids_direct_adoption_and_dispatch(self):
        self.terminal()
        self.add_pr(1)
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        record = api.ledger["workItems"][0]
        execution.refresh(api, record, record["assignments"][0])
        api.persist()
        with api.followup_scope(record["control"], record["assignments"][0], normal_loop.source(api, 20872)):
            for action in (lambda: execution.import_pr(api, lambda: record["control"]),
                           lambda: execution.observe(api, lambda: record["control"], adopt=True),
                           lambda: api.start_task({}, lambda: None)):
                with self.assertRaisesRegex(ValueError, "observational follow-up"):
                    action()
        self.assertEqual([], self.saved()["chains"])
        self.assertEqual(1, len(self.posts))

    def test_packet_ownership_digest_rejects_replaced_assignment_before_assessment_acceptance(self):
        self.terminal()
        packet = self.prepare()
        original = self.saved()
        for field in ("id", "basis", "task_id", "session_id"):
            with self.subTest(field=field):
                saved = deepcopy(original)
                assignment = saved["workItems"][0]["assignments"][0]
                if field == "id":
                    assignment["id"] = "replaced-assignment"
                    assignment["execution"]["approval"]["assignment_id"] = assignment["id"]
                    assignment["result"]["assignment_id"] = assignment["id"]
                elif field == "basis":
                    assignment["basis"]["scope"] += " changed"
                else:
                    assignment["execution"][field] = "different-" + field
                    assignment["execution"]["followup"][field] = assignment["execution"][field]
                    if field == "session_id":
                        assignment["result"]["worker_id"] = assignment["execution"][field]
                self.remote.comments[0]["body"] = state.render(saved)
                self.assertEqual("rejected", self.settle(packet, self.assessment(packet))["outcome"])
                after = self.saved()
                self.assertIsNone(after["workItems"][0]["assignments"][0]["execution"]["followup"]["assessment"])
                self.assertEqual(2, after["normalRuns"][-1]["nativeActual"])
                self.assertEqual([], self.issue_posts)
                self.assertEqual(1, len(self.posts))

    def test_changed_source_substantive_pr_is_observed_without_import(self):
        self.terminal()
        self.add_pr(1)
        self.issue["title"] += " changed"
        packet = self.prepare()
        self.assertEqual(1, packet["context"]["mapping"]["observation"]["changedFiles"])
        self.assertEqual("Substantive PR requires human reconciliation.", packet["context"]["mapping"]["reason"])
        self.assertEqual([], self.saved()["chains"])
        self.assertEqual(1, len(self.posts))

    def test_current_exact_false_and_mismatched_policy_deny_late_reporting(self):
        self.terminal()
        packet = self.prepare()
        self.settle(packet, self.assessment(packet))
        original = self.saved()["workItems"][0]["control"]
        for mismatch in ("false", "number", "repository", "node_id"):
            with self.subTest(mismatch=mismatch):
                value = self.policy(issue_comment=mismatch != "false")
                if mismatch != "false":
                    value["issue"][mismatch] = {"number": 20873, "repository": "radical/aspire",
                                               "node_id": "OTHER"}[mismatch]
                os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(value)
                self.assertIsNone(self.prepare())
                self.assertEqual([], self.issue_posts)
                self.assertEqual(original, self.saved()["workItems"][0]["control"])

    def test_current_policy_revokes_saved_grant_and_malformed_policy_fails_closed(self):
        self.policy()
        packet = self.prepare(FEED)
        self.settle(packet, self.intake(packet))
        self.task("completed")
        self.logs = "No structured worker JSON."
        self.policy(issue_comment=False)
        packet = self.prepare()
        receipt = self.settle(packet, self.assessment(packet))
        self.assertEqual("human_wait", receipt["report"]["outcome"])
        self.assertTrue(self.saved()["workItems"][0]["control"]["authority"]["issue_comment"])
        exact = self.policy(issue_comment=False)
        for malformed in (exact | {"issue_comment": "true"}, [exact, exact]):
            with self.subTest(malformed=malformed):
                os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(malformed)
                with self.assertRaisesRegex(ValueError, "normal policy|duplicate issue policy"):
                    self.prepare()
                self.assertEqual([], self.issue_posts)
        os.environ.pop("CI_SHEPHERD_NORMAL_POLICY")
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_posts))

    def test_missing_worker_json_interrupted_assessment_reports_unknown_without_inference_replay(self):
        self.terminal()
        self.logs = "Unstructured session text claims a cause, but supplies no final JSON."
        packet = self.prepare()
        self.assertIsNone(packet["context"]["result"])
        receipts = self.saved()["normalRuns"]
        self.policy()
        self.assertIsNone(self.prepare())
        record = self.saved()["workItems"][0]
        self.assertIsNone(record["assignments"][0]["result"])
        self.assertIsNone(record["assignments"][0]["execution"]["followup"]["assessment"])
        self.assertEqual(receipts, self.saved()["normalRuns"])
        body = self.issue_posts[0][0]["body"]
        self.assertIn("the cause is not independently established", body)
        self.assertIn("No correlated worker explanation available.", body)
        self.assertIn("No PR artifact observed.", body)
        self.assertEqual(1, len(self.posts))
        self.assertEqual(1, len(self.issue_posts))

    def test_late_reporting_grant_remains_bound_to_exact_endpoint_and_body(self):
        self.terminal()
        packet = self.prepare()
        self.settle(packet, self.assessment(packet))
        self.policy()
        api = pilot.hosted_api(RUN, os.environ)
        api.read_authority()
        record = api.ledger["workItems"][0]
        control, assignment = record["control"], record["assignments"][0]
        with api.followup_scope(control, assignment, normal_loop.source(api, 20872)):
            record["comment"]["delivery"] = "delivering"
            api.persist()
            body = record["comment"]["body"]
            for issue, text in ((control["issue"] | {"number": 20873}, body),
                                (control["issue"], body + "\nForged")):
                with self.subTest(issue=issue, text=text):
                    with self.assertRaisesRegex(ValueError, "exact linked issue|exact comment send intent"):
                        api.post_comment(issue, text, lambda: self.fail("invalid report reached final send"))
                    self.assertIsNone(api.transport.approved_report)
        self.assertEqual([], self.issue_posts)
        self.assertEqual(1, len(self.posts))

    def test_uncertain_exact_comment_lookup_never_recollects_or_reposts(self):
        self.terminal()
        packet = self.prepare()
        self.policy()
        original = pilot_github.PilotGitHub.post_comment
        def lost(api, *args):
            original(api, *args)
            raise LostResponse("successful external post; response lost")
        with patch.object(pilot_github.PilotGitHub, "post_comment", lost):
            self.assertEqual("comment_uncertain", self.settle(packet, self.assessment(packet))["report"]["outcome"])
        original_authority = self.remote.comments[0]["body"]
        endpoint = self.prefix + "/issues/20872/comments"
        original_comments = deepcopy(self.remote.values[endpoint])
        for mutation in ("body", "author", "multiple", "absent"):
            with self.subTest(mutation=mutation):
                self.remote.comments[0]["body"] = original_authority
                comments = deepcopy(original_comments)
                if mutation == "body":
                    comments[0]["body"] += "\nChanged same-marker body."
                elif mutation == "author":
                    comments[0]["user"] = {"id": 1, "login": "foreign"}
                elif mutation == "multiple":
                    comments.append(comments[0] | {"id": 8001})
                else:
                    comments = []
                self.remote.values[endpoint] = comments
                with patch.object(result_collector.HostedCollector, "__call__",
                                  side_effect=AssertionError("uncertain delivery must not recollect")):
                    self.assertIsNone(self.prepare())
                self.assertEqual(1, len(self.issue_posts))
                self.assertEqual("uncertain", self.saved()["workItems"][0]["comment"]["delivery"])

    def test_final_send_pr_source_task_policy_mutations_block_all_report_paths(self):
        for path in ("settled", "cached", "unknown"):
            for mutation in ("body", "head", "state", "count", "source", "task", "policy", "owner"):
                with self.subTest(path=path, mutation=mutation):
                    self.remote.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
                    self.posts, self.issue_posts = [], []
                    os.environ.pop("CI_SHEPHERD_NORMAL_POLICY", None)
                    os.environ.pop("CI_SHEPHERD_WORK_ITEM_APPROVAL", None)
                    self.issue["state"], self.issue["labels"] = "open", []
                    self.terminal()
                    pull = self.add_pr(0)
                    packet = self.prepare()
                    if path == "cached":
                        self.settle(packet, self.assessment(packet))
                    self.policy()
                    original = pilot_github.PilotGitHub.post_comment
                    def changed(api, issue, body, before_send):
                        def boundary():
                            if mutation == "body":
                                pull["body"] += "\nEdited after rendering."
                            elif mutation == "head":
                                pull["head"]["sha"] = "c" * 40
                            elif mutation == "state":
                                pull["state"] = "closed"
                            elif mutation == "count":
                                pull["changed_files"] = 1
                            elif mutation == "source":
                                self.issue["body"] += "\nEdited before POST."
                            elif mutation == "task":
                                self.remote.values["agents/repos/microsoft/aspire/tasks/TASK1"]["state"] = "in_progress"
                            elif mutation == "policy":
                                self.policy(issue_comment=False)
                            else:
                                saved = self.saved()
                                saved["workItems"][0]["assignments"][0]["basis"]["scope"] += " replaced"
                                self.remote.comments[0]["body"] = state.render(saved)
                            before_send()
                        return original(api, issue, body, boundary)
                    with patch.object(pilot_github.PilotGitHub, "post_comment", changed):
                        if path == "settled":
                            receipt = self.settle(packet, self.assessment(packet))
                            self.assertEqual("rejected", receipt["outcome"])
                        else:
                            with self.assertRaises(ValueError):
                                self.prepare()
                    self.assertEqual([], self.issue_posts)
                    self.assertEqual(1, len(self.posts))
                    self.assertEqual("delivering", self.saved()["workItems"][0]["comment"]["delivery"])
class FixtureScratchTests(WorkspaceTest, unittest.TestCase):
    def test_consecutive_fallback_fixtures_route_real_collector_with_populated_cache(self):
        with patch.dict(os.environ, {"TMPDIR": str(self.work.resolve().parent)}):
            cached = tempfile.gettempdir()
        for _ in range(2):
            with patch.dict(os.environ):
                os.environ.pop("TMPDIR", None)
                fixture = NormalLoopTests()
                fixture.setUp()
                owned = Path(fixture.tmpdir)
                try:
                    self.assertEqual(fixture.tmpdir, os.environ["TMPDIR"])
                    self.assertEqual(fixture.tmpdir, fixture.env["TMPDIR"])
                    self.assertIn(owned, fixture.root.parents)
                    paths = []
                    original = result_collector.bounded_command
                    def command(argv, environment, **kwargs):
                        directory = Path(environment["GH_CONFIG_DIR"])
                        paths.append(directory)
                        self.assertIn(owned, directory.parents)
                        self.assertTrue(directory.is_dir())
                        return original(argv, environment, **kwargs)
                    collector = result_collector.HostedCollector("gho_fixture", command=command)
                    self.assertTrue(collector.capable)
                    collector("microsoft/aspire", SESSION)
                    collector.pinned({"repository": "microsoft/aspire", "run_id": 100,
                                      "run_attempt": 1, "job_id": 200})
                    self.assertTrue(paths)
                    self.assertEqual(cached, tempfile.tempdir)
                    fixture.process(fixture.root / "scratch-process.json", "prepare-feed")
                finally:
                    fixture.doCleanups()
                self.assertFalse(owned.exists())
                self.assertNotIn("TMPDIR", os.environ)
                self.assertTrue(all(not path.exists() for path in paths))

    def test_inherited_scratch_survives_cleanup_and_environment_clearing(self):
        inherited = str(self.work.resolve())
        with patch.dict(os.environ, {"TMPDIR": inherited}):
            fixture = NormalLoopTests()
            fixture.setUp()
            try:
                self.assertEqual(inherited, fixture.tmpdir)
                self.assertEqual(inherited, fixture.env["TMPDIR"])
                self.assertEqual(inherited, os.environ["TMPDIR"])
                self.assertIn(Path(inherited), fixture.root.parents)
                fixture.process(fixture.root / "scratch-process.json", "prepare-feed")
            finally:
                fixture.doCleanups()
            self.assertTrue(Path(inherited).is_dir())
            self.assertEqual(inherited, os.environ["TMPDIR"])

    def test_invalid_scratch_rejected_before_fixture_allocation(self):
        escape = self.work / "escape"
        escape.symlink_to(Path(__file__).resolve().parents[4].parent, target_is_directory=True)
        for value in ("", "relative", str(self.work.resolve() / "missing"),
                      str(Path(__file__).resolve().parents[4]), str(escape.resolve()),
                      str(escape.absolute())):
            with self.subTest(value=value), patch.dict(os.environ, {"TMPDIR": value}):
                fixture = NormalLoopTests()
                with self.assertRaisesRegex(ValueError, "TMPDIR"):
                    fixture.setUp()
                self.assertFalse(hasattr(fixture, "directory"))
                fixture.doCleanups()


class HostedCollectorTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        allocator = patch.object(result_collector, "tempfile", SimpleNamespace(
            TemporaryDirectory=lambda **kwargs: tempfile.TemporaryDirectory(
                dir=str(self.work.resolve()), **kwargs)))
        allocator.start()
        self.addCleanup(allocator.stop)

    def test_pinned_reader_uses_exact_job_and_selected_credential(self):
        calls = []
        paths = []
        def command(argv, environment, **kwargs):
            config = Path(environment["GH_CONFIG_DIR"]) / "hosts.yml"
            paths.append(config)
            self.assertNotIn("GH_TOKEN", environment)
            self.assertEqual("gho_fixture", json.loads(config.read_text())["github.com"]["oauth_token"])
            calls.append(argv)
            if argv == ["gh", "--version"]:
                return 0, "gh version 2.102.0 (fixture)", ""
            if argv[:2] == ["gh", "auth"]:
                return 0, "gho_fixture", ""
            if argv == ["gh", "api", "user", "--method", "GET"]:
                return 0, '{"id":1472,"login":"radical"}', ""
            return 0, "pinned job log", ""
        collector = result_collector.HostedCollector("gho_fixture", command=command)
        self.assertEqual("pinned job log", collector.pinned({
            "repository": "microsoft/aspire", "run_id": 100, "run_attempt": 1, "job_id": 200}))
        self.assertEqual(["gh", "api", "repos/microsoft/aspire/actions/jobs/200/logs"], calls[-1])
        self.assertTrue(all(not path.exists() for path in paths))

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
