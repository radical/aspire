import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import zipfile

from helpers import WorkspaceTest, compiled_environment, compiled_step, reconciliation_decision, reconciliation_evidence
from github import IncompleteInventory, LostResponse, RejectedEffect, Response
import hosted
import live
import receipts
import round as contracts


RUN = {"repository": live.REPOSITORY, "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40}


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as value:
        for name, content in files.items():
            value.writestr(name, json.dumps(content))
    return output.getvalue()


class FakeService:
    """HTTP-shaped fixture with independent tasks, canonical comments and Actions artifacts."""
    def __init__(self):
        repo = {"id": live.REPOSITORY_ID, "full_name": live.REPOSITORY}
        self.pr = {"id": live.PR_ID, "node_id": live.PR_NODE, "number": 121, "created_at": live.CREATED_AT,
                   "state": "open", "labels": [{"name": "shepherd-adopted"}],
                   "base": {"ref": live.BASE, "repo": repo}, "head": {"ref": live.HEAD, "sha": live.INITIAL_HEAD, "repo": repo}}
        self.actor = {"id": 500, "login": "radical"}
        self.run = deepcopy(RUN)
        self.comments, self.reviews, self.tasks, self.history, self.calls = [], [], {}, [], []
        self.logs = b"test_normalization (test_labels.LabelTests)\nAssertionError: '  HELLO  ' != 'hello'\nFAILED (failures=5)\n"
        self.task_loss = False
        self.task_reject = False
        self.comment_loss = None
        self.ci_head = live.INITIAL_HEAD
        self.ci_state = "completed"
        self.ci_conclusion = "failure"
        self.ci_extra_runs = []
        self.ci_jobs = None
        self.task_status = "queued"
        self.hide_tasks = False
        self.task_model = "actual-server-model"
        self.incomplete = None
        self.before_request = None
        self.file_scope = True
        self.commit_shas = []
        self.commit_files = {}
        self.commit_headers = {}
        self.comparison_override = {}

    def current_run(self):
        return {"id": int(self.run["runId"]), "run_attempt": int(self.run["runAttempt"]), "path": live.WORKFLOW,
                "run_number": int(self.run["runId"]) - 38,
                "event": "workflow_dispatch", "status": "in_progress", "conclusion": None,
                "head_repository": {"full_name": live.REPOSITORY}, "head_sha": self.run["workflowSha"],
                "actor": self.actor, "updated_at": "2026-10-04T04:00:00Z"}

    def task_value(self, task_id, prompt):
        return {"id": task_id, "state": self.task_status, "creator": self.actor,
                "repository": {"id": live.REPOSITORY_ID}, "session_count": 1, "created_at": "2026-10-04T04:00:00Z",
                "artifacts": [{"provider": "github", "type": "branch", "data": {"head_ref": live.HEAD, "base_ref": live.BASE}}],
                "sessions": [{"id": "session-" + task_id, "task_id": task_id, "repository": {"id": live.REPOSITORY_ID},
                              "prompt": prompt, "state": self.task_status, "model": self.task_model,
                              "created_at": "2026-10-04T04:00:00Z",
                              "head_ref": live.HEAD, "base_ref": live.BASE,
                              "usage": {"type": "ai_credits", "amount": 1500000000}}]}

    def transport(self, method, endpoint, body):
        self.calls.append((method, endpoint, deepcopy(body)))
        if self.before_request:
            self.before_request(self, method, endpoint)
        parsed = urlparse(endpoint)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self.incomplete and self.incomplete in endpoint:
            return Response({}, {}, 403)
        prefix = f"repos/{live.REPOSITORY}"
        if path == "user" or path == "users/radical":
            return Response(self.actor, {})
        if path == prefix:
            return Response({"id": live.REPOSITORY_ID, "full_name": live.REPOSITORY}, {})
        if path == prefix + "/pulls/121":
            return Response(deepcopy(self.pr), {})
        if path == prefix + "/pulls":
            return Response([deepcopy(self.pr)], {})
        if path == prefix + "/pulls/121/reviews":
            return Response(deepcopy(self.reviews), {})
        if path == prefix + "/pulls/121/comments":
            return Response([], {})
        if path == prefix + "/issues/121/comments":
            if method == "GET":
                return Response(deepcopy(self.comments), {})
            record = {"id": 501, "user": self.actor, **body}
            if self.comment_loss == "before":
                self.comment_loss = None
                raise LostResponse("lost create")
            self.comments.append(record)
            if self.comment_loss == "after":
                self.comment_loss = None
                raise LostResponse("lost create")
            return Response(record, {}, 201)
        if path == prefix + "/issues/comments/501":
            self.comments[0].update(body)
            return Response(deepcopy(self.comments[0]), {})
        if path == f"agents/repos/{live.REPOSITORY}/tasks":
            if method == "GET":
                archived = query["is_archived"][0]
                values = list(self.tasks.values()) if archived == "false" and not self.hide_tasks else []
                return Response({"tasks": deepcopy(values)}, {})
            if self.task_reject:
                raise RejectedEffect("HTTP 403")
            task_id = "task-" + str(len(self.tasks) + 1)
            self.tasks[task_id] = self.task_value(task_id, body["prompt"])
            if self.task_loss:
                raise LostResponse("lost POST")
            return Response(deepcopy(self.tasks[task_id]), {}, 201)
        if path.startswith(f"agents/repos/{live.REPOSITORY}/tasks/"):
            return Response(deepcopy(self.tasks[path.rsplit("/", 1)[1]]), {})
        actions = prefix + "/actions"
        if path == actions + "/workflows/ci-shepherd.lock.yml":
            return Response({"id": live.WORKFLOW_ID, "path": live.WORKFLOW}, {})
        if path == actions + "/workflows/ci-shepherd-fixture.yml":
            return Response({"id": 200, "path": live.FIXTURE_WORKFLOW}, {})
        if path == actions + f"/workflows/{live.WORKFLOW_ID}/runs":
            return Response({"workflow_runs": [self.current_run()] + [entry["run"] for entry in self.history]}, {})
        if path == actions + "/workflows/200/runs":
            values = [{"id": 10, "run_attempt": 1, "head_sha": self.ci_head, "path": live.FIXTURE_WORKFLOW,
                       "event": "pull_request", "pull_requests": [{"number": 121}],
                       "status": self.ci_state, "conclusion": self.ci_conclusion}] if query["head_sha"] == [self.ci_head] else []
            values.extend(deepcopy(run) for run in self.ci_extra_runs if query["head_sha"] == [run["head_sha"]])
            return Response({"workflow_runs": values}, {})
        if path == actions + "/runs/10/attempts/1/jobs":
            if self.ci_jobs is not None:
                return Response(deepcopy(self.ci_jobs), {})
            return Response({"jobs": [{"id": 20, "run_id": 10, "head_sha": self.ci_head, "name": live.JOB,
                                      "status": self.ci_state, "conclusion": self.ci_conclusion}]}, {})
        if path == actions + "/jobs/20/logs":
            return Response(self.logs, {})
        if "/compare/" in path:
            shas = self.commit_shas or [self.pr["head"]["sha"]]
            return Response({"status": "ahead", "total_commits": len(shas), "ahead_by": len(shas), "behind_by": 0,
                             "base_commit": {"sha": live.INITIAL_HEAD}, "merge_base_commit": {"sha": live.INITIAL_HEAD},
                             "commits": [{"sha": sha, "parents": [{"sha": shas[index - 1] if index else live.INITIAL_HEAD}]}
                                         for index, sha in enumerate(shas)],
                             "files": [{"filename": ".ci-shepherd-fixture/labels.py" if self.file_scope else ".ci-shepherd-fixture/test_labels.py",
                                        "status": "modified"}], **deepcopy(self.comparison_override)}, {})
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            shas = self.commit_shas or [self.pr["head"]["sha"]]
            index = shas.index(sha)
            return Response({"sha": sha, "parents": [{"sha": shas[index - 1] if index else live.INITIAL_HEAD}],
                             "files": deepcopy(self.commit_files.get(sha, [
                                 {"filename": ".ci-shepherd-fixture/labels.py", "status": "modified"}]))},
                            self.commit_headers.get(sha, {}))
        for entry in self.history:
            run = entry["run"]
            if path == actions + f"/runs/{run['id']}/artifacts":
                return Response({"artifacts": entry["artifacts"]}, {})
            if path == actions + f"/runs/{run['id']}/attempts/1/jobs":
                return Response({"jobs": entry["jobs"]}, {})
            if path == actions + f"/artifacts/{run['id']}/zip":
                return Response(entry["zip"], {})
        if path == actions + f"/runs/{self.run['runId']}/artifacts":
            return Response({"artifacts": []}, {})
        raise AssertionError((method, endpoint, body))

    def add_history(self, receipt, audit):
        run = {**self.current_run(), "id": int(receipt["run"]["runId"]), "head_sha": receipt["run"]["workflowSha"],
               "status": "completed", "conclusion": "success"}
        self.history.append({"run": run, "jobs": [{"id": run["id"], "status": "completed", "conclusion": "success"}],
                             "artifacts": [{"id": run["id"], "name": f"ci-shepherd-receipt-{run['id']}-1", "expired": False,
                                            "workflow_run": {"id": run["id"]}}],
                             "zip": archive({"receipt.json": receipt, "audit.json": audit})})
        self.run = {**self.run, "runId": str(int(self.run["runId"]) + 1)}

    def posts(self):
        return [call for call in self.calls if call[0] == "POST" and call[1].endswith("/tasks")]


class HTTPFixture:
    def __init__(self, service):
        self.service = service
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def do_PATCH(self):
                self.handle_request()

            def handle_request(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))) if "Content-Length" in self.headers else None
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                result = service.transport(self.command, self.path[1:], body)
                payload = result.payload
                if isinstance(payload, bytes):
                    # Downloads are served through the injected opener as bytes.
                    raw = payload
                else:
                    raw = json.dumps(payload).encode()
                self.send_response(result.status)
                for key, value in result.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def open(self, request, timeout):
        endpoint = urlparse(request.full_url).path
        query = urlparse(request.full_url).query
        rewritten = Request(f"http://127.0.0.1:{self.server.server_port}" + endpoint + ("?" + query if query else ""),
                            data=request.data, headers=dict(request.headers), method=request.method)
        return urlopen(rewritten, timeout=timeout)


class LiveTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = FakeService()
        self.directory = self.work / "prepared"

    def prepare(self, mode="live"):
        return hosted.prepare(self.directory, mode, self.service.run, transport=self.service.transport, host_check=lambda run: None)

    def apply(self, packet, action="repair-pr", **kwargs):
        feedback_ids = [item["id"] for item in packet["observation"]["subjects"][0]["feedback"] if item["id"].startswith("ci-")]
        decision = reconciliation_decision(packet, action, {"feedbackIds": feedback_ids} if action == "repair-pr" else None)
        evidence = self.work / ("evidence-" + str(len(list(self.work.glob("evidence-*")))) + ".json")
        output = evidence.with_name("output-" + evidence.name)
        receipt = evidence.with_name("receipt-" + evidence.name)
        contracts.write_json(evidence, reconciliation_evidence(decision))
        contracts.write_json(output, {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        result = hosted.apply(self.directory / "trusted", evidence, output, receipt, self.service.run,
                              transport=self.service.transport, host_check=lambda run: None, **kwargs)
        return result, receipt

    def next_run(self, receipt):
        audit = contracts.read_json(self.work / "audit.json")
        self.service.add_history(receipt, audit)
        self.directory = self.work / ("prepared-" + self.service.run["runId"])

    def test_real_ci_evidence_can_select_repair_without_synthetic_comment(self):
        packet, envelope, prompt = self.prepare()
        self.assertEqual(self.service.comments, [])
        self.assertEqual(packet["basis"]["feedback"], [{"id": "ci-10-20", "revision": live.INITIAL_HEAD + ":1", "state": "open"}])
        self.assertEqual(envelope["context"]["feedback"][0]["body"], self.service.logs.decode())
        self.assertIn("AssertionError:", prompt)
        self.assertEqual({method for method, _, _ in self.service.calls}, {"GET"})
        result, _ = self.apply(packet)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)
        body = self.service.posts()[0][2]
        self.assertEqual(set(body), {"prompt", "base_ref", "head_ref", "create_pull_request"})
        self.assertEqual((body["base_ref"], body["head_ref"], body["create_pull_request"]), (live.BASE, live.HEAD, False))
        self.assertIn(
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com> as its final trailer.",
            body["prompt"],
        )
        self.assertEqual(result["tasks"][0]["sessions"][0]["model"], "actual-server-model")
        self.assertEqual(result["tasks"][0]["sessions"][0]["usage"]["displayAmount"], 1.5)
        self.assertFalse(result["gate"]["ready"])
        record = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(record["repairBatches"], 1)
        self.assertEqual(record["operations"][0]["state"], "confirmed")
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual([attempt["kind"] for attempt in audit["attempts"]], ["status"] * 4 + ["task", "task-response", "status"])

    def task_with_pull_artifact(self, global_id):
        trial = {"trialId": "8ac4f956-3cd7-42b9-bd69-546b220b128f",
                 "trialStartedAt": "2026-10-04T04:13:32.988354Z", "expiresAt": "2026-10-05T04:13:32.988354Z"}
        correlation = {"root": live.ROOT, "trial": trial,
                       "operationId": "1d681347-69c8-4fe5-ba17-24aa6cd7f238", "sourceHead": live.INITIAL_HEAD}
        task = self.service.task_value("task-1", live.CORRELATION + receipts.canonical(correlation))
        data = {"id": live.PR_ID}
        if global_id != "absent":
            data["global_id"] = global_id
        task["artifacts"].append({"provider": "github", "type": "pull", "data": data})
        self.service.tasks[task["id"]] = task
        return task, correlation

    def test_optional_blank_pull_global_id_requires_independent_rest_identity(self):
        for global_id in ("absent", "", live.PR_NODE):
            with self.subTest(global_id=global_id):
                self.service = FakeService()
                task, expected = self.task_with_pull_artifact(global_id)
                github = live.FixtureGitHub(self.service.transport, self.service.run)
                failure, result = None, None
                try:
                    result = github.task(task["id"])
                except ValueError as error:
                    failure = str(error)
                self.assertIsNone(failure, "Supported optional/blank task node identity rejected: " + str(failure))
                self.assertEqual(result[1], expected)
                self.assertIn(("GET", f"repos/{live.REPOSITORY}/pulls/121", None), self.service.calls)
                self.assertEqual(self.service.posts(), [])

    def test_pull_artifact_wrong_ids_mapping_or_ambiguous_correlation_block(self):
        for change in ("global-id", "null-global-id", "database-id", "float-database-id", "rest-node",
                       "rest-ref", "creator", "session-repository", "two-correlations", "duplicate-marker"):
            with self.subTest(change=change):
                self.service = FakeService()
                task, correlation = self.task_with_pull_artifact("absent")
                data = task["artifacts"][-1]["data"]
                if change == "global-id":
                    data["global_id"] = "PR_wrong"
                elif change == "null-global-id":
                    data["global_id"] = None
                elif change == "database-id":
                    data["id"] += 1
                elif change == "float-database-id":
                    data["id"] = float(live.PR_ID)
                elif change == "rest-node":
                    self.service.pr["node_id"] = "PR_wrong"
                elif change == "rest-ref":
                    self.service.pr["head"]["ref"] = "different-branch"
                elif change == "creator":
                    task["creator"] = {"id": 999, "login": "radical"}
                elif change == "session-repository":
                    task["sessions"][0]["repository"]["id"] += 1
                else:
                    second = deepcopy(correlation)
                    if change == "two-correlations":
                        second["operationId"] = "a8b5ce20-6eca-487c-831e-6ac596bf9ba6"
                    task["sessions"][0]["prompt"] += "\n" + live.CORRELATION + receipts.canonical(second)
                github = live.FixtureGitHub(self.service.transport, self.service.run)
                with self.assertRaises((ValueError, KeyError)):
                    github.task(task["id"])
                self.assertEqual(self.service.posts(), [])

    def test_post_send_invalid_artifact_retains_consumed_capacity_without_retry(self):
        original = self.service.task_value

        def invalid_task(task_id, prompt):
            task = original(task_id, prompt)
            task["artifacts"].append({"provider": "github", "type": "pull",
                                       "data": {"id": live.PR_ID, "global_id": "PR_wrong"}})
            return task

        self.service.task_value = invalid_task
        packet, _, _ = self.prepare()
        with self.assertRaisesRegex(ValueError, "artifact mismatch"):
            self.apply(packet)
        canonical = receipts.parse_body(self.service.comments[0]["body"])
        self.assertEqual(canonical["repairBatches"], 1)
        self.assertEqual(canonical["operations"][0]["state"], "consumed")
        self.assertIsNone(canonical["operations"][0]["result"])
        self.assertEqual(len(self.service.posts()), 1)
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual(audit["phase"], "failed")
        self.assertIn({"kind": "task-response", "operationId": canonical["operations"][0]["id"],
                       "status": 201, "taskId": "task-1"}, audit["attempts"])

    def test_delayed_same_task_get_confirmation_never_reposts(self):
        original_task = self.service.task_value

        def task_value(task_id, prompt):
            task = original_task(task_id, prompt)
            task["artifacts"].append({"provider": "github", "type": "pull",
                                       "data": {"id": live.PR_ID, "global_id": live.PR_NODE}})
            return task

        self.service.task_value = task_value
        original_transport = self.service.transport
        task_reads = []

        def transport(method, endpoint, body):
            response = original_transport(method, endpoint, body)
            if method == "GET" and "/tasks/" in endpoint:
                task_reads.append(endpoint)
                if len(task_reads) == 1:
                    attempts = contracts.read_json(self.work / "audit.json")["attempts"]
                    self.assertEqual(attempts[-1]["kind"], "task-response")
                    self.assertEqual(attempts[-1]["status"], 201)
                    self.assertEqual(attempts[-1]["taskId"], "task-1")
                    response.payload["artifacts"][-1]["data"]["global_id"] = "PR_unverified"
            return response

        self.service.transport = transport
        packet, _, _ = self.prepare()
        result, _ = self.apply(packet)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["operation"]["result"], {"id": "task-1", "kind": "worker"})
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(set(task_reads), {f"agents/repos/{live.REPOSITORY}/tasks/task-1"})
        self.assertGreater(len(task_reads), 1)
        self.assertLessEqual(len(task_reads), 6)
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"])["repairBatches"], 1)

    def test_both_archive_lanes_are_explicit_and_missing_visibility_blocks(self):
        for lane in ("true", "false"):
            with self.subTest(lane=lane):
                self.directory = self.work / ("prepared-" + lane)
                self.service.incomplete = "is_archived=" + lane
                with self.assertRaises(IncompleteInventory):
                    self.prepare()
                self.assertEqual(self.service.posts(), [])
                self.assertEqual(contracts.read_json(self.directory / "audit.json")["phase"], "failed")
        self.service.incomplete = None
        self.directory = self.work / "good"
        self.prepare()
        lanes = {parse_qs(urlparse(path).query)["is_archived"][0] for method, path, _ in self.service.calls
                 if "/tasks?" in path}
        self.assertEqual(lanes, {"false", "true"})

    def test_public_mapping_actor_and_missing_task_status_fail_closed(self):
        bad = [lambda service: service.pr["head"].update(ref="other"),
               lambda service: service.pr["base"].update(ref="other"),
               lambda service: service.actor.update(login="someone"),
               lambda service: service.tasks.update({"unavailable": {"id": "unavailable", "created_at": live.CREATED_AT}})]
        for index, change in enumerate(bad):
            with self.subTest(index=index):
                self.service = FakeService()
                change(self.service)
                self.directory = self.work / str(index)
                with self.assertRaises((ValueError, KeyError)):
                    self.prepare()
                self.assertEqual(self.service.posts(), [])

    def test_lost_post_and_lost_publication_recover_without_duplicate(self):
        self.service.task_loss = True
        self.service.comment_loss = "after"
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.assertEqual(first["outcome"], "confirmed")
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(len(self.service.comments), 1)
        self.next_run(first)
        packet, _, _ = self.prepare()
        result, _ = self.apply(packet, "wait")
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(len(self.service.posts()), 1)

    def test_delayed_lost_post_recovers_on_fresh_wait_without_post_or_budget_reset(self):
        self.service.task_loss = True
        self.service.hide_tasks = True
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.assertEqual(first["outcome"], "needs-human")
        self.assertEqual(len(self.service.posts()), 1)
        self.next_run(first)
        self.service.hide_tasks = False
        self.service.pr["head"]["sha"] = "c" * 40
        packet, _, _ = self.prepare()
        result, _ = self.apply(packet, "wait")
        self.assertTrue(result["recovered"])
        self.assertEqual(result["effects"], [])
        self.assertEqual(result["operation"]["result"], {"id": "task-1", "kind": "worker"})
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"])["repairBatches"], 1)

    def test_known_task_rejection_spends_reservation_without_retry(self):
        self.service.task_reject = True
        packet, _, _ = self.prepare()
        result, _ = self.apply(packet)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(receipts.parse_body(self.service.comments[0]["body"])["repairBatches"], 1)
        audit = contracts.read_json(self.work / "audit.json")
        self.assertEqual(next(item for item in audit["attempts"] if item["kind"] == "task-rejected")["error"], "HTTP 403")

    def test_lost_status_without_publication_never_retries_or_restarts(self):
        packet, _, _ = self.prepare()
        self.service.comment_loss = "before"
        with self.assertRaisesRegex(ValueError, "missing|uncertain"):
            self.apply(packet)
        self.assertEqual(len([call for call in self.service.calls if call[0] == "POST"]), 1)
        self.assertEqual(self.service.posts(), [])
        self.assertEqual(contracts.read_json(self.work / "audit.json")["phase"], "failed")

    def test_active_task_new_packet_cannot_duplicate_even_if_agent_requests_repair(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        packet, _, _ = self.prepare()
        result, _ = self.apply(packet)
        self.assertEqual(result["outcome"], "replay")
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(result["tasks"][0]["state"], "queued")

    def test_all_explicit_task_states_and_unknown_hold_capacity(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        for state in sorted(live.TASK_STATES | {"future_state"}):
            with self.subTest(state=state):
                task = self.service.tasks["task-1"]
                task["state"] = state
                task["sessions"][0]["state"] = state
                adapter = live.FixtureGitHub(self.service.transport, self.service.run)
                snapshot = adapter.refresh(live.ROOT)
                expected = state if state in live.TASK_STATES else "unknown"
                self.assertEqual(snapshot["workers"][0]["state"], expected)
                self.assertFalse(adapter.context["gate"]["ready"])

    def test_head_feedback_takeover_and_authority_change_before_dispatch_make_zero_posts(self):
        for index, change in enumerate([
            lambda service: service.pr["head"].update(sha="c" * 40),
            lambda service: service.pr["labels"].append({"name": "shepherd-hands-off"}),
            lambda service: service.pr.update(labels=[]),
            lambda service: service.reviews.append({"id": 9, "body": "new feedback", "user": service.actor, "submitted_at": live.CREATED_AT}),
        ]):
            with self.subTest(index=index):
                self.service = FakeService()
                self.directory = self.work / str(index)
                packet, _, _ = self.prepare()
                change(self.service)
                with self.assertRaises(ValueError):
                    self.apply(packet)
                self.assertEqual(self.service.posts(), [])
        self.service = FakeService()
        self.directory = self.work / "last-mapping"
        packet, _, _ = self.prepare()

        def takeover(service, method, endpoint):
            if endpoint.endswith("/pulls/121") and service.comments:
                record = receipts.parse_body(service.comments[0]["body"])
                if record["operations"] and record["operations"][0]["state"] == "consumed":
                    service.pr["labels"].append({"name": "shepherd-hands-off"})
        self.service.before_request = takeover
        with self.assertRaisesRegex(ValueError, "hands-off"):
            self.apply(packet)
        self.assertEqual(self.service.posts(), [])

    def test_dispatch_mapping_delay_cannot_post_after_packet_expiry(self):
        from datetime import timedelta
        for scenario in ("before-expiry", "at-expiry", "after-expiry", "rollback"):
            with self.subTest(scenario=scenario):
                self.service = FakeService()
                self.directory = self.work / ("mapping-delay-" + scenario)
                packet, _, _ = self.prepare()
                prepared = live.issue_pr.timestamp(packet["preparedAt"])
                valid_until = live.issue_pr.timestamp(packet["validUntil"])
                delayed_at = {
                    "before-expiry": prepared + timedelta(seconds=1),
                    "at-expiry": valid_until,
                    "after-expiry": valid_until + timedelta(seconds=1),
                    "rollback": prepared - timedelta(seconds=1),
                }[scenario]
                now = [prepared]
                post_clocks = []
                dispatching, checking_guard = [False], [False]

                def delay_mapping(service, method, endpoint):
                    if method == "POST":
                        post_clocks.append((endpoint, now[0]))
                    if method == "GET" and endpoint.endswith("/pulls/121") and dispatching[0] and not checking_guard[0]:
                        now[0] = delayed_at

                original_bind = live.ExistingPRExecutor.bind

                def bind_guard(executor, operation, trial, snapshot, guard):
                    def checking():
                        checking_guard[0] = True
                        try:
                            return guard()
                        finally:
                            checking_guard[0] = False
                    original_bind(executor, operation, trial, snapshot, checking)
                    dispatching[0] = True

                self.service.before_request = delay_mapping
                with patch.object(live, "clock", side_effect=lambda: now[0]):
                    with patch.object(live.ExistingPRExecutor, "bind", bind_guard):
                        if scenario == "before-expiry":
                            result, _ = self.apply(packet)
                            self.assertEqual(result["outcome"], "confirmed")
                        else:
                            with self.assertRaisesRegex(ValueError, "rollback|expired|expiry"):
                                self.apply(packet)
                self.assertTrue(post_clocks)
                for endpoint, observed_at in post_clocks:
                    self.assertGreaterEqual(observed_at, prepared, endpoint)
                    self.assertLess(observed_at, valid_until, endpoint)
                if scenario == "before-expiry":
                    self.assertEqual(len(self.service.posts()), 1)
                    self.assertEqual(next(observed_at for endpoint, observed_at in post_clocks if endpoint.endswith("/tasks")), delayed_at)
                else:
                    self.assertEqual(self.service.posts(), [])

    def test_unassociated_newest_completed_session_retains_unknown_capacity(self):
        for refs in ((live.HEAD, live.BASE), ("different-branch", "release/other")):
            with self.subTest(refs=refs):
                self.service = FakeService()
                self.directory = self.work / refs[0]
                packet, _, _ = self.prepare()
                first, _ = self.apply(packet)
                self.next_run(first)
                self.directory = self.work / (refs[0] + "-fresh")
                task = self.service.tasks["task-1"]
                newest = deepcopy(task["sessions"][0])
                newest.update(id="unrelated-newest-session", state="completed",
                              created_at="2026-10-04T05:00:00Z", prompt="Perform unrelated work",
                              head_ref=refs[0], base_ref=refs[1])
                task.update(state="completed", session_count=2)
                task["sessions"].append(newest)
                self.service.pr["head"]["sha"] = self.service.ci_head = "c" * 40
                self.service.ci_conclusion = "success"
                adapter = live.FixtureGitHub(self.service.transport, self.service.run, write=False)
                snapshot = adapter.refresh(live.ROOT)
                self.assertEqual(snapshot["workers"][0]["state"], "unknown")
                self.assertFalse(adapter.context["gate"]["ready"])
                self.assertEqual(task["sessions"][0]["state"], "queued")
                packet, envelope, _ = self.prepare()
                self.assertEqual(packet["observation"]["workers"][0]["state"], "unknown")
                self.assertEqual(envelope["context"]["tasks"][0]["state"], "unknown")
                self.assertFalse(envelope["context"]["gate"]["ready"])
                self.assertTrue(envelope["context"]["gate"]["ciPassed"])
                result, _ = self.apply(packet, "wait")
                self.assertFalse(result["gate"]["ready"])
                self.assertEqual(len(self.service.posts()), 1)

    def test_initial_head_green_without_resulting_push_never_passes_early_gate(self):
        self.service.ci_conclusion = "success"
        adapter = live.FixtureGitHub(self.service.transport, self.service.run, write=False)
        snapshot = adapter.refresh(live.ROOT)
        self.assertEqual(snapshot["workers"], [])
        self.assertIsNone(adapter.context.get("push"))
        self.assertFalse(adapter.context["gate"]["ready"])
        self.assertTrue(adapter.context["gate"]["ciPassed"])
        self.assertEqual({method for method, _, _ in self.service.calls}, {"GET"})
        packet, envelope, _ = self.prepare()
        self.assertEqual(envelope["context"]["sourceHead"], live.INITIAL_HEAD)
        self.assertEqual(envelope["context"]["tasks"], [])
        self.assertIsNone(envelope["context"].get("push"))
        self.assertEqual(envelope["context"]["gate"]["conclusion"], "success")
        self.assertFalse(envelope["context"]["gate"]["ready"])
        self.assertTrue(envelope["context"]["gate"]["ciPassed"])
        result, _ = self.apply(packet, "wait")
        self.assertFalse(result["gate"]["ready"])
        self.assertEqual(self.service.posts(), [])

    def test_independent_history_lost_record_failed_run_or_missing_audit_blocks(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        saved = deepcopy(self.service.history)
        comments = deepcopy(self.service.comments)
        for index in range(3):
            with self.subTest(index=index):
                self.service.history = deepcopy(saved)
                self.service.comments = deepcopy(comments)
                self.directory = self.work / ("blocked-" + str(index))
                if index == 0:
                    self.service.comments = []
                elif index == 1:
                    self.service.history[0]["jobs"][0]["conclusion"] = "cancelled"
                else:
                    self.service.history[0]["artifacts"] = []
                with self.assertRaisesRegex(ValueError, "missing|unverifiable|failed|cancelled"):
                    self.prepare()
                self.assertEqual(len(self.service.posts()), 1)

    def test_deleting_record_task_and_entire_prior_run_cannot_bootstrap_budget(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        self.service.comments = []
        self.service.tasks = {}
        self.service.history = []
        with self.assertRaisesRegex(IncompleteInventory, "sequence gap"):
            self.prepare()
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(self.service.comments, [])

    def test_hands_off_reports_existing_task_without_claiming_cancellation(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        self.service.pr["labels"].append({"name": "shepherd-hands-off"})
        before = len([call for call in self.service.calls if call[0] != "GET"])
        with self.assertRaisesRegex(ValueError, "hands-off"):
            self.prepare()
        observation = contracts.read_json(self.directory / "observation.json")
        self.assertEqual(observation["tasks"][0]["state"], "queued")
        self.assertEqual(observation["effects"], [])
        self.assertFalse(observation["authority"])
        self.assertEqual(before, len([call for call in self.service.calls if call[0] != "GET"]))

    def test_current_head_observation_never_uses_stale_green_or_task_claim(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        self.service.tasks["task-1"]["state"] = "completed"
        self.service.tasks["task-1"]["sessions"][0]["state"] = "completed"
        self.service.pr["head"]["sha"] = "c" * 40
        self.service.ci_conclusion = "success"
        packet, envelope, _ = self.prepare()
        self.assertEqual(envelope["context"]["gate"], {"state": "missing-current-head-ci", "headSha": "c" * 40, "ciPassed": False, "ready": False})
        result, _ = self.apply(packet, "wait")
        self.assertEqual(result["currentHead"], "c" * 40)
        self.assertFalse(result["gate"]["ready"])
        self.assertEqual(len(self.service.posts()), 1)
        self.service.ci_head = "c" * 40
        adapter = live.FixtureGitHub(self.service.transport, self.service.run)
        adapter.refresh(live.ROOT)
        self.assertTrue(adapter.context["gate"]["ready"])
        self.service.file_scope = False
        adapter.refresh(live.ROOT)
        self.assertFalse(adapter.context["gate"]["ready"])

    def test_zero_job_approval_blocked_pr_ci_allows_observe_wait_not_manual_green(self):
        self.service.pr["head"]["sha"] = self.service.ci_head = "c" * 40
        self.service.ci_conclusion = "action_required"
        self.service.ci_jobs = {"total_count": 0, "jobs": []}
        self.service.ci_extra_runs = [{"id": 11, "run_attempt": 1, "head_sha": self.service.ci_head,
                                      "path": live.FIXTURE_WORKFLOW, "event": "workflow_dispatch",
                                      "pull_requests": [], "status": "completed", "conclusion": "success"}]
        prepared, failure = None, None
        try:
            prepared = self.prepare("observe")
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Approval-blocked zero-job PR CI prevented observation: " + str(failure))
        packet, envelope, _ = prepared
        gate = {"headSha": self.service.ci_head, "runId": 10, "jobId": None, "state": "approval-blocked",
                "conclusion": "action_required", "jobConclusion": None, "ciPassed": False, "ready": False}
        self.assertEqual(envelope["context"]["gate"], gate)
        self.assertEqual(packet["observation"]["jobs"], [])
        self.assertEqual(packet["basis"]["feedback"], [])
        self.assertEqual(envelope["context"]["feedback"], [])
        result, _ = self.apply(packet, "wait")
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(result["gate"], gate)
        self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])
        self.assertEqual(self.service.comments, [])

    def test_zero_job_approval_exception_rejects_other_conclusions_and_unproven_inventories(self):
        for change in ("success", "failure", "cancelled", "unknown", "queued", "in_progress", "missing-count",
                       "wrong-count", "float-count", "boolean-count", "missing-count-next-page", "wrong-count-next-page",
                       "missing-jobs", "other-job", "ambiguous-job", "http"):
            with self.subTest(change=change):
                self.service = FakeService()
                self.service.ci_conclusion = "action_required"
                self.service.ci_jobs = {"total_count": 0, "jobs": []}
                if change in {"success", "failure", "cancelled", "unknown"}:
                    self.service.ci_conclusion = change
                elif change in {"queued", "in_progress"}:
                    self.service.ci_state = change
                elif change == "missing-count":
                    self.service.ci_jobs.pop("total_count")
                elif change in {"wrong-count", "float-count", "boolean-count"}:
                    self.service.ci_jobs["total_count"] = {"wrong-count": 1, "float-count": 0.0, "boolean-count": False}[change]
                elif change in {"missing-count-next-page", "wrong-count-next-page"}:
                    if change == "missing-count-next-page":
                        self.service.ci_jobs.pop("total_count")
                    else:
                        self.service.ci_jobs["total_count"] = 1
                    original = self.service.transport

                    def transport(method, endpoint, body):
                        response = original(method, endpoint, body)
                        if "/runs/10/attempts/1/jobs?" in endpoint:
                            if parse_qs(urlparse(endpoint).query)["page"] == ["1"]:
                                response.headers["Link"] = (
                                    f'<https://api.github.com/repos/{live.REPOSITORY}/actions/runs/10/attempts/1/jobs'
                                    '?per_page=100&page=2>; rel="next"')
                            else:
                                return Response({"total_count": 0, "jobs": []}, {})
                        return response

                    self.service.transport = transport
                elif change == "missing-jobs":
                    self.service.ci_jobs.pop("jobs")
                elif change in {"other-job", "ambiguous-job"}:
                    self.service.ci_jobs["jobs"] = [
                        {"id": 20 + index, "run_id": 10, "head_sha": self.service.ci_head,
                         "name": "different job" if change == "other-job" else live.JOB,
                         "status": "completed", "conclusion": "success"}
                        for index in range(1 if change == "other-job" else 2)]
                    self.service.ci_jobs["total_count"] = len(self.service.ci_jobs["jobs"])
                else:
                    self.service.incomplete = "/runs/10/attempts/1/jobs"
                github = live.FixtureGitHub(self.service.transport, self.service.run)
                with self.assertRaises(ValueError):
                    github.refresh(live.ROOT)
                self.assertEqual([call for call in self.service.calls if call[0] != "GET"], [])

    def test_observe_and_default_transport_do_not_write_or_start_trial(self):
        packet, _, _ = self.prepare("observe")
        result, _ = self.apply(packet)
        self.assertEqual(result["outcome"], "dry-run")
        self.assertEqual([method for method, _, _ in self.service.calls if method != "GET"], [])
        self.assertEqual(self.service.comments, [])
        packet, _, _ = hosted.prepare(self.work / "proof", "transport-proof", RUN,
                                     transport=lambda *args: self.fail("transport default touched API"),
                                     host_check=lambda *args: self.fail("transport default requested OIDC"))
        self.assertEqual(packet["kind"], "transport-proof")

    def test_terminal_task_with_nonterminal_latest_session_remains_unknown(self):
        packet, _, _ = self.prepare()
        first, _ = self.apply(packet)
        self.next_run(first)
        self.service.tasks["task-1"]["state"] = "completed"
        packet, envelope, _ = self.prepare()
        self.assertEqual(packet["observation"]["workers"][0]["state"], "unknown")
        self.assertEqual(envelope["context"]["tasks"][0]["state"], "unknown")
        self.assertFalse(envelope["context"]["gate"]["ready"])
        result, _ = self.apply(packet, "wait")
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(len(self.service.posts()), 1)

    def test_output_has_no_agent_api_payload_capability(self):
        packet, _, _ = self.prepare()
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["ci-10-20"], "prompt": "evil"})
        with self.assertRaisesRegex(ValueError, "exactly"):
            contracts.validate_reconciliation_decision(packet, decision, RUN)
        self.assertEqual(self.service.posts(), [])

    def test_fake_http_requests_capture_actual_path_headers_json_and_rejection(self):
        fixture = HTTPFixture(self.service)
        self.addCleanup(fixture.close)
        transport = live.HTTPTransport("test-only-selected-credential", write=True, opener=fixture)
        # Logs are normally redirected binary downloads; override this one GET
        # with test evidence while keeping task POST and identity reads real HTTP.
        def request(method, endpoint, body):
            if endpoint.endswith("/logs"):
                return self.service.transport(method, endpoint, body)
            return transport(method, endpoint, body)
        packet, _, _ = hosted.prepare(self.directory, "live", RUN, transport=request, host_check=lambda run: None)
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["ci-10-20"]})
        github = live.FixtureGitHub(request, RUN, write=True)
        scope = receipts.TrialScope(live.ROOT, None)
        executor = live.ExistingPRExecutor(github, packet, github.refresh(live.ROOT) and github.context)
        result = contracts.apply_reconciliation(packet, decision, RUN, github, live.clock, scope,
                                               executor=executor, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(result["outcome"], "confirmed")
        post = next(item for item in fixture.requests if item[0] == "POST" and "/tasks" in item[1])
        self.assertEqual(post[1], "/agents/repos/radical/aspire/tasks")
        self.assertEqual(post[2]["X-Github-Api-Version"], "2026-03-10")
        self.assertEqual(post[2]["Authorization"], "Bearer test-only-selected-credential")
        self.assertEqual(post[3]["create_pull_request"], False)
        self.assertEqual(post[3]["head_ref"], live.HEAD)
        with self.assertRaisesRegex(ValueError, "write"):
            live.HTTPTransport("test", opener=fixture)("POST", "agents/repos/radical/aspire/tasks", {})

    def test_compiled_prepare_collect_and_apply_execute_hosted_capability(self):
        fixture = HTTPFixture(self.service)
        self.addCleanup(fixture.close)
        source = Path(__file__).resolve().parents[1]
        scripts = self.work / ".github" / "workflows"
        scripts.mkdir(parents=True)
        (scripts / "ci-shepherd").symlink_to(source, target_is_directory=True)
        binaries = self.work / "bin"
        binaries.mkdir()
        shim = binaries / "python3"
        shim.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixture_host.py").read_text())
        shim.chmod(0o700)
        environment = {"PATH": str(binaries.resolve()) + ":" + os.defpath,
                       "GITHUB_REPOSITORY": RUN["repository"], "GITHUB_RUN_ID": RUN["runId"],
                       "GITHUB_RUN_ATTEMPT": RUN["runAttempt"], "GITHUB_WORKFLOW_SHA": RUN["workflowSha"],
                       "GITHUB_OUTPUT": str((self.work / "output").resolve()), "SHEPHERD_MODE": "live",
                       "CI_SHEPHERD_USER_TOKEN": "test-only-host-credential",
                       "TEST_HTTP_PORT": str(fixture.server.server_port), "TEST_SOURCE": str(source)}
        # The hostile log remains prompt data, never shell source.
        self.service.logs += b"$(touch should-not-exist); ' ; echo unsafe\n"
        pre = compiled_step("Prepare host-owned envelope")
        result = subprocess.run(["bash", "-c", pre["run"]], cwd=self.work, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifacts = self.work / "artifacts" / "ci-shepherd"
        prepared = artifacts / "prepared" / "trusted"
        packet = contracts.read_json(prepared / "packet.json")
        envelope = contracts.read_json(prepared / "envelope.json")
        self.assertNotIn("test-only-host-credential", json.dumps(envelope))
        self.assertFalse((self.work / "should-not-exist").exists())
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["ci-10-20"]})
        evidence = reconciliation_evidence(decision)
        engine = compiled_step("Execute GitHub Copilot CLI")
        sessions = Path(compiled_environment(engine, self.work)["AWF_SESSION_STATE_DIR"]) / evidence["sessionId"]
        sessions.mkdir(parents=True)
        (sessions / "events.jsonl").write_text("\n".join(json.dumps(event) for event in evidence["events"] if event["type"] != "result"))
        logs = artifacts / "host-logs"
        logs.mkdir()
        (logs / "native.log").write_text(evidence["debug"])
        collector = compiled_step("Collect host process and session evidence")
        result = subprocess.run(["bash", "-c", collector["run"]], cwd=self.work,
                                env={**environment, **compiled_environment(collector, self.work)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        import shutil
        shutil.copytree(prepared, artifacts / "trusted")
        (artifacts / "evidence").mkdir()
        shutil.copyfile(artifacts / "evidence.json", artifacts / "evidence" / "evidence.json")
        agent_output = self.work / "agent-output.json"
        contracts.write_json(agent_output, {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        apply = compiled_step("Guarded host apply")
        result = subprocess.run(["bash", "-c", apply["run"]], cwd=self.work,
                                env={**environment, "GH_AW_AGENT_OUTPUT": str(agent_output.resolve())},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = contracts.read_json(artifacts / "receipt.json")
        self.assertEqual(receipt["effects"], [{"id": "task-1", "kind": "worker"}])
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual(contracts.read_json(artifacts / "audit.json")["phase"], "complete")
        self.assertFalse((self.work / "should-not-exist").exists())

    def test_compiled_prepare_reports_bounded_quota_wait_without_effects(self):
        original = self.service.transport
        def exhausted(method, endpoint, body):
            response = original(method, endpoint, body)
            if method == "GET" and endpoint.startswith("agents/"):
                return Response(response.payload, {"X-RateLimit-Resource": "mission_control",
                                                  "X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0",
                                                  "X-RateLimit-Reset": str(int(live.clock().timestamp()) + 3600)})
            return response
        self.service.transport = exhausted
        fixture = HTTPFixture(self.service)
        self.addCleanup(fixture.close)
        source = Path(__file__).resolve().parents[1]
        scripts = self.work / ".github" / "workflows"
        scripts.mkdir(parents=True)
        (scripts / "ci-shepherd").symlink_to(source, target_is_directory=True)
        binaries = self.work / "bin"
        binaries.mkdir()
        shim = binaries / "python3"
        shim.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixture_host.py").read_text())
        shim.chmod(0o700)
        environment = {"PATH": str(binaries.resolve()) + ":" + os.defpath,
                       "GITHUB_REPOSITORY": RUN["repository"], "GITHUB_RUN_ID": RUN["runId"],
                       "GITHUB_RUN_ATTEMPT": RUN["runAttempt"], "GITHUB_WORKFLOW_SHA": RUN["workflowSha"],
                       "GITHUB_OUTPUT": str((self.work / "output").resolve()), "SHEPHERD_MODE": "observe",
                       "CI_SHEPHERD_USER_TOKEN": "test-only-host-credential",
                       "TEST_HTTP_PORT": str(fixture.server.server_port), "TEST_SOURCE": str(source)}
        step = compiled_step("Prepare host-owned envelope")
        result = subprocess.run(["bash", "-c", step["run"]], cwd=self.work, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        audit = contracts.read_json(self.work / "artifacts/ci-shepherd/prepared/audit.json")
        self.assertEqual(audit["phase"], "failed")
        self.assertEqual(audit["attempts"], [])
        self.assertIn("quota wait exceeds 180s", result.stderr)
        self.assertEqual({method for method, _, _ in self.service.calls}, {"GET"})

    def test_task_http_rejections_and_uncertain_errors_are_distinct_without_retries(self):
        class Reject:
            def __init__(self, code):
                self.code = code
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, self.code, "fixture", {}, None)
        for code in (400, 401, 403, 422, 500, 429):
            opener = Reject(code)
            expected = RejectedEffect if code in {400, 401, 403, 422} else LostResponse
            with self.subTest(code=code), self.assertRaises(expected):
                live.HTTPTransport("test", write=True, opener=opener)("POST", "agents/repos/radical/aspire/tasks", {})
            self.assertEqual(opener.calls, 1)

    def test_storage_redirect_never_forwards_selected_user_credential(self):
        class Redirect:
            def __init__(self, location):
                self.location, self.requests = location, []

            def open(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise HTTPError(request.full_url, 302, "fixture", {"Location": self.location}, io.BytesIO())
                result = io.BytesIO(b"fixture log bytes")
                result.status = 200
                return result
        opener = Redirect("https://fixture.blob.core.windows.net/log?sig=test-only")
        response = live.HTTPTransport("test-user-credential", opener=opener)(
            "GET", "repos/radical/aspire/actions/jobs/20/logs", None)
        self.assertEqual(response.payload, b"fixture log bytes")
        self.assertEqual(opener.requests[0].headers["Authorization"], "Bearer test-user-credential")
        self.assertEqual(opener.requests[1].headers, {})
        opener = Redirect("https://example.com/steal")
        with self.assertRaisesRegex(IncompleteInventory, "untrusted"):
            live.HTTPTransport("test", opener=opener)("GET", "repos/radical/aspire/actions/jobs/20/logs", None)
        self.assertEqual(len(opener.requests), 1)

    def test_bounded_pagination_and_foreign_link_are_not_complete_history(self):
        for link in ('<https://example.com/agents/repos/radical/aspire/tasks?page=2>; rel="next"',
                     '<https://api.github.com/agents/repos/radical/aspire/tasks?is_archived=true&per_page=100&page=2>; rel="next"'):
            with self.subTest(link=link), self.assertRaises(IncompleteInventory):
                live.API(lambda *args: Response({"tasks": []}, {"Link": link})).pages(
                    "agents/repos/radical/aspire/tasks", key="tasks", query={"is_archived": "false"})
        with self.assertRaisesRegex(IncompleteInventory, "limit"):
            live.API(lambda *args: Response({"tasks": [{"id": str(index)} for index in range(100)]}, {}),
                     max_pages=1).pages("agents/repos/radical/aspire/tasks", key="tasks", query={"is_archived": "false"})

    def test_invalid_host_evidence_never_writes_status_or_posts(self):
        packet, _, _ = self.prepare()
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["ci-10-20"]})
        evidence = reconciliation_evidence(decision)
        evidence["debug"] = ""
        evidence_path = self.work / "bad-evidence.json"
        decision_path = self.work / "decision.json"
        contracts.write_json(evidence_path, evidence)
        contracts.write_json(decision_path, {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        with self.assertRaisesRegex(ValueError, "effective tool"):
            hosted.apply(self.directory / "trusted", evidence_path, decision_path, self.work / "receipt.json", RUN,
                         transport=self.service.transport, host_check=lambda run: None)
        self.assertEqual(self.service.comments, [])
        self.assertEqual(self.service.posts(), [])

    def test_apply_clock_cannot_roll_back_after_authenticated_host_identity_read(self):
        from datetime import timedelta
        packet, _, _ = self.prepare()
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["ci-10-20"]})
        evidence = self.work / "clock-evidence.json"
        output = self.work / "clock-output.json"
        contracts.write_json(evidence, reconciliation_evidence(decision))
        contracts.write_json(output, {"items": [{"type": "submit_decision", "decision": json.dumps(decision)}], "errors": []})
        prepared = live.issue_pr.timestamp(packet["preparedAt"])
        identity_time = live.issue_pr.stamp(prepared + timedelta(minutes=1))
        with patch.object(live, "clock", return_value=prepared + timedelta(seconds=30)):
            with self.assertRaisesRegex(ValueError, "rollback"):
                hosted.apply(self.directory / "trusted", evidence, output, self.work / "clock-receipt.json", RUN,
                             transport=self.service.transport,
                             host_check=lambda run: {"hostObservedAt": identity_time})
        self.assertEqual(self.service.posts(), [])
        self.assertEqual(self.service.comments, [])


class HostedIdentityTests(unittest.TestCase):
    def test_local_live_and_agent_claims_do_not_create_host_capability(self):
        for environment in ({}, {"GITHUB_ACTIONS": "true", "sessionId": "fake"},
                            {"GITHUB_ACTIONS": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fake",
                             "ACTIONS_ID_TOKEN_REQUEST_URL": "http://example.com"}):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, "forbidden|required"):
                hosted.require_host(RUN, environment=environment)

    def test_host_oidc_claims_are_obtained_from_isolated_tls_service(self):
        now = int(live.clock().timestamp())
        claims = {"aud": "ci-shepherd-existing-pr", "iss": "https://token.actions.githubusercontent.com",
                  "repository": live.REPOSITORY, "repository_id": str(live.REPOSITORY_ID), "run_id": RUN["runId"],
                  "run_attempt": "1", "workflow_sha": RUN["workflowSha"], "event_name": "workflow_dispatch",
                  "actor": "radical", "runner_environment": "github-hosted",
                  "workflow_ref": live.REPOSITORY + "/" + live.WORKFLOW + "@refs/heads/main",
                  "nbf": now - 10, "exp": now + 60}

        class Opener:
            def open(self, request, timeout):
                self.request = request
                payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                result = io.BytesIO(json.dumps({"value": "header." + payload + ".signature"}).encode())
                result.status = 200
                return result
        opener = Opener()
        environment = {"GITHUB_ACTIONS": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "host-only",
                       "ACTIONS_ID_TOKEN_REQUEST_URL": "https://runner.actions.githubusercontent.com/idtoken?api-version=2.0"}
        identity = hosted.require_host(RUN, environment=environment, opener=opener)
        self.assertEqual({key: identity[key] for key in claims}, claims)
        self.assertIsInstance(identity["hostObservedAt"], str)
        self.assertEqual(opener.request.headers["Authorization"], "Bearer host-only")
        claims["workflow_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "identity"):
            hosted.require_host(RUN, environment=environment, opener=opener)


if __name__ == "__main__":
    unittest.main()
