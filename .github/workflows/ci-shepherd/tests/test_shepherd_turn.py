"""Multi-item worklist evaluation and multi-turn batch lifecycle tests."""

from copy import deepcopy
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from github import Response
from helpers import FakeClock, compiled_step, reconciliation_evidence
import hosted
import decision_server
import live
import normal_loop
import pilot
import pilot_binding as bindings
import pilot_github
import pilot_state as state
import result_collector
import round as contracts
import run_report
import work_item_execution as execution
from test_pilot_github import Transport, pr
from test_work_items import result


RUN = {"repository": "radical/aspire", "runId": "42", "runAttempt": "1", "workflowSha": "c" * 40}
SESSION1 = "11111111-1111-4111-8111-111111111111"
SESSION2 = "22222222-2222-4222-8222-222222222222"


class ShepherdMultiItemTurnTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = FakeClock()
        self.remote = Transport()
        self.remote.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        self.prefix = "repos/microsoft/aspire"
        self.repo = {"id": bindings.UPSTREAM.repository_id, "full_name": "microsoft/aspire"}

        self.remote.values[self.prefix] = dict(self.repo, default_branch="main")

        # Two tracked issues: #20872 and #20873
        self.issue1 = {
            "number": 20872, "node_id": "ISSUE20872", "state": "open",
            "repository_url": "https://api.github.com/" + self.prefix, "labels": [],
            "title": "Main CI failure 1", "updated_at": "2026-10-04T00:00:00Z",
            "body": "https://github.com/microsoft/aspire/actions/runs/100",
        }
        self.issue2 = {
            "number": 20873, "node_id": "ISSUE20873", "state": "open",
            "repository_url": "https://api.github.com/" + self.prefix, "labels": [],
            "title": "Main CI failure 2", "updated_at": "2026-10-04T00:00:00Z",
            "body": "https://github.com/microsoft/aspire/actions/runs/200",
        }
        self.remote.values[self.prefix + "/issues/20872"] = self.issue1
        self.remote.values[self.prefix + "/issues/20873"] = self.issue2

        for run_id in (100, 200):
            run = {"id": run_id, "run_attempt": 1, "head_sha": "a" * 40,
                   "repository": self.repo, "status": "completed", "conclusion": "failure"}
            job = {"id": run_id + 50, "run_id": run_id, "run_attempt": 1, "head_sha": "a" * 40,
                   "status": "completed", "conclusion": "failure", "name": f"Unit tests {run_id}", "steps": []}
            self.remote.values[self.prefix + f"/actions/runs/{run_id}"] = run
            self.remote.values[self.prefix + f"/actions/runs/{run_id}/attempts/1"] = run
            self.remote.values[self.prefix + f"/actions/runs/{run_id}/attempts/1/jobs"] = {
                "total_count": 1, "jobs": [job]}
            self.remote.values[self.prefix + f"/actions/jobs/{run_id + 50}"] = job

        self.env = {
            "CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_USER_TOKEN": "gho_fixture",
            "SHEPHERD_TARGET": "upstream", "CI_SHEPHERD_UPSTREAM_TRACKER": "99",
            "CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT": "500",
            "CI_SHEPHERD_UPSTREAM_TRACKER_NODE": "TRACKER99", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTIONS": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fixture-oidc",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.actions.githubusercontent.com/token",
        }
        self.posts = []
        self.issue_posts = []
        self.issue_endpoints = []
        self.logs = {}

        def http(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            if method == "POST" and endpoint == "agents/repos/microsoft/aspire/tasks":
                self.posts.append((deepcopy(body), self.saved()))
                task_id = f"TASK{len(self.posts)}"
                return Response({"id": task_id}, {}, 201)
            if method == "POST" and endpoint.startswith(self.prefix + "/issues/") and endpoint.endswith("/comments"):
                self.issue_posts.append((deepcopy(body), self.saved()))
                self.issue_endpoints.append(endpoint)
                comment = {"id": 8000 + len(self.issue_posts), "user": {"id": 1472, "login": "radical"}, "body": body["body"]}
                self.remote.values.setdefault(endpoint, []).append(comment)
                return Response(comment, {}, 201)
            return self.remote(method, endpoint, body)

        def command(argv, environment, **kwargs):
            if argv == ["gh", "--version"]:
                return 0, "gh version 2.102.0 (2026-09-30)", ""
            if argv[:2] == ["gh", "api"]:
                return 0, json.dumps(self.repo | {"login": "radical", "id": 1472}), ""
            if argv[:2] == ["gh", "auth"]:
                return 0, "gho_fixture\n", ""
            if argv[:3] == ["gh", "agent-task", "view"]:
                session = argv[3]
                return 0, self.logs.get(session, "No structured result."), ""
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

    def settle(self, packet, decision, *, usage=2):
        value = reconciliation_evidence(decision) if decision is not None else None
        return pilot.settle(pilot.hosted_api(RUN, os.environ, billing=True),
                            packet, value, usage, self.clock(), billing_only=False)

    def intake_decision(self, packet, action="investigate"):
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "action": action, "route": "workflow_failure" if action == "investigate" else None,
                "evidenceId": packet["context"]["candidates"][0][0] if action == "investigate" else None,
                "reason": "Investigate the candidate occurrence."}

    def assessment_decision(self, packet):
        summary = ("Worker reported no fix; cause remains unknown." if packet["context"]["result"]
                   else "Structured worker result is unavailable; cause remains unknown.")
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "action": "assess", "assessment": {"summary": summary,
                    "evidence": ["Exact session result is available."], "unknowns": ["Root cause is not validated."]}}

    def policy(self, number=20872, **permissions):
        value = {"schema_version": 1, "issue": {"repository": "microsoft/aspire",
                 "number": number, "node_id": f"ISSUE{number}"}, "allow_cloud_task": True,
                 "allow_branch_writes": True, "allow_draft_pr": True, "track_linked_pr": True,
                 "issue_comment": True} | permissions
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(value)
        return value

    def task(self, task_id, status, session_id=SESSION1, item_index=0):
        prompt = self.posts[item_index][0]["prompt"]
        saved = {"id": task_id, "repository": self.repo, "creator": {"id": 1472}, "state": status,
                 "session_count": 1, "artifacts": [], "sessions": [{
                     "id": session_id, "task_id": task_id, "repository": self.repo, "user": {"id": 1472},
                     "state": status, "base_ref": "main", "head_ref": f"copilot/diagnose-{item_index}",
                     "prompt": prompt, "usage": {"type": "ai_credits", "amount": 10_000_000}}]}
        self.remote.values[f"agents/repos/microsoft/aspire/tasks/{task_id}"] = saved
        return saved

    def add_pr_to_task(self, task_id, pr_number, changes, source_number):
        task = self.remote.values[f"agents/repos/microsoft/aspire/tasks/{task_id}"]
        pull = pr(pr_number)
        for key in ("base", "head"):
            pull[key]["repo"] = self.repo
        pull.update(draft=True, labels=[], issue_url=f"https://api.github.com/{self.prefix}/issues/{pr_number}",
                    body=f"Refs microsoft/aspire#{source_number}", merged=False, merged_at=None, commits=1,
                    changed_files=changes)
        issue = {"id": 5000 + pr_number, "node_id": pull["node_id"], "number": pr_number,
                 "repository_url": f"https://api.github.com/{self.prefix}",
                 "pull_request": {"url": f"https://api.github.com/{self.prefix}/pulls/{pr_number}"}}
        task["sessions"][0]["head_ref"] = pull["head"]["ref"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": pull["id"], "global_id": pull["node_id"]}},
            {"provider": "github", "type": "branch",
             "data": {"base_ref": "main", "head_ref": pull["head"]["ref"]}},
        ]
        self.remote.values.update({
            f"{self.prefix}/pulls/{pr_number}": pull,
            f"{self.prefix}/issues/{pr_number}": issue,
            f"{self.prefix}/pulls/{pr_number}/commits": [
                {"sha": pull["head"]["sha"], "commit": {"message": f"Refs microsoft/aspire#{source_number}"}}],
            f"{self.prefix}/git/ref/heads/{pull['head']['ref']}": {
                "ref": "refs/heads/" + pull["head"]["ref"], "object": {"sha": pull["head"]["sha"]}},
            f"{self.prefix}/issues/{source_number}/timeline": [
                {"event": "cross-referenced", "source": {"type": "issue", "issue": issue}}],
        })
        self.remote.values.setdefault(f"{self.prefix}/pulls", []).append(pull)
        return pull

    def test_multi_item_turn_prioritizes_work_without_blocking_on_running_task(self):
        # 1. First run: intake issue 20872 with policy -> launches TASK1
        self.policy(20872)
        feed1 = {"reported_kind": "workflow_failure", "issue_url": "https://github.com/microsoft/aspire/issues/20872"}
        packet = self.prepare(feed1)
        self.assertEqual("issue-intake", packet["normal"])
        receipt = self.settle(packet, self.intake_decision(packet))
        self.assertEqual("sent", receipt["outcome"])
        self.assertEqual(1, len(self.posts))
        task_id = receipt["task_id"]

        # TASK1 is running in the background with canonical prompt
        self.task(task_id, "in_progress", session_id=SESSION1, item_index=0)

        self.policy(20873)
        feed2 = {"reported_kind": "workflow_failure", "issue_url": "https://github.com/microsoft/aspire/issues/20873"}
        packet2 = self.prepare(feed2)
        self.assertIsNotNone(packet2, "running task must not prevent intake of a new candidate issue")
        self.assertEqual("worklist", packet2["normal"])
        self.assertEqual("in_progress", packet2["worklist"][0]["taskState"])
        receipt2 = self.settle(packet2, self.turn_decision(packet2))
        self.assertEqual("sent", receipt2["items"][0]["outcome"])
        self.assertEqual(2, len(self.posts))
        self.assertEqual(998, self.saved()["workItems"][0]["assignments"][0]["execution"]["worker_reserved"])
        self.task("TASK2", "in_progress", session_id=SESSION2, item_index=1)
        self.assertIsNone(self.prepare())

        # Both work items now exist in ledger
        items = self.saved()["workItems"]
        self.assertEqual(2, len(items))
        self.assertEqual("sent", items[0]["assignments"][0]["execution"]["state"])
        self.assertEqual("sent", items[1]["assignments"][0]["execution"]["state"])
        for index, (_, checkpoint) in enumerate(self.posts):
            saved = checkpoint["workItems"][index]["assignments"][0]["execution"]
            self.assertEqual("sending", saved["state"])
            self.assertGreater(saved["worker_reserved"], 0)
        self.assertLessEqual(state.repository_spend(self.saved(), self.clock()), state.REPOSITORY_ALLOWANCE)

    def test_multi_item_turn_substantive_pr_and_empty_pr_triage(self):
        self.launch_both()
        self.task("TASK1", "completed", session_id=SESSION1, item_index=0)
        self.task("TASK2", "completed", session_id=SESSION2, item_index=1)

        assignment = self.saved()["workItems"][1]["assignments"][0]
        claimed = result(assignment)
        claimed.update(item_id=self.saved()["workItems"][1]["id"], worker_id=SESSION2,
                       outcome="no_fix", changed_files=[], pr_proposal=None,
                       actual_classification="inconclusive", summary="Empty PR generated, no fix established.")
        self.logs[SESSION2] = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"
        self.add_pr_to_task("TASK1", 20926, changes=2, source_number=20872)
        self.add_pr_to_task("TASK2", 20927, changes=0, source_number=20873)

        # Run prepare: empty PR is detected, mapping is denied with zero changes, diverted to assessment
        packet_assess = self.prepare()
        self.assertEqual("worklist", packet_assess["normal"])
        child = packet_assess["packets"][0]
        self.assertEqual("issue-assessment", child["normal"])
        self.assertIn("zero changes", child["context"]["mapping"]["reason"])
        ledger = self.saved()
        self.assertEqual("no_fix", ledger["workItems"][1]["assignments"][0]["result"]["outcome"])
        self.assertEqual(1, len(ledger["chains"]))
        chain = ledger["chains"][0]
        self.assertEqual(20926, chain["origin"])
        self.assertEqual("handoff_needed", chain["handoff"]["phase"])
        self.assertEqual("handoff_needed", packet_assess["worklist"][0]["handoff"])
        self.assertEqual(ledger["workItems"][0]["id"], chain["sourceWorkItem"]["item_id"])
        self.assertIsNone(ledger["workItems"][1]["assignments"][0]["execution"]["imported_chain_id"])

        # Settle assessment -> posts follow-up comment preview / comment
        receipt_assess = self.settle(packet_assess, self.turn_decision(packet_assess))
        self.assertEqual("assessed", receipt_assess["items"][0]["outcome"])
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual([self.prefix + "/issues/20873/comments"], self.issue_endpoints)
        intent = self.issue_posts[0][1]["workItems"][1]["comment"]
        self.assertEqual("delivering", intent["delivery"])
        published = self.saved()["workItems"][1]["comment"]
        self.assertEqual("delivered", published["delivery"])
        self.assertEqual(8001, published["comment_id"])
        self.assertIn("without a verified fix", self.issue_posts[0][0]["body"])
        self.assertIn("Worker reported no fix", self.issue_posts[0][0]["body"])
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_posts))
        self.assertEqual(2, len(self.posts))
        self.assertEqual("handoff_needed", self.saved()["chains"][0]["handoff"]["phase"])

    def turn_decision(self, packet):
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "decisions": [self.intake_decision(child) if child["normal"] == "issue-intake"
                              else self.assessment_decision(child) for child in packet["packets"]]}

    def launch_both(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        feeds = [{"reported_kind": "workflow_failure",
                  "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                 for number in (20872, 20873)]
        packet = self.prepare(feeds)
        self.assertEqual(2, len(packet["packets"]))
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["sent", "sent"], [entry["outcome"] for entry in receipt["items"]])
        self.assertFalse(receipt["waitingForCloud"])
        return packet

    def hosted_environment(self, step_name, policies):
        variables = self.env | {"CI_SHEPHERD_NORMAL_POLICY": json.dumps(policies)}
        environment = dict(self.env)
        for key, expression in compiled_step(step_name)["env"].items():
            match = re.fullmatch(r"\$\{\{\s*vars\.(\w+)(?:\s*\|\|\s*'([^']*)')?\s*\}\}", expression)
            environment[key] = (variables.get(match[1], "") or match[2] or "") if match else self.env.get(key, "")
        return environment

    def test_hosted_repository_policy_binding_authorizes_batch_in_both_stages(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        feeds = [{"reported_kind": "workflow_failure",
                  "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                 for number in (20872, 20873)]
        with patch.dict(os.environ, self.hosted_environment("Prepare host-owned envelope", policies), clear=True):
            packet = self.prepare(feeds)
        self.assertEqual(2, len(packet["packets"]))
        with patch.dict(os.environ, self.hosted_environment(
                "Settle native billing before authorizing an action", policies), clear=True):
            receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["sent", "sent"], [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual(2, len(self.posts))
        self.assertEqual([True, True], [record["control"]["authority"]["issue_comment"]
                                      for record in self.saved()["workItems"]])

    def test_hosted_preparation_policy_binding_dispatches_reserved_batch(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ.pop("CI_SHEPHERD_NORMAL_POLICY")
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["approval-required", "approval-required"],
                         [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual([], self.posts)
        with patch.dict(os.environ, self.hosted_environment("Prepare host-owned envelope", policies), clear=True):
            self.assertIsNone(self.prepare())
        self.assertEqual(2, len(self.posts))
        self.assertEqual(["sent", "sent"], [record["assignments"][0]["execution"]["state"]
                                          for record in self.saved()["workItems"]])
        self.assertEqual([False, False], [record["control"]["authority"]["issue_comment"]
                                        for record in self.saved()["workItems"]])

    def test_reserved_capacity_deferral_allows_fresh_intake_and_exactly_once_retry(self):
        self.launch_both()
        first = self.task("TASK1", "in_progress", SESSION1, 0)
        self.task("TASK2", "in_progress", SESSION2, 1)
        self.assertIsNone(self.prepare())
        hold = self.saved()["workItems"][0]["assignments"][0]["execution"]["worker_reserved"]
        first["sessions"][0]["usage"] = None
        for number in (20874, 20875):
            self.remote.values[f"{self.prefix}/issues/{number}"] = dict(
                self.issue1, number=number, node_id=f"ISSUE{number}")
        policies = [self.policy(number) for number in range(20872, 20876)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        feed = lambda number: {"reported_kind": "workflow_failure",
                               "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
        third = self.prepare(feed(20874))
        receipt = self.settle(third, self.turn_decision(third))
        self.assertEqual("deferred", receipt["items"][0]["outcome"])
        reserved = self.saved()["workItems"][2]["assignments"][0]
        self.assertEqual("reserved", reserved["execution"]["state"])
        self.assertEqual(2, len(self.posts))

        fourth = self.prepare(feed(20875))
        self.assertEqual(20875, fourth["packets"][0]["context"]["issue"]["identity"]["number"])
        self.assertEqual("shared cloud capacity exhausted", fourth["worklist"][2]["attention"])
        self.assertEqual("deferred", self.settle(fourth, self.turn_decision(fourth))["items"][0]["outcome"])
        self.assertEqual(20875, self.saved()["workItems"][3]["control"]["issue"]["number"])
        self.assertEqual("reserved", self.saved()["workItems"][3]["assignments"][0]["execution"]["state"])
        self.assertEqual(reserved, self.saved()["workItems"][2]["assignments"][0])
        self.assertEqual(hold, self.saved()["workItems"][0]["assignments"][0]["execution"]["worker_reserved"])
        self.assertEqual(2, len(self.posts))

        self.task("TASK1", "completed", SESSION1, 0)
        self.task("TASK2", "completed", SESSION2, 1)
        self.prepare()
        self.assertEqual(4, len(self.posts))
        self.assertEqual("sent", self.saved()["workItems"][2]["assignments"][0]["execution"]["state"])
        self.assertIsNone(self.posts[2][1]["workItems"][2]["assignments"][0]["execution"]["attention"])
        self.assertEqual(reserved["id"], self.saved()["workItems"][2]["assignments"][0]["id"])
        self.task("TASK3", "in_progress", "33333333-3333-4333-8333-333333333333", 2)
        self.task("TASK4", "in_progress", "44444444-4444-4444-8444-444444444444", 3)
        self.prepare()
        self.assertEqual(4, len(self.posts))

    def test_reserved_reentry_propagates_authority_and_unclassified_failures(self):
        self.policy()
        packet = self.prepare({"reported_kind": "workflow_failure",
                               "issue_url": "https://github.com/microsoft/aspire/issues/20872"})
        with patch.object(execution, "execute", side_effect=execution.AdmissionDeferred("shared cloud capacity exhausted")):
            self.assertEqual("deferred", self.settle(packet, self.intake_decision(packet))["outcome"])
        before = self.saved()["workItems"][0]["assignments"][0]
        for error in (pilot_github.AuthorityUncertain("shared cloud capacity exhausted"),
                      ValueError("shared cloud capacity exhausted")):
            with self.subTest(error=type(error).__name__), patch.object(execution, "execute", side_effect=error):
                with self.assertRaises(type(error)):
                    self.prepare()
            self.assertEqual(before, self.saved()["workItems"][0]["assignments"][0])
        self.assertEqual([], self.posts)

    def test_three_queued_issues_drain_one_free_slot_at_a_time(self):
        self.launch_both()
        sessions = [SESSION1, SESSION2] + [
            f"{number:08d}-3333-4333-8333-333333333333" for number in range(3, 6)]
        self.task("TASK1", "in_progress", sessions[0], 0)
        self.task("TASK2", "in_progress", sessions[1], 1)
        policies = [self.policy(number) for number in range(20872, 20877)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        for number in range(20874, 20877):
            self.remote.values[f"{self.prefix}/issues/{number}"] = dict(
                self.issue1, number=number, node_id=f"ISSUE{number}")
            packet = self.prepare({"reported_kind": "workflow_failure",
                                   "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"})
            self.assertEqual("deferred", self.settle(packet, self.turn_decision(packet))["items"][0]["outcome"])
        queued = [deepcopy(record["assignments"][0]) for record in self.saved()["workItems"][2:]]
        self.assertEqual(2, len(self.posts))

        for completed in range(3):
            self.task(f"TASK{completed + 1}", "completed", sessions[completed], completed)
            self.prepare()
            self.assertEqual(completed + 3, len(self.posts))
            assignments = [record["assignments"][0] for record in self.saved()["workItems"][2:]]
            self.assertEqual([entry["id"] for entry in queued], [entry["id"] for entry in assignments])
            self.assertEqual(["sent"] * (completed + 1) + ["reserved"] * (2 - completed),
                             [entry["execution"]["state"] for entry in assignments])
            self.task(f"TASK{completed + 3}", "in_progress", sessions[completed + 2], completed + 2)
            self.prepare()
            self.assertEqual(completed + 3, len(self.posts))
        for index in (3, 4):
            self.task(f"TASK{index + 1}", "completed", sessions[index], index)
        self.prepare()
        self.prepare()
        self.assertEqual(5, len(self.posts))
        self.assertEqual([1] * 5, [len(record["assignments"]) for record in self.saved()["workItems"]])

    def test_last_before_send_authority_uncertainty_aborts_batch_without_compensation(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        refresh = execution.refresh_capacity
        calls = []
        boundary = []

        def uncertain_at_final_guard(api):
            calls.append(None)
            if len(calls) == 4:
                boundary.append((deepcopy(self.saved()), persist.call_count))
                raise pilot_github.AuthorityUncertain("final capacity authority unavailable")
            return refresh(api)

        with patch.object(execution, "refresh_capacity", side_effect=uncertain_at_final_guard), \
                patch.object(pilot_github.PilotGitHub, "persist", autospec=True,
                             side_effect=pilot_github.PilotGitHub.persist) as persist:
            with self.assertRaisesRegex(pilot_github.AuthorityUncertain, "final capacity"):
                self.settle(packet, self.turn_decision(packet))
            self.assertEqual(4, len(calls))
            self.assertEqual(boundary[0][0], self.saved())
            self.assertEqual(boundary[0][1], persist.call_count)
        self.assertEqual([], self.posts)
        first = self.saved()["workItems"][0]["assignments"][0]["execution"]
        self.assertEqual("sending", first["state"])
        self.assertGreater(first["worker_reserved"], 0)
        self.assertIsNone(first["task_id"])

    def test_resumed_workers_after_sending_checkpoint_leave_candidate_retryable(self):
        self.launch_both()
        self.task("TASK1", "completed", SESSION1, 0)
        self.task("TASK2", "completed", SESSION2, 1)
        self.remote.values[f"{self.prefix}/issues/20874"] = dict(
            self.issue1, number=20874, node_id="ISSUE20874")
        policies = [self.policy(number) for number in (20872, 20873, 20874)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare({"reported_kind": "workflow_failure",
                               "issue_url": "https://github.com/microsoft/aspire/issues/20874"})
        original = pilot_github.PilotGitHub.persist
        checkpoints = []

        def resume_at_sending(api):
            original(api)
            records = api.ledger.get("workItems", [])
            if (len(records) == 3 and records[2]["assignments"]
                    and records[2]["assignments"][-1]["execution"]["state"] == "sending"):
                checkpoints.append(deepcopy(self.saved()))
                self.task("TASK1", "in_progress", SESSION1, 0)
                self.task("TASK2", "in_progress", SESSION2, 1)

        with patch.object(pilot_github.PilotGitHub, "persist", autospec=True, side_effect=resume_at_sending):
            receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual("deferred", receipt["items"][-1]["outcome"])
        candidate = self.saved()["workItems"][2]["assignments"][-1]
        self.assertEqual("reserved", candidate["execution"]["state"])
        self.assertEqual("shared cloud capacity exhausted", candidate["execution"]["attention"])
        self.assertIsNone(candidate["execution"]["task_id"])
        self.assertEqual(checkpoints[0]["workItems"][2]["assignments"][-1]["execution"]["worker_reserved"],
                         candidate["execution"]["worker_reserved"])
        self.assertGreater(candidate["execution"]["worker_reserved"], 0)
        self.assertEqual(2, len(self.posts))
        self.assertEqual(["in_progress", "in_progress"],
                         [row["assignments"][-1]["execution"]["task_state"]
                          for row in self.saved()["workItems"][:2]])

        self.task("TASK1", "completed", SESSION1, 0)
        self.task("TASK2", "completed", SESSION2, 1)
        self.prepare()
        self.assertEqual(3, len(self.posts))
        self.assertEqual(candidate["id"], self.saved()["workItems"][2]["assignments"][-1]["id"])
        self.task("TASK3", "in_progress", "33333333-3333-4333-8333-333333333333", 2)
        self.prepare()
        self.assertEqual(3, len(self.posts))

    def test_batch_refresh_rejects_cross_item_session_correlation(self):
        self.launch_both()
        first = self.task("TASK1", "in_progress", SESSION1, 0)
        second = self.task("TASK2", "in_progress", SESSION2, 1)
        first["sessions"][0]["prompt"], second["sessions"][0]["prompt"] = (
            second["sessions"][0]["prompt"], first["sessions"][0]["prompt"])
        self.assertIsNone(self.prepare())
        for record in self.saved()["workItems"]:
            self.assertIn("correlation mismatch", record["assignments"][0]["execution"]["attention"])
        self.assertEqual(2, len(self.posts))

    def test_batch_swapped_decisions_reject_before_any_task_send(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        decision = self.turn_decision(packet)
        decision["decisions"].reverse()
        receipt = self.settle(packet, decision)
        self.assertEqual(["rejected", "rejected"], [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual([], self.posts)
        self.assertEqual(2, sum(entry["nativeActual"] for entry in self.saved()["normalRuns"]))

    def test_batch_reentry_settles_usage_once_and_reports_each_outcome(self):
        packet = self.launch_both()
        before = self.saved()
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["replay", "replay"], [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual(before, self.saved())
        report = run_report.render(RUN, "upstream", state.new_ledger("microsoft/aspire"),
                                   self.saved(), receipt, packet, packet["preparedAt"], packet["preparedAt"])
        self.assertIn("2 decisions in one fresh session", report)
        self.assertIn("without waiting for cloud completion", report)
        for entry in packet["packets"]:
            self.assertIn(entry["item"], report)

    def test_batch_one_stale_source_does_not_block_other_authorized_intake(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        self.issue1["state"] = "closed"
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["rejected", "sent"], [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual(1, len(self.posts))
        self.assertEqual(20873, self.saved()["workItems"][0]["control"]["issue"]["number"])
        self.assertEqual(2, sum(entry["nativeActual"] for entry in self.saved()["normalRuns"]))

    def test_batch_unknown_native_billing_keeps_each_hold_without_resend(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        receipt = self.settle(packet, None, usage=None)
        self.assertEqual(["rejected", "rejected"], [entry["outcome"] for entry in receipt["items"]])
        self.clock.advance(hours=25)
        self.assertEqual(2 * state.NATIVE_RESERVE, state.repository_spend(self.saved(), self.clock()))
        self.assertEqual([], self.posts)

    def test_batch_bills_all_children_before_uncertain_authority_stops_effects(self):
        policies = [self.policy(number) for number in (20872, 20873)]
        os.environ["CI_SHEPHERD_NORMAL_POLICY"] = json.dumps(policies)
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        decision = self.turn_decision(packet)
        with patch.object(execution, "execute", side_effect=pilot_github.AuthorityUncertain("receipt unavailable")):
            with self.assertRaises(pilot_github.AuthorityUncertain):
                self.settle(packet, decision)
        receipts = self.saved()["normalRuns"]
        self.assertEqual([1, 1], [entry["nativeActual"] for entry in receipts])
        self.assertEqual([0, 0], [entry["nativeReserved"] for entry in receipts])
        self.assertEqual([], self.posts)

    def test_safe_output_accepts_batch_proposal_without_mutations(self):
        packet = self.prepare([{"reported_kind": "workflow_failure",
                                "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                               for number in (20872, 20873)])
        decision = self.turn_decision(packet)
        before = self.saved()
        output = self.root / "batch-decision.json"
        response = decision_server.handle({"method": "tools/call", "params": {
            "name": "submit_decision", "arguments": {"decision": json.dumps(decision)}}}, output)
        self.assertFalse(response["isError"])
        self.assertEqual(decision, json.loads(output.read_text()))
        self.assertEqual(before, self.saved())
        self.assertEqual([], self.posts)
        self.assertEqual([], self.issue_posts)

    def test_batch_transport_capacity_defers_children_without_orphan_reservations(self):
        with patch.object(contracts, "MAX_JSON_BYTES", 8000):
            packet = self.prepare([{"reported_kind": "workflow_failure",
                                    "issue_url": f"https://github.com/microsoft/aspire/issues/{number}"}
                                   for number in (20872, 20873)])
        self.assertEqual("worklist", packet["normal"])
        self.assertEqual(1, len(packet["packets"]))
        self.assertEqual(1, len(self.saved()["normalRuns"]))
        self.assertEqual(2, len(packet["worklist"]))
        self.assertIn("capacity exhausted", packet["worklist"][1]["attention"])
        self.assertLessEqual(len(json.dumps(packet).encode()), 8000)
        self.assertEqual([], self.posts)

    def test_unknown_terminal_item_budget_does_not_starve_other_assessment(self):
        self.launch_both()
        first = self.task("TASK1", "completed", SESSION1, 0)
        first["sessions"][0]["usage"] = None
        self.task("TASK2", "completed", SESSION2, 1)
        packet = self.prepare()
        self.assertEqual(1, len(packet["packets"]))
        self.assertEqual(self.saved()["workItems"][1]["id"], packet["packets"][0]["item"])
        self.assertIn("credit allowance exhausted", packet["worklist"][0]["attention"])
        first_execution = self.saved()["workItems"][0]["assignments"][0]["execution"]
        self.assertEqual(999, first_execution["worker_reserved"])
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual("assessed", receipt["items"][0]["outcome"])
        self.assertEqual([self.prefix + "/issues/20873/comments"], self.issue_endpoints)
        self.assertEqual(999, self.saved()["workItems"][0]["assignments"][0]["execution"]["worker_reserved"])

    def test_missing_pr_metadata_is_unknown_not_zero_changes(self):
        self.launch_both()
        self.task("TASK1", "completed", SESSION1, 0)
        self.task("TASK2", "in_progress", SESSION2, 1)
        pull = self.add_pr_to_task("TASK1", 20926, changes=2, source_number=20872)
        del pull["changed_files"]
        packet = self.prepare()
        child = packet["packets"][0]
        self.assertEqual("issue-assessment", child["normal"])
        self.assertEqual("PR mapping denied: PR change inventory unavailable", child["context"]["mapping"]["reason"])
        self.assertIsNone(child["context"]["result"])
        self.assertEqual([], self.saved()["chains"])
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual("assessed", receipt["items"][0]["outcome"])
        self.assertEqual([self.prefix + "/issues/20872/comments"], self.issue_endpoints)
        self.assertIsNone(self.prepare())
        self.assertEqual(1, len(self.issue_posts))

    def test_failed_and_completed_no_pr_tasks_are_assessed_together_once(self):
        self.launch_both()
        self.task("TASK1", "failed", SESSION1, 0)
        self.task("TASK2", "completed", SESSION2, 1)
        record = self.saved()["workItems"][1]
        claimed = result(record["assignments"][0])
        claimed.update(item_id=record["id"], worker_id=SESSION2, outcome="no_fix",
                       changed_files=[], pr_proposal=None)
        self.logs[SESSION2] = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"
        packet = self.prepare()
        self.assertEqual(["issue-assessment", "issue-assessment"], [child["normal"] for child in packet["packets"]])
        self.assertIsNone(packet["packets"][0]["context"]["result"])
        self.assertEqual("no_fix", packet["packets"][1]["context"]["result"]["outcome"])
        receipt = self.settle(packet, self.turn_decision(packet))
        self.assertEqual(["assessed", "assessed"], [entry["outcome"] for entry in receipt["items"]])
        self.assertEqual([self.prefix + "/issues/20872/comments", self.prefix + "/issues/20873/comments"],
                         self.issue_endpoints)
        self.assertEqual([], self.saved()["chains"])
        self.assertIsNone(self.prepare())
        self.assertEqual(2, len(self.issue_posts))
        self.assertEqual(2, len(self.posts))


if __name__ == "__main__":
    unittest.main()
