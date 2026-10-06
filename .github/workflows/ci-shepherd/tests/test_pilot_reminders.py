from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import unittest
import uuid
from unittest.mock import patch

from helpers import WorkspaceTest, compiled_step, reconciliation_evidence
from github import IncompleteInventory, LostResponse, Response
from test_pilot import RUN
from test_pilot_github import ACTOR, pr
import test_pilot_tracked_only as fixtures
import hosted
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_reminders as reminders
import pilot_state as state


class ReminderTests(WorkspaceTest, unittest.TestCase):
    def setup_api(self, binding=bindings.UPSTREAM):
        api, transport = fixtures.TrackedOnlyTests().api(binding)
        original = api.transport
        self.posts = []
        self.send_outcome = "accepted"
        self.runs_response = None

        def send(method, endpoint, body):
            if method == "GET" and "/actions/runs?" in endpoint and self.runs_response is not None:
                if isinstance(self.runs_response, Exception):
                    raise self.runs_response
                return self.runs_response(endpoint)
            if method == "POST" and endpoint.endswith("/comments") and body["body"].startswith("[automated] @radical"):
                self.posts.append(body["body"])
                comment = {"id": 800 + len(self.posts), "user": ACTOR, "body": body["body"],
                           "updated_at": "2026-10-04T00:00:00Z"}
                if self.send_outcome != "lost-no-receipt":
                    transport.values.setdefault(endpoint, []).append(comment)
                if self.send_outcome.startswith("lost"):
                    raise LostResponse("notification outcome unknown")
                return Response(deepcopy(comment), {}, 201)
            return original(method, endpoint, body)

        api.transport = api.api.transport = send
        api.read_authority()
        number = binding.subject or 7
        value = transport.values[f"{api.prefix}/pulls/{number}"]
        chain = state.adopt(api.ledger, number, "pr", value["node_id"])
        api.persist()
        self.api, self.transport, self.chain = api, transport, chain
        self.run = {"id": 42, "head_sha": value["head"]["sha"], "status": "completed", "conclusion": "action_required",
                    "repository": {"id": api.repository_id, "full_name": api.repository},
                    "html_url": f"https://github.com/{api.repository}/actions/runs/42"}
        self.set_runs([self.run])
        return api, transport, chain

    def set_runs(self, runs):
        self.transport.values[f"{self.api.prefix}/actions/runs"] = {"workflow_runs": runs, "total_count": len(runs)}

    def tick(self):
        log = io.StringIO()
        with redirect_stdout(log):
            observation = self.api.observe(self.chain)
            reminders.process(self.api, self.chain, observation, self.api.clock())
        return observation, log.getvalue()

    def restart(self):
        prior = self.api
        self.api = github.PilotGitHub(prior.transport, prior.tracker, prior.authority_id, prior.tracker_node,
                                     write=True, binding=prior.binding)
        self.api.clock = prior.clock
        self.api.read_authority()
        self.chain = self.api.ledger["chains"][0]

    def setup_issue_api(self, number=8):
        # A childless issue-phase chain: origin only, no child PR bound yet.
        api, transport = fixtures.TrackedOnlyTests().api(bindings.FORK)
        transport.values[f"{api.prefix}/issues/{number}"] = {
            "number": number, "node_id": "NODE" + str(number), "state": "open",
            "labels": [{"name": "shepherd-adopted"}], "title": "Fix normalization", "body": "Steps to repro"}
        transport.values[f"{api.prefix}/issues/{number}/comments"] = []
        api.read_authority()
        chain = state.adopt(api.ledger, number, "issue", "NODE" + str(number))
        api.persist()
        self.api, self.transport, self.chain = api, transport, chain
        self.posts = []
        original = api.transport

        def send(method, endpoint, body):
            if method == "POST" and endpoint.endswith("/comments") and body["body"].startswith("[automated] @radical"):
                self.posts.append(body["body"])
                comment = {"id": 900 + len(self.posts), "user": ACTOR, "body": body["body"],
                           "updated_at": "2026-10-04T00:00:00Z"}
                transport.values.setdefault(endpoint, []).append(comment)
                return Response(deepcopy(comment), {}, 201)
            return original(method, endpoint, body)

        api.transport = api.api.transport = send
        return api, transport, chain

    def setup_issue_worker_api(self, task_id):
        api, transport, chain = self.setup_issue_api(8)
        issue = transport.values[f"{api.prefix}/issues/8"]
        issue["id"] = 1008
        transport.values[f"{api.prefix}/issues"] = [issue]
        observed = api.observe(chain)
        operation = state.reserve(api.ledger, chain, github.fingerprint(observed) + ":round:1", api.clock(), local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(api.ledger, chain, operation, api.clock())
        state.sent(operation)
        operation["taskId"] = task_id
        task = {"id": task_id, "state": "completed", "repository": {"id": api.repository_id}, "creator": {"id": 1472},
                "session_count": 1, "updated_at": "2026-10-04T00:00:00Z", "artifacts": [],
                "sessions": [{"id": "SESSION8", "task_id": task_id, "state": "completed",
                    "repository": {"id": api.repository_id}, "user": {"id": 1472},
                    "base_ref": "main", "head_ref": "work",
                    "prompt": github.CORRELATION + json.dumps(
                        {"chain": chain["id"], "operation": operation["id"], "origin": 8}),
                    "usage": {"type": "ai_credits", "amount": 1500000000}}]}
        transport.values[f"agents/repos/{api.repository}/tasks/{task_id}"] = task
        api.persist()
        return api, transport, chain, operation, task

    def test_threshold_59_60_restart_dedup_and_no_action_accounting(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                self.setup_api(binding)
                before = deepcopy(self.chain)
                observed, log = self.tick()
                self.assertFalse(observed["ready"])
                self.assertFalse(observed["actionable"])
                self.assertIn("delay 60s", log)
                timestamp = self.chain["reminder"]["firstObservedAt"]
                self.api.clock.advance(seconds=59)
                self.restart()
                _, log = self.tick()
                self.assertEqual([], self.posts)
                self.assertIn("remaining 1s", log)
                self.assertEqual(timestamp, self.chain["reminder"]["firstObservedAt"])
                self.api.clock.advance(seconds=1)
                _, log = self.tick()
                self.assertEqual(1, len(self.posts))
                self.assertIn("notified @radical", log)
                self.assertIn(self.run["html_url"], self.posts[0])
                self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
                self.api.clock.advance(minutes=2)
                self.restart()
                self.tick()
                self.assertEqual(1, len(self.posts))
                self.assertEqual(before, {key: value for key, value in self.chain.items() if key != "reminder"})
                self.assertEqual(0, state.worker_slots(self.api.ledger))
                self.assertEqual(0, state.chain_spend(self.chain))

    def test_uncertain_copilot_review_posts_one_delayed_owner_notice_without_retry_or_inference(self):
        self.setup_api(bindings.FORK)
        self.set_runs([])
        head = self.transport.values[f"{self.api.prefix}/pulls/7"]["head"]["sha"]
        self.transport.values[f"{self.api.prefix}/pulls/7/comments"] = []
        self.transport.values[f"{self.api.prefix}/issues/7/comments"] = []
        self.transport.values[f"{self.api.prefix}/commits/{head}/status"] = {
            "statuses": [{"id": 20, "context": "test", "state": "success"}], "state": "success"}
        original = self.api.transport
        requests = []

        def lose(method, endpoint, body):
            if method == "POST" and endpoint.endswith("/requested_reviewers"):
                requests.append(body)
                raise LostResponse("review outcome unknown")
            return original(method, endpoint, body)

        self.api.transport = self.api.api.transport = lose
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.api, RUN, self.api.clock(), present=False))
        history = deepcopy(self.chain["reviews"])
        self.tick()
        self.api.clock.advance(seconds=59)
        self.restart()
        self.tick()
        self.assertEqual([], self.posts)
        self.api.clock.advance(seconds=1)
        self.tick()
        self.assertEqual(1, len(self.posts))
        self.assertTrue(reminders.valid_body(self.posts[0], self.api.repository, 7))
        self.assertIn("Copilot review", self.posts[0])
        self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
        self.api.clock.advance(days=1)
        self.restart()
        self.tick()
        self.assertEqual((1, 1, 0, 30), (len(self.posts), len(requests), self.chain["rounds"],
                                       state.chain_spend(self.chain)))
        self.assertEqual(history, self.chain["reviews"])

    def test_prepare_zero_job_approval_blocks_paid_repair_and_approval_feedback(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                self.setup_api(binding)
                head = self.run["head_sha"]
                self.transport.values[f"{self.api.prefix}/commits/{head}/check-runs"] = {
                    "total_count": 1, "check_runs": [{"id": 3, "head_sha": head, "status": "completed",
                        "conclusion": "action_required", "name": "Approval", "html_url": self.run["html_url"]}]}
                with patch.object(self.api, "publish_status"), redirect_stdout(io.StringIO()):
                    self.assertIsNone(pilot.prepare(self.api, RUN, self.api.clock()))
                    self.assertEqual([], self.posts)
                    self.assertEqual((0, [], 0, 0), (
                        self.chain["rounds"], self.chain["operations"], state.chain_spend(self.chain),
                        state.worker_slots(self.api.ledger)))
                    self.transport.values[f"{self.api.prefix}/commits/{head}/check-runs"] = {
                        "total_count": 0, "check_runs": []}
                    self.api.clock.advance(seconds=60)
                    self.assertIsNone(pilot.prepare(self.api, RUN, self.api.clock()))
                self.assertEqual(1, len(self.posts))
                observed = self.api.observe(self.chain)
                self.assertEqual("42", observed["approval"]["id"])
                self.assertTrue(all(not item["id"].startswith("check:") for item in observed["feedback"]))
                self.assertEqual((0, []), (self.chain["rounds"], self.chain["operations"]))
                self.assertEqual((0, 0), (state.chain_spend(self.chain), state.worker_slots(self.api.ledger)))
                self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_current_approval_prevents_false_green_with_other_successful_checks_and_review(self):
        self.setup_api()
        head = self.run["head_sha"]
        self.transport.values[f"{self.api.prefix}/commits/{head}/status"] = {
            "statuses": [{"id": 2, "context": "test", "state": "success"}]}
        self.transport.values[f"{self.api.prefix}/pulls/20722/reviews"] = [{
            "id": 3, "user": {"id": 10}, "state": "APPROVED", "commit_id": head, "body": "",
            "submitted_at": "2026-10-04T00:00:00Z"}]
        self.assertFalse(self.api.observe(self.chain)["ready"])
        self.run["conclusion"] = "success"
        self.assertTrue(self.api.observe(self.chain)["ready"])

    def test_unknown_workflow_before_prepare_blocks_admission_and_preserves_all_reminder_receipts(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            for send_state in (None, "observed", "sent", "uncertain", "confirmed"):
                with self.subTest(binding=binding.name, send_state=send_state):
                    self.setup_api(binding)
                    if send_state is not None:
                        self.tick()
                        if send_state == "sent":
                            self.chain["reminder"]["sendState"] = "sent"
                            self.api.persist()
                        elif send_state in {"uncertain", "confirmed"}:
                            if send_state == "uncertain":
                                self.send_outcome = "lost-no-receipt"
                            self.api.clock.advance(seconds=60)
                            self.tick()
                    before = deepcopy(self.chain)
                    posts = len(self.posts)
                    self.runs_response = IncompleteInventory("HTTP 403")
                    self.api.clock.advance(minutes=2)
                    self.restart()
                    log = io.StringIO()
                    with patch.object(self.api, "publish_status"), redirect_stdout(log):
                        packet = pilot.prepare(self.api, RUN, self.api.clock())
                    self.assertIsNone(packet)
                    self.assertEqual(before, self.chain)
                    self.assertEqual((0, [], 0, 0), (
                        self.chain["rounds"], self.chain["operations"], state.chain_spend(self.chain),
                        state.worker_slots(self.api.ledger)))
                    self.assertEqual(posts, len(self.posts))
                    self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])
                    observed = self.api.observe(self.chain)
                    self.assertFalse(observed["actionable"])
                    self.assertEqual(observed["workflowAttention"] + " No inference.",
                                     self.api.next_action(self.chain, observed))
                    self.assertIn("Next action: " + observed["workflowAttention"] + " No inference.", log.getvalue())

    def test_unknown_workflow_at_fresh_settlement_blocks_task_post_and_still_settles_native_billing(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                self.setup_api(binding)
                self.set_runs([])
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(self.api, RUN, self.api.clock(), present=False)
                self.assertIsNotNone(packet)
                self.runs_response = IncompleteInventory("HTTP 403")
                self.restart()
                result = pilot.settle(self.api, packet,
                                      reconciliation_evidence(fixtures.decision(packet)), 4, self.api.clock())
                self.assertEqual("failed", result["outcome"])
                self.assertIn("approval is unknown", result["error"])
                operation = self.chain["operations"][-1]
                self.assertEqual((1, 4, 0, 0, None), (
                    self.chain["rounds"], operation["nativeActual"], operation["nativeReserved"],
                    operation["workerReserved"], operation["taskId"]))
                self.assertEqual((4, 0), (state.chain_spend(self.chain), state.worker_slots(self.api.ledger)))
                self.assertEqual([], self.posts)
                self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_ordinary_pending_draft_and_review_waits_never_start_reminders(self):
        for kind in ("pending-run", "pending-check", "draft", "review"):
            with self.subTest(kind=kind):
                self.setup_api()
                self.set_runs([])
                if kind == "pending-run":
                    self.run.update(status="queued", conclusion=None)
                    self.set_runs([self.run])
                elif kind == "pending-check":
                    self.transport.values[f"{self.api.prefix}/commits/{self.run['head_sha']}/check-runs"] = {
                        "total_count": 1, "check_runs": [{"id": 2, "head_sha": self.run["head_sha"],
                                                         "status": "in_progress", "conclusion": None}]}
                else:
                    value = self.transport.values[f"{self.api.prefix}/pulls/20722"]
                    value["draft"] = kind == "draft"
                    value["requested_reviewers"] = [{"id": 10}] if kind == "review" else []
                self.tick()
                self.api.clock.advance(minutes=2)
                self.tick()
                self.assertNotIn("reminder", self.chain)
                self.assertEqual([], self.posts)

    def test_saved_worker_input_and_native_handoff_have_task_and_pr_links_without_changing_usage(self):
        for kind in ("worker", "native"):
            with self.subTest(kind=kind):
                api, transport, chain = self.setup_api(bindings.FORK)
                self.set_runs([])
                if kind == "worker":
                    chain, operation, task = fixtures.TrackedOnlyTests().seed_worker(api, transport)
                    task["state"] = task["sessions"][0]["state"] = "waiting_for_user"
                    api.reconcile_workers()
                    url = f"https://github.com/{api.repository}/tasks/{operation['taskId']}"
                    self.assertIn(url, api.status(chain, api.observe(chain), api.clock()))
                else:
                    operation = state.reserve(api.ledger, chain, github.fingerprint(api.observe(chain)) + ":round:1",
                                              api.clock(), local=False)
                    state.settle_native(operation, 2)
                    operation["sessionId"] = "NATIVE"
                    state.finish(operation, "completed")
                    chain["state"] = "human"
                    url = f"https://github.com/{api.repository}/pull/7"
                    with self.assertRaisesRegex(ValueError, "authority or credit allowance exhausted"):
                        api.guard(chain, api.observe(chain))
                api.persist()
                before = deepcopy(chain["operations"])
                spend = state.chain_spend(chain)
                self.tick()
                api.clock.advance(seconds=60)
                self.tick()
                self.assertEqual(1, len(self.posts))
                self.assertIn(url, self.posts[0])
                self.assertEqual(before, chain["operations"])
                self.assertEqual(spend, state.chain_spend(chain))
                self.assertEqual(1, chain["rounds"])
                if kind == "native":
                    self.assertEqual("human", chain["state"])

    def test_resolution_reappearance_and_head_changes_start_new_episodes(self):
        self.setup_api()
        self.tick()
        self.api.clock.advance(seconds=60)
        self.tick()
        old = deepcopy(self.chain["reminder"])
        self.run["conclusion"] = "success"
        self.tick()
        self.assertNotIn("reminder", self.chain)
        self.run["conclusion"] = "action_required"
        self.tick()
        self.assertNotEqual(old["id"], self.chain["reminder"]["id"])
        self.api.clock.advance(seconds=60)
        self.tick()
        self.assertEqual(2, len(self.posts))
        self.transport.values[f"{self.api.prefix}/pulls/20722"]["head"]["sha"] = self.run["head_sha"] = "b" * 40
        self.tick()
        self.assertEqual("b" * 40, self.chain["reminder"]["head"])
        self.assertEqual("observed", self.chain["reminder"]["sendState"])
        old = deepcopy(self.chain["reminder"])
        self.run.update(id=43, html_url=f"https://github.com/{self.api.repository}/actions/runs/43")
        self.tick()
        self.assertEqual(old["id"], self.chain["reminder"]["id"])
        self.assertEqual(old["firstObservedAt"], self.chain["reminder"]["firstObservedAt"])
        self.assertEqual("43", self.chain["reminder"]["reason"])
        self.assertEqual(2, len(self.posts))

    def test_approval_run_changes_keep_timer_and_sent_receipt_without_reping(self):
        self.setup_api(bindings.FORK)
        self.tick()
        before = deepcopy(self.chain["reminder"])
        self.api.clock.advance(seconds=59)
        self.run.update(id=43, html_url=f"https://github.com/{self.api.repository}/actions/runs/43")
        self.tick()
        self.assertEqual(before["id"], self.chain["reminder"]["id"])
        self.assertEqual(before["firstObservedAt"], self.chain["reminder"]["firstObservedAt"])
        self.assertEqual([], self.posts)
        self.api.clock.advance(seconds=1)
        self.tick()
        self.assertEqual(1, len(self.posts))
        self.assertIn(self.run["html_url"], self.posts[0])
        sent = deepcopy(self.chain["reminder"])
        self.run.update(id=44, html_url=f"https://github.com/{self.api.repository}/actions/runs/44")
        self.restart()
        self.api.clock.advance(minutes=2)
        self.tick()
        self.assertEqual(sent, self.chain["reminder"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual((0, [], 0), (self.chain["rounds"], self.chain["operations"], state.chain_spend(self.chain)))

    def test_unknown_workflow_or_saved_task_preserves_timer_and_all_send_states_across_restart(self):
        for kind in ("workflow", "worker"):
            for send_state in ("observed", "sent", "uncertain", "confirmed"):
                with self.subTest(kind=kind, send_state=send_state):
                    api, transport, chain = self.setup_api(bindings.FORK)
                    if kind == "worker":
                        self.set_runs([])
                        chain, operation, task = fixtures.TrackedOnlyTests().seed_worker(api, transport)
                        task["state"] = task["sessions"][0]["state"] = "waiting_for_user"
                        api.reconcile_workers()
                        api.persist()
                    self.tick()
                    if send_state == "sent":
                        chain["reminder"]["sendState"] = "sent"
                        api.persist()
                    elif send_state in {"uncertain", "confirmed"}:
                        if send_state == "uncertain":
                            self.send_outcome = "lost-no-receipt"
                        api.clock.advance(seconds=60)
                        self.tick()
                    before = deepcopy(chain["reminder"])
                    posts = len(self.posts)
                    if kind == "workflow":
                        self.runs_response = IncompleteInventory("HTTP 403")
                    else:
                        endpoint = f"agents/repos/{api.repository}/tasks/{operation['taskId']}"
                        transport.values[endpoint] = None
                        api.reconcile_workers()
                        api.persist()
                    api.clock.advance(minutes=2)
                    self.restart()
                    _, log = self.tick()
                    self.assertIn("timer/receipt retained", log)
                    self.assertEqual(before, self.chain["reminder"])
                    self.assertEqual(posts, len(self.posts))
                    if kind == "workflow":
                        self.runs_response = None
                    else:
                        transport.values[endpoint] = task
                    self.restart()
                    if kind == "worker":
                        self.api.reconcile_workers()
                        self.api.persist()
                    self.tick()
                    self.assertEqual(before["id"], self.chain["reminder"]["id"])
                    self.assertEqual(before["firstObservedAt"], self.chain["reminder"]["firstObservedAt"])
                    self.assertEqual(posts + (send_state == "observed"), len(self.posts))
                    if send_state != "observed":
                        self.assertEqual(before, self.chain["reminder"])

    def test_unknown_approval_does_not_replace_sent_episode_with_known_native_handoff(self):
        self.setup_api(bindings.FORK)
        self.tick()
        self.api.clock.advance(seconds=60)
        self.tick()
        before = deepcopy(self.chain["reminder"])
        operation = state.reserve(self.api.ledger, self.chain,
                                  github.fingerprint(self.api.observe(self.chain)) + ":round:1",
                                  self.api.clock(), local=False)
        state.settle_native(operation, 2)
        operation["sessionId"] = "NATIVE"
        state.finish(operation, "completed")
        self.chain["state"] = "human"
        self.api.persist()
        self.runs_response = IncompleteInventory("HTTP 403")
        self.restart()
        self.api.clock.advance(minutes=2)
        self.tick()
        self.assertEqual(before, self.chain["reminder"])
        self.assertEqual(1, len(self.posts))
        self.runs_response = None
        self.restart()
        self.tick()
        self.assertEqual(before, self.chain["reminder"])
        self.assertEqual(1, len(self.posts))
        self.assertEqual("human", self.chain["state"])

    def test_unknown_workflow_during_final_notification_guard_suppresses_post_and_keeps_timer(self):
        self.setup_api()
        self.tick()
        before = deepcopy(self.chain["reminder"])
        self.api.clock.advance(seconds=60)
        original = self.api.persist

        def persist():
            original()
            if self.chain["reminder"]["sendState"] == "sent":
                self.runs_response = IncompleteInventory("HTTP 403")

        self.api.persist = persist
        _, log = self.tick()
        self.assertEqual([], self.posts)
        self.assertIn("basis changed", log)
        self.assertEqual(before, self.chain["reminder"])
        self.api.persist = original
        self.runs_response = None
        self.restart()
        self.tick()
        self.assertEqual(1, len(self.posts))
        self.assertEqual(before["id"], self.chain["reminder"]["id"])
        self.assertEqual(before["firstObservedAt"], self.chain["reminder"]["firstObservedAt"])

    def test_closure_adoption_removal_hands_off_and_late_head_change_suppress_send(self):
        for change in ("closed", "removed", "hands-off", "late-head", "late-takeover", "late-resolution"):
            with self.subTest(change=change):
                self.setup_api()
                self.tick()
                self.api.clock.advance(seconds=60)
                value = self.transport.values[f"{self.api.prefix}/pulls/20722"]
                if change == "closed":
                    value["state"] = "closed"
                elif change == "removed":
                    value["labels"] = []
                elif change == "hands-off":
                    value["labels"].append({"name": "shepherd-hands-off"})
                if change.startswith("late"):
                    original = self.api.persist

                    def persist():
                        original()
                        if self.chain["reminder"]["sendState"] == "sent":
                            if change == "late-head":
                                value["head"]["sha"] = "b" * 40
                            elif change == "late-takeover":
                                value["labels"] = []
                            else:
                                self.run["conclusion"] = "success"

                    self.api.persist = persist
                self.tick()
                self.assertEqual([], self.posts)

    def test_worker_resolution_during_fresh_check_suppresses_ping(self):
        api, transport, chain = self.setup_api(bindings.FORK)
        self.set_runs([])
        chain, operation, task = fixtures.TrackedOnlyTests().seed_worker(api, transport)
        task["state"] = task["sessions"][0]["state"] = "waiting_for_user"
        api.reconcile_workers()
        api.persist()
        self.tick()
        api.clock.advance(seconds=60)
        task["state"] = task["sessions"][0]["state"] = "in_progress"
        _, log = self.tick()
        self.assertEqual([], self.posts)
        self.assertIn("human blocker changed", log)
        self.assertEqual("observed", chain["reminder"]["sendState"])
        self.tick()
        self.assertNotIn("reminder", chain)
        self.assertEqual(1, chain["rounds"])
        self.assertEqual(1, state.worker_slots(api.ledger))

    def test_authority_replacement_vetoes_reminder_before_send(self):
        self.setup_api()
        self.tick()
        self.api.clock.advance(seconds=60)
        replaced = deepcopy(self.api.ledger)
        replaced["cursor"] += 1
        self.transport.comments[0]["body"] = state.render(replaced)
        _, log = self.tick()
        self.assertEqual([], self.posts)
        self.assertIn("authority changed", log)

    def test_custom_delay_and_clock_rollback_never_send_early(self):
        self.setup_api()
        self.api.reminder_delay = 120
        self.tick()
        before = deepcopy(self.chain["reminder"])
        self.api.clock.advance(seconds=-1)
        _, log = self.tick()
        self.assertIn("clock rollback", log)
        self.assertEqual(before, self.chain["reminder"])
        self.api.clock.advance(seconds=120)
        self.tick()
        self.assertEqual([], self.posts)
        self.api.clock.advance(seconds=1)
        self.tick()
        self.assertEqual(1, len(self.posts))

    def test_optional_reminder_validation_rejects_malformed_fields_and_body_still_has_global_bound(self):
        self.setup_api()
        self.tick()
        for key, value in (("id", "not-a-uuid"), ("head", None), ("kind", []), ("reason", "x" * 257),
                           ("sendState", "completed"), ("commentId", True), ("firstObservedAt", "yesterday")):
            with self.subTest(field=key), self.assertRaises(ValueError):
                malformed = deepcopy(self.api.ledger)
                malformed["chains"][0]["reminder"][key] = value
                state.render(malformed)
        self.assertLessEqual(len(state.render(self.api.ledger).encode()), state.MAX_BODY)
        self.assertLessEqual(len(self.chain["reminder"]["id"]), 256)

    def test_foreign_workflow_pagination_and_unknown_response_never_prove_approval(self):
        self.setup_api()
        for response in (
            Response({"workflow_runs": [self.run], "total_count": 1}, {
                "Link": '<https://api.github.com/repos/radical/aspire/actions/runs?page=2>; rel="next"'}),
            Response({"workflow_runs": [], "total_count": 1}, {}),
            Response({"workflow_runs": None, "total_count": 0}, {}),
        ):
            self.runs_response = lambda _endpoint: response
            observed, _ = self.tick()
            self.assertIsNone(observed["approval"])
            self.assertIsNotNone(observed["workflowAttention"])
            self.assertFalse(observed["ready"])
            self.assertEqual([], self.posts)

    def test_lost_response_reconciles_created_comment_or_holds_unknown_without_repeat(self):
        for outcome in ("lost-created", "lost-no-receipt"):
            with self.subTest(outcome=outcome):
                self.setup_api()
                self.send_outcome = outcome
                self.tick()
                self.api.clock.advance(seconds=60)
                self.tick()
                self.assertEqual("confirmed" if outcome == "lost-created" else "uncertain",
                                 self.chain["reminder"]["sendState"])
                self.restart()
                self.api.clock.advance(minutes=2)
                self.tick()
                self.assertEqual(1, len(self.posts))
                if outcome == "lost-no-receipt":
                    endpoint = f"{self.api.prefix}/issues/20722/comments"
                    self.transport.values.setdefault(endpoint, []).append({
                        "id": 801, "user": ACTOR, "body": self.posts[0], "updated_at": "2026-10-04T00:00:00Z"})
                    self.tick()
                    self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
                    self.assertEqual(1, len(self.posts))

    def test_persisted_sent_boundary_without_receipt_never_reposts_on_restart(self):
        self.setup_api()
        self.tick()
        self.chain["reminder"]["sendState"] = "sent"
        self.api.persist()
        self.restart()
        self.api.clock.advance(minutes=2)
        self.tick()
        self.assertEqual([], self.posts)
        self.assertEqual("sent", self.chain["reminder"]["sendState"])

    def test_owned_reminders_and_unapproved_copies_are_excluded_but_approved_comments_remain(self):
        self.setup_api()
        self.tick()
        self.api.clock.advance(seconds=60)
        self.tick()
        endpoint = f"{self.api.prefix}/issues/20722/comments"
        self.transport.values[endpoint].extend([
            {"id": 901, "user": {"id": 99, "login": "other"}, "body": self.posts[0],
             "updated_at": "2026-10-04T00:00:00Z"},
            {"id": 902, "user": ACTOR, "body": "Please fix another concern",
             "updated_at": "2026-10-04T00:00:00Z"}])
        ids = [item["id"] for item in self.api.observe(self.chain)["feedback"]]
        self.assertEqual(["comment:902:2026-10-04T00:00:00Z",
                          "review-comment:31:2026-10-04T00:00:00Z"], ids)

    def test_unknown_run_evidence_is_visible_not_approval_or_green_and_retains_previous_timer(self):
        for failure in ("http", "old-head", "foreign-repo", "foreign-url", "count"):
            with self.subTest(failure=failure):
                self.setup_api()
                self.tick()
                before = deepcopy(self.chain["reminder"])
                if failure == "http":
                    self.runs_response = IncompleteInventory("HTTP 403")
                elif failure == "old-head":
                    self.run["head_sha"] = "b" * 40
                elif failure == "foreign-repo":
                    self.run["repository"]["id"] = bindings.FORK.repository_id
                elif failure == "foreign-url":
                    self.run["html_url"] = "https://elsewhere.test/actions/runs/42"
                else:
                    self.transport.values[f"{self.api.prefix}/actions/runs"]["total_count"] = 2
                self.api.clock.advance(minutes=2)
                observed, log = self.tick()
                self.assertIsNone(observed["approval"])
                self.assertFalse(observed["ready"])
                self.assertIn("unknown", observed["workflowAttention"])
                self.assertIn("timer/receipt retained", log)
                self.assertEqual(before, self.chain["reminder"])
                self.assertEqual([], self.posts)

    def test_complete_workflow_run_pagination_checks_every_current_head_item(self):
        self.setup_api()
        calls = []
        runs = [{**self.run, "id": index, "html_url": f"https://github.com/{self.api.repository}/actions/runs/{index}"}
                for index in range(1, 102)]
        path = f"{self.api.prefix}/actions/runs"

        def pages(endpoint):
            calls.append(endpoint)
            first = endpoint.endswith("page=1")
            link = f'<https://api.github.com/{path}?head_sha={self.run["head_sha"]}&per_page=100&page=2>; rel="next"'
            return Response({"total_count": 101, "workflow_runs": runs[:100] if first else runs[100:]},
                            {"Link": link} if first else {})

        self.runs_response = pages
        observed, _ = self.tick()
        self.assertEqual(2, len(calls))
        self.assertEqual("1", observed["approval"]["id"])
        runs[-1]["head_sha"] = "b" * 40
        observed, _ = self.tick()
        self.assertIsNone(observed["approval"])
        self.assertIsNotNone(observed["workflowAttention"])
        self.assertEqual([], self.posts)

    def test_disable_does_not_construct_writer_or_reset_existing_receipt(self):
        self.setup_api()
        self.tick()
        before = deepcopy(self.chain)
        with patch.dict(pilot.os.environ, {"CI_SHEPHERD_ENABLE": "false"}, clear=True), \
                patch.object(github, "PilotTransport") as transport, redirect_stdout(io.StringIO()):
            packet, envelope, prompt = hosted.prepare(self.work / "disabled", "pilot", RUN)
        transport.assert_not_called()
        self.assertIsNone(packet)
        self.assertIsNone(envelope["packet"])
        self.assertEqual("", prompt)
        self.assertEqual(before, self.chain)
        self.assertEqual([], self.posts)

    def test_delay_configuration_optional_schema_and_compiled_prepare_binding(self):
        self.setup_api()
        old = state.render(self.api.ledger)
        self.assertEqual(old, state.render(state.parse(old)))
        self.tick()
        self.assertEqual(self.api.ledger, state.parse(state.render(self.api.ledger)))
        environment = {"CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_TRACKER": "99",
                       "CI_SHEPHERD_TRACKER_NODE": "TRACKER", "CI_SHEPHERD_AUTHORITY_COMMENT": "500"}
        self.assertEqual(60, pilot.configuration(environment)["reminderDelay"])
        self.assertEqual(120, pilot.configuration({**environment, "CI_SHEPHERD_REMINDER_DELAY_SECONDS": "120"})["reminderDelay"])
        for bad in ("0", "-1", "one", "86401", "60.0"):
            with self.subTest(delay=bad), self.assertRaises(ValueError):
                pilot.configuration({**environment, "CI_SHEPHERD_REMINDER_DELAY_SECONDS": bad})
        self.assertEqual(60, pilot.configuration(
            {**environment, "CI_SHEPHERD_REMINDER_DELAY_SECONDS": "bad"}, billing=True)["reminderDelay"])
        from test_pilot_hosted import expression
        emitted = compiled_step("Prepare host-owned envelope")["env"]["CI_SHEPHERD_REMINDER_DELAY_SECONDS"]
        for configured, expected in (("", "60"), ("120", "120")):
            self.assertEqual(expected, expression(emitted, {"vars.CI_SHEPHERD_REMINDER_DELAY_SECONDS": configured}))

    def test_transport_fixed_upstream_body_and_bounded_run_reads_only(self):
        self.setup_api()
        self.tick()
        body = reminders.render(self.chain["reminder"], self.api.repository, 20722)
        endpoint = f"{self.api.prefix}/issues/20722/comments"
        writer = github.PilotTransport("fixture", write=True, binding=bindings.UPSTREAM)
        writer.validate_endpoint("POST", endpoint, {"body": body})
        for bad in (body.replace("@radical", "@other"), body + "\nUntrusted instructions",
                    body.replace("github.com/microsoft/aspire/actions", "github.com/radical/aspire/actions"),
                    "[automated] arbitrary comment"):
            with self.subTest(body=bad), self.assertRaises(ValueError):
                writer.validate_endpoint("POST", endpoint, {"body": bad})
        for method, route in (("PATCH", endpoint), ("POST", endpoint.replace("20722", "20723"))):
            with self.assertRaises(ValueError):
                writer.validate_endpoint(method, route, {"body": body})
        with self.assertRaises(ValueError):
            github.PilotTransport("fixture", write=False, binding=bindings.UPSTREAM).validate_endpoint(
                "POST", endpoint, {"body": body})
        for binding in (bindings.FORK, bindings.UPSTREAM):
            reader = github.PilotTransport("fixture", binding=binding)
            route = f"repos/{binding.repository}/actions/runs"
            reader.validate_endpoint("GET", f"{route}?head_sha={'a' * 40}&per_page=100&page=1", None)
            for query in ("", "?head_sha=bad&per_page=100&page=1",
                          f"?head_sha={'a' * 40}&per_page=100&page=11",
                          f"?head_sha={'a' * 40}&per_page=100&page=1&status=action_required"):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    reader.validate_endpoint("GET", route + query, None)

    def test_issue_phase_native_handoff_posts_to_origin_issue_with_content_state_digest(self):
        api, transport, chain = self.setup_issue_api(8)
        clock = api.clock
        identity = json.dumps([None, None], separators=(",", ":")) + ":round:1"
        operation = state.reserve(api.ledger, chain, identity, clock(), local=True)
        operation["sessionId"] = "SESSION-HANDOFF"
        state.finish(operation, "completed")
        chain["state"] = "human"
        api.persist()
        observed, log = self.tick()
        self.assertEqual("issue", observed["kind"])
        self.assertEqual(64, len(observed["head"]))
        self.assertIn("delay 60s", log)
        clock.advance(seconds=60)
        self.tick()
        self.assertEqual(1, len(self.posts))
        self.assertIn(f"Issue #8 at content state `{observed['head']}`", self.posts[0])
        self.assertIn("is blocked on an explicit human handoff", self.posts[0])
        self.assertIn(f"https://github.com/{api.repository}/issues/8", self.posts[0])
        self.assertNotIn("/pull/8", self.posts[0])
        self.assertTrue(reminders.valid_body(self.posts[0], api.repository, 8))
        # The reminder comment itself must never be mistaken for repair feedback.
        follow_up = api.observe(chain)
        self.assertEqual([], follow_up["feedback"])

    def test_child_adoption_reminder_guard_cancels_on_fresh_label_confirmation(self):
        api, transport = fixtures.TrackedOnlyTests().api(bindings.FORK)
        api.read_authority()
        chain = state.adopt(api.ledger, 8, "issue", "NODE8")
        transport.values[f"{api.prefix}/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []  # unconfirmed when the reminder episode was observed
        transport.values[f"{api.prefix}/pulls/9"] = child
        state.bind_child(api.ledger, chain, 9, child["node_id"])
        chain.update(childAdoption="uncertain", state="human")
        api.persist()
        observation = api.observe(chain)
        value = {"id": str(uuid.uuid4()), "head": observation["head"], "kind": "child-adoption",
                 "reason": "uncertain", "firstObservedAt": "2026-10-04T00:00:00Z",
                 "sendState": "observed", "commentId": None}
        # The label actually confirms between observation and the send attempt.
        transport.values[f"{api.prefix}/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        with self.assertRaises(ValueError):
            reminders.notification_guard(api, chain, observation, value)
        self.assertEqual("confirmed", chain["childAdoption"],
                          "the fresh recheck must actually resolve adoption, not just block the stale send")

    def test_legacy_pr_phase_reminder_body_still_recognized_after_kind_addition(self):
        legacy = ("[automated] @radical CI Shepherd needs human help.\n\n"
                  "PR #20722 at head `" + "a" * 40 + "` is blocked on an explicit human handoff.\n"
                  "Please review: https://github.com/microsoft/aspire/pull/20722\n\n"
                  + reminders.MARKER + str(uuid.uuid4()) + " -->")
        self.assertTrue(reminders.valid_body(legacy, "microsoft/aspire", 20722))

    def test_issue_phase_worker_input_reminder_survives_restart_and_dedupes(self):
        api, transport, chain = self.setup_issue_api(8)
        observed = api.observe(chain)
        operation = state.reserve(api.ledger, chain, github.fingerprint(observed) + ":round:1", api.clock(), local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(api.ledger, chain, operation, api.clock())
        state.sent(operation)
        task_id = "OWNEDISSUE8"
        operation["taskId"] = task_id
        task = {"id": task_id, "state": "waiting_for_user", "repository": {"id": api.repository_id},
                "creator": {"id": 1472}, "session_count": 1, "updated_at": "2026-10-04T00:00:00Z", "artifacts": [],
                "sessions": [{"id": "SESSION8", "task_id": task_id, "state": "waiting_for_user",
                              "repository": {"id": api.repository_id}, "user": {"id": 1472},
                              "base_ref": "main", "head_ref": "fix-8",
                              "prompt": github.CORRELATION + json.dumps(
                                  {"chain": chain["id"], "operation": operation["id"], "origin": 8})}]}
        transport.values[f"agents/repos/{api.repository}/tasks/{task_id}"] = task
        api.persist()
        api.reconcile_workers()
        api.persist()
        observed, log = self.tick()
        self.assertIn("delay 60s", log)
        timestamp = chain["reminder"]["firstObservedAt"]
        api.clock.advance(seconds=59)
        self.restart()
        _, log = self.tick()
        self.assertEqual([], self.posts)
        self.assertIn("remaining 1s", log)
        self.assertEqual(timestamp, self.chain["reminder"]["firstObservedAt"])
        self.api.clock.advance(seconds=1)
        self.tick()
        self.assertEqual(1, len(self.posts))
        self.assertIn(f"https://github.com/{api.repository}/tasks/{task_id}", self.posts[0])
        self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
        self.api.clock.advance(minutes=2)
        self.restart()
        self.tick()
        self.assertEqual(1, len(self.posts), "an already-confirmed receipt must dedupe, never resend")

    def setup_child_adoption_api(self, origin=8, child_number=9):
        # An issue-phase chain with a child PR bound, but the controller's
        # own adoption label write could not be confirmed on the child: the
        # exact state in which observation["managed"] is False while the
        # chain must still surface an owner-facing reminder.
        api, transport = fixtures.TrackedOnlyTests().api(bindings.FORK)
        api.read_authority()
        chain = state.adopt(api.ledger, origin, "issue", "NODE" + str(origin))
        transport.values[f"{api.prefix}/issues/{origin}"] = {
            "number": origin, "node_id": "NODE" + str(origin), "state": "open",
            "labels": [{"name": "shepherd-adopted"}]}
        transport.values[f"{api.prefix}/issues/{origin}/comments"] = []
        child = pr(child_number)
        child["labels"] = []  # unconfirmed when the reminder episode was observed
        transport.values[f"{api.prefix}/pulls/{child_number}"] = child
        state.bind_child(api.ledger, chain, child_number, child["node_id"])
        chain.update(childAdoption="uncertain", state="human")
        api.persist()
        self.api, self.transport, self.chain = api, transport, chain
        self.posts = []
        original = api.transport

        def send(method, endpoint, body):
            if method == "POST" and endpoint.endswith("/comments") and body["body"].startswith("[automated] @radical"):
                self.posts.append((endpoint, body["body"]))
                comment = {"id": 900 + len(self.posts), "user": ACTOR, "body": body["body"],
                           "updated_at": "2026-10-04T00:00:00Z"}
                transport.values.setdefault(endpoint, []).append(comment)
                return Response(deepcopy(comment), {}, 201)
            return original(method, endpoint, body)

        api.transport = api.api.transport = send
        return api, transport, chain

    def test_child_adoption_reminder_reaches_process_posts_to_origin_and_dedupes_across_restart(self):
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        observed, log = self.tick()
        self.assertFalse(observed["managed"], "the pending-adoption subject must still read unmanaged")
        self.assertIn("delay 60s", log)
        self.assertEqual("observed", chain["reminder"]["sendState"])
        clock.advance(seconds=59)
        self.restart()
        _, log = self.tick()
        self.assertEqual([], self.posts)
        self.assertIn("remaining 1s", log)
        clock.advance(seconds=1)
        self.tick()
        self.assertEqual(1, len(self.posts), "exactly one origin-issue post, never the child PR")
        endpoint, body = self.posts[0]
        self.assertEqual(f"{api.prefix}/issues/8/comments", endpoint,
                          "an unresolved child adoption must notify on the origin issue, not the unconfirmed child PR")
        self.assertIn("PR #9", body)
        self.assertIn(f"https://github.com/{api.repository}/pull/9", body)
        self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
        clock.advance(minutes=2)
        self.restart()
        self.tick()
        self.assertEqual(1, len(self.posts), "an already-confirmed receipt must dedupe, never resend")

    def test_child_adoption_reminder_through_real_sweep_and_prepare(self):
        # Not just process() called directly: the real sweep()/prepare() path
        # must independently reach the same pending-adoption notification.
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        with redirect_stdout(io.StringIO()):
            pilot.prepare(api, RUN, clock())
        self.assertEqual("observed", chain["reminder"]["sendState"])
        clock.advance(seconds=60)
        with redirect_stdout(io.StringIO()):
            pilot.prepare(api, RUN, clock())
        self.assertEqual(1, len(self.posts))
        self.assertEqual(f"{api.prefix}/issues/8/comments", self.posts[0][0])
        self.assertEqual("confirmed", chain["reminder"]["sendState"])
        clock.advance(minutes=2)
        with redirect_stdout(io.StringIO()):
            pilot.prepare(api, RUN, clock())
        self.assertEqual(1, len(self.posts), "a real sweep/prepare dedupe must never resend")

    def test_pending_child_adoption_takes_precedence_over_a_concurrent_native_handoff(self):
        # A genuine, unrelated native handoff (the chain's own latest
        # operation) coexists with a still-unconfirmed child adoption. Before
        # this fix, blocker() picked native-handoff first, and that kind can
        # never pass guard()'s managed-subject check while the label is
        # unconfirmed, so the allowed origin-only pending-adoption notice was
        # silently suppressed forever. The pending adoption must take
        # precedence instead, without disturbing the human chain state or the
        # native handoff operation itself.
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        # reserve() requires an "open" chain; the fixture already set "human"
        # for its own unconfirmed-adoption reason, so briefly reopen it just
        # to append this operation, matching how a native handoff would in
        # fact have been reserved before that later human stop occurred.
        chain["state"] = "open"
        identity = json.dumps([None, None], separators=(",", ":")) + ":round:1"
        operation = state.reserve(api.ledger, chain, identity, clock(), local=True)
        operation["sessionId"] = "SESSION-CONCURRENT-HANDOFF"
        state.finish(operation, "completed")
        chain["state"] = "human"
        api.persist()
        self.assertTrue(github.native_handoff(chain), "fixture must actually be a genuine native handoff")
        observed, log = self.tick()
        self.assertEqual("child-adoption", chain["reminder"]["kind"],
                          "pending child-adoption must win the reminder, not the concurrent native handoff")
        self.assertIn("ambiguous child-adoption needing confirmation", log)
        clock.advance(seconds=60)
        self.tick()
        self.assertEqual(1, len(self.posts))
        endpoint, body = self.posts[0]
        self.assertEqual(f"{api.prefix}/issues/8/comments", endpoint)
        self.assertIn("ambiguous child-adoption needing confirmation", body)
        self.assertEqual("human", chain["state"], "the genuine human chain state must not be disturbed")
        self.assertTrue(github.native_handoff(chain), "the separate native handoff operation itself must remain")

        # The child's own label write is now confirmed; because the chain's
        # latest operation is still this same genuine native handoff,
        # confirming the adoption must not silently reopen the chain to
        # "open" — only the pending-adoption reminder clears, and the
        # now-exposed native handoff must get its own, separate reminder.
        transport.values[f"{api.prefix}/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        api.adopt_child(chain)
        self.assertEqual("confirmed", chain["childAdoption"])
        self.assertEqual("human", chain["state"], "confirming adoption alongside a genuine handoff must not reopen it")
        observed, log = self.tick()
        self.assertEqual("native-handoff", chain["reminder"]["kind"],
                          "once confirmed, the separate native handoff must surface its own reminder")
        self.assertIn("an explicit human handoff", log)

    def test_child_adoption_reminder_lost_send_reconciles_without_reposting(self):
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        self.tick()
        clock.advance(seconds=60)
        original = api.transport

        def lost(method, endpoint, body):
            if method == "POST" and endpoint.endswith("/comments") and body["body"].startswith("[automated] @radical"):
                comment = {"id": 777, "user": ACTOR, "body": body["body"], "updated_at": "2026-10-04T00:00:00Z"}
                transport.values.setdefault(endpoint, []).append(comment)
                raise LostResponse("reminder outcome unknown")
            return original(method, endpoint, body)

        api.transport = api.api.transport = lost
        _, log = self.tick()
        self.assertEqual("confirmed", chain["reminder"]["sendState"])
        self.assertIn("confirmed by owned comment receipt", log)
        self.assertEqual(0, len(self.posts), "the lost-send fixture echoes through transport.values, not self.posts")
        before = transport.values[f"{api.prefix}/issues/8/comments"]
        api.transport = api.api.transport = original
        self.tick()
        self.assertIs(before, transport.values[f"{api.prefix}/issues/8/comments"],
                       "a confirmed receipt must never attempt another send")

    def test_child_adoption_reminder_cancels_when_confirmed_before_send_deadline(self):
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        self.tick()
        # The label actually confirms before the delayed send fires.
        transport.values[f"{api.prefix}/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        clock.advance(seconds=60)
        self.tick()
        self.assertEqual([], self.posts, "a confirmed adoption must cancel the pending reminder, not post")
        self.assertEqual("confirmed", chain["childAdoption"])
        self.assertNotEqual("confirmed", chain["reminder"]["sendState"], "a cancelled reminder must never be marked sent")

    def test_child_adoption_reminder_guard_catches_a_hands_off_race_after_adopt_child_but_before_its_own_fresh_read(self):
        # adopt_child()'s own fetch-based recheck (inside notification_guard())
        # and guard()'s subsequent fresh observe() are two separately-timed
        # reads of the same child PR. A hands-off label applied strictly
        # between them (still unseen by adopt_child's read, so childAdoption
        # stays "uncertain" and does not set chain["state"] to "hands-off")
        # must still be caught by guard()'s OWN fresh read, not waved through
        # because adopt_child's earlier read looked fine.
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        self.tick()
        clock.advance(seconds=60)
        original = api.transport
        calls = {"pulls": 0}

        def racing(method, endpoint, body):
            if method == "GET" and endpoint == f"{api.prefix}/pulls/9":
                calls["pulls"] += 1
                # Call 1 is this tick's own top-level observe() (before
                # notification_guard runs at all); call 2 is adopt_child()'s
                # fetch-based recheck inside notification_guard. Only from
                # call 3 (guard()'s own fresh observe()) does the race labe
                # actually appear, so adopt_child's read is still clean.
                if calls["pulls"] >= 3:
                    transport.values[endpoint]["labels"] = [{"name": "shepherd-hands-off"}]
            return original(method, endpoint, body)

        api.transport = api.api.transport = racing
        _, log = self.tick()
        self.assertEqual([], self.posts, "guard's own fresh read must cancel a mid-flight hands-off, "
                                          "even though adopt_child's earlier read in the same attempt saw none")
        self.assertGreaterEqual(calls["pulls"], 3, "the race requires tick's observe, adopt_child, and guard to "
                                                    "each read independently")
        self.assertIn("not sent; fresh guard unavailable", log)

    def test_child_adoption_reminder_cancels_on_hands_off_before_send_deadline(self):
        # Driven through the real sweep()/prepare() path (not process() called
        # in isolation): a genuine hands-off takeover is settled by sweep()'s
        # own observed-based adopt_child() call before process() ever runs,
        # the same ordering production uses. Once settled, process()'s own
        # fresh-field pending_adoption check (a backstop, not the primary
        # settlement path) must also agree the reminder is no longer pending.
        api, transport, chain = self.setup_child_adoption_api(8, 9)
        clock = api.clock
        with redirect_stdout(io.StringIO()):
            pilot.prepare(api, RUN, clock())
        # A human explicitly takes the child over before the delayed send fires.
        transport.values[f"{api.prefix}/pulls/9"]["labels"] = [{"name": "shepherd-hands-off"}]
        clock.advance(seconds=60)
        with redirect_stdout(io.StringIO()):
            pilot.prepare(api, RUN, clock())
        self.assertEqual([], self.posts, "a genuine hands-off takeover must cancel the pending reminder, not post")
        self.assertEqual("hands-off", chain["state"])

    def test_resolved_issue_worker_result_in_human_state_also_gets_a_reminder(self):
        api, _, chain, operation, _ = self.setup_issue_worker_api("AMBIGUOUSARTIFACT8")
        clock = api.clock
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(api, RUN, clock()))
        self.assertEqual(("human", None, "worker-result", "observed"),
                          (chain["state"], chain["child"], chain["reminder"]["kind"], chain["reminder"]["sendState"]))
        self.assertEqual(("completed", "completed", 1.5, 0),
                          (operation["state"], operation["workerState"], operation["workerActual"], operation["workerReserved"]))
        history = deepcopy({key: chain[key] for key in ("rounds", "operations", "dispositions")})
        self.assertEqual([], self.posts)
        clock.advance(seconds=59)
        self.restart()
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.api, RUN, clock()))
        self.assertEqual([], self.posts)
        clock.advance(seconds=1)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.api, RUN, clock()))
        self.assertEqual(1, len(self.posts))
        self.assertIn("Issue #8 at content state", self.posts[0])
        self.assertIn(f"https://github.com/{api.repository}/tasks/AMBIGUOUSARTIFACT8", self.posts[0])
        self.assertEqual("confirmed", self.chain["reminder"]["sendState"])
        clock.advance(minutes=2)
        self.restart()
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(self.api, RUN, clock()))
        self.assertEqual(1, len(self.posts))
        self.assertEqual(history, {key: self.chain[key] for key in history})

    def test_worker_result_resumed_task_during_fresh_check_suppresses_stale_ping(self):
        api, _, chain, _, task = self.setup_issue_worker_api("AMBIGUOUSARTIFACT8R")
        api.reconcile_workers()
        api.persist()
        self.tick()
        self.assertIsNotNone(chain.get("reminder"))
        self.api.clock.advance(seconds=60)
        # Resume after observation: the fresh receipt must cancel a stale terminal notice.
        task["state"] = task["sessions"][0]["state"] = "in_progress"
        _, log = self.tick()
        self.assertEqual([], self.posts, "a resumed task must cancel the stale worker-result reminder, not post it")
        self.assertIn("not sent; fresh guard unavailable", log)
        self.assertIn("subject basis changed", log)
        self.assertEqual("observed", chain["reminder"]["sendState"])
