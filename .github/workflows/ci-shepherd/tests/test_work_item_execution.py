from copy import deepcopy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlencode, urlparse
import unittest
from unittest.mock import patch

from github import LostResponse, RejectedEffect, Response
import live
import local
import pilot_binding as bindings
import pilot_github
import pilot_handoff
import pilot_reminders
import pilot_state as state
import work_item_github
import work_item_receiver
import work_items
import round as contracts
from test_pilot_github import Transport, pr

from test_work_item_receiver import Authority
from test_work_items import control, result, validation
import work_item_execution as execution


class CloudAuthority(Authority):
    repository = "radical/aspire"

    def __init__(self, saved=None):
        super().__init__(saved)
        self.posts = []

    def start_task(self, body, before_send):
        before_send()
        self.posts.append((deepcopy(body), deepcopy(self.saved)))
        return "TASK1"

    def reconcile_workers(self, *, adopt_children, acquisition):
        pass


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.control = control()
        self.api = CloudAuthority()
        self.read = lambda: deepcopy(self.control)

    def test_reentry_never_launches_the_assignment_twice(self):
        prepared = execution.prepare(self.api, self.read)
        approval = prepared["approval_template"]
        read_approval = lambda: deepcopy(approval)
        first = execution.execute(self.api, self.read, read_approval)
        second = execution.execute(self.api, self.read, read_approval)
        self.assertEqual("TASK1", first["task_id"])
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.api.posts))
        sent = self.api.posts[0][1]["workItems"][0]["assignments"][0]["execution"]
        self.assertEqual("sending", sent["state"])
        self.assertIsNone(sent["task_id"])
        self.assertTrue(self.api.posts[0][0]["create_pull_request"])
        self.assertEqual("main", self.api.posts[0][0]["base_ref"])

    def test_lost_post_response_never_retries_even_after_restart(self):
        prepared = execution.prepare(self.api, self.read)
        approval = prepared["approval_template"]
        with patch.object(self.api, "start_task", side_effect=LostResponse("lost")):
            self.assertEqual("uncertain", execution.execute(self.api, self.read, lambda: approval)["outcome"])
        restarted = CloudAuthority(self.api.saved)
        self.assertEqual("uncertain", execution.execute(restarted, self.read, lambda: approval)["outcome"])
        self.assertEqual([], restarted.posts)
        self.assertEqual(1, state.worker_slots(restarted.saved))
        self.assertGreater(state.repository_spend(restarted.saved, execution.live.clock()), 0)

    def test_wrong_or_incomplete_approval_never_sends(self):
        prepared = execution.prepare(self.api, self.read)
        for key in prepared["approval_template"]:
            with self.subTest(key=key):
                approval = deepcopy(prepared["approval_template"])
                approval[key] = False
                with self.assertRaises(ValueError):
                    execution.execute(self.api, self.read, lambda: approval)
                self.assertEqual([], self.api.posts)

    def test_stale_control_and_escaped_packet_cannot_launch(self):
        work_item_receiver.receive(self.api, self.read, lambda _: None)
        with self.assertRaisesRegex(ValueError, "packet escaped"):
            execution.prepare(self.api, self.read)
        self.assertEqual([], self.api.posts)

    def test_changed_control_after_preparation_cannot_launch(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        self.control.update(revision=2, action="pause")
        with self.assertRaises(ValueError):
            execution.execute(self.api, self.read, lambda: approval)
        self.assertEqual([], self.api.posts)

    def test_same_issue_new_item_cannot_bypass_unknown_send(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        with patch.object(self.api, "start_task", side_effect=LostResponse("lost")):
            execution.execute(self.api, self.read, lambda: approval)
        self.control.update(id="new-item", revision=2)
        with self.assertRaisesRegex(ValueError, "already has cloud execution"):
            execution.prepare(self.api, self.read)
        self.assertEqual([], self.api.posts)

    def test_checkpoint_and_revision_cannot_replace_cloud_assignment(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        execution.execute(self.api, self.read, lambda: approval)
        record = self.api.ledger["workItems"][0]
        assignment = record["assignments"][0]
        assignment["execution"]["session_id"] = "session-1"
        work_items.checkpoint(record, result(assignment), "session-1", None)
        self.control["revision"] += 1
        with self.assertRaisesRegex(ValueError, "cloud"):
            work_items.claim(record, self.control)
        self.assertEqual(1, len(record["assignments"]))

    def test_all_escaped_execution_states_block_claim_but_no_send_can_be_replaced(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        original = deepcopy(self.api.ledger["workItems"][0])
        changed_control = deepcopy(self.control)
        changed_control["revision"] += 1
        for status in ("reserved", "sending", "sent", "uncertain", "no_send"):
            with self.subTest(status=status):
                record = deepcopy(original)
                assignment = record["assignments"][0]
                assignment["execution"].update(
                    state=status, approval=None if status == "reserved" else approval,
                    task_id="TASK1" if status == "sent" else None)
                assignment["result"] = result(assignment)
                if status == "no_send":
                    replacement = work_items.claim(record, changed_control)
                    self.assertEqual(2, len(record["assignments"]))
                    self.assertEqual(2, replacement["revision"])
                    self.assertNotEqual(assignment["id"], replacement["id"])
                else:
                    with self.assertRaisesRegex(ValueError, "cloud execution already owns"):
                        work_items.claim(record, changed_control)
                    self.assertEqual(1, len(record["assignments"]))

    def test_failed_sending_persistence_never_crosses_post_boundary(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        persist = self.api.persist
        calls = []
        def interrupted():
            calls.append(None)
            if len(calls) == 2:
                raise ValueError("persistence unavailable")
            return persist()
        with patch.object(self.api, "persist", side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, "persistence unavailable"):
                execution.execute(self.api, self.read, lambda: approval)
        self.assertEqual([], self.api.posts)

    def test_task_receipt_persist_failure_retains_sending_and_never_retries(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        persist = self.api.persist
        calls = []
        def interrupted():
            calls.append(None)
            if len(calls) == 3:
                raise ValueError("persistence unavailable")
            return persist()
        with patch.object(self.api, "persist", side_effect=interrupted):
            with self.assertRaises(ValueError):
                execution.execute(self.api, self.read, lambda: approval)
        self.assertEqual(1, len(self.api.posts))
        restarted = CloudAuthority(self.api.saved)
        self.assertEqual("sending", execution.execute(restarted, self.read, lambda: approval)["outcome"])
        self.assertEqual([], restarted.posts)

    def test_definitive_no_send_releases_but_does_not_redispatch(self):
        approval = execution.prepare(self.api, self.read)["approval_template"]
        with patch.object(self.api, "start_task", side_effect=RejectedEffect("denied")):
            self.assertEqual("no_send", execution.execute(self.api, self.read, lambda: approval)["outcome"])
        self.assertEqual(0, state.worker_slots(self.api.saved))
        self.assertEqual(0, state.repository_spend(self.api.saved, execution.live.clock()))
        self.assertEqual("no_send", execution.execute(self.api, self.read, lambda: approval)["outcome"])
        self.assertEqual([], self.api.posts)


class MappingTests(unittest.TestCase):
    repository = "radical/aspire"

    def setUp(self):
        self.control = control()
        self.control["issue"]["repository"] = self.repository
        self.control["occurrence"]["repository"] = self.repository
        self.prefix = f"repos/{self.repository}"
        self.transport = Transport()
        self.transport.comments[0]["body"] = state.render(state.new_ledger(self.repository))
        repository = {"id": work_item_github.REPOSITORY_IDS[self.repository], "full_name": self.repository}
        self.transport.values[self.prefix] = dict(repository, default_branch="main")
        self.transport.values[f"{self.prefix}/issues/900"] = {
            "id": 1900, "number": 900, "node_id": "ISSUE900", "state": "open", "labels": []}
        self.api = work_item_github.CloudWorkItemGitHub(
            "fixture", 99, 500, "TRACKER99", revision="c" * 40,
            control=self.control, transport=self.transport)
        for name in ("require_source", "require_idle_actions"):
            mocked = patch.object(local, name)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(self.api, "enabled", return_value=True)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.read = lambda: deepcopy(self.control)
        prepared = execution.prepare(self.api, self.read)
        self.approval = prepared["approval_template"]
        self.assignment = self.api.ledger["workItems"][0]["assignments"][0]
        self.assignment["execution"].update(approval=self.approval, state="sending", worker_reserved=500)
        self.api.persist()
        self.assignment["execution"].update(state="sent", task_id="TASK1")
        self.api.persist()
        self.pull = pr(7)
        for key in ("base", "head"):
            self.pull[key]["repo"] = repository
        self.pull.update(draft=True, labels=[], issue_url=f"https://api.github.com/{self.prefix}/issues/7",
                         body=f"Refs {self.repository}#900", merged=False, merged_at=None, commits=1)
        # The issue representation and pull resource have DIFFERENT numeric IDs.
        self.issue = {"id": 5007, "node_id": self.pull["node_id"], "number": 7,
                      "repository_url": f"https://api.github.com/{self.prefix}",
                      "pull_request": {"url": f"https://api.github.com/{self.prefix}/pulls/7"}}
        self.task = {
            "id": "TASK1", "repository": repository, "creator": {"id": 1472}, "state": "completed",
            "session_count": 1, "sessions": [{
                "id": "SESSION1", "task_id": "TASK1", "repository": repository, "user": {"id": 1472},
                "state": "completed", "base_ref": "main", "head_ref": self.pull["head"]["ref"],
                "prompt": execution.request(self.api, self.api.ledger["workItems"][0], self.assignment)["prompt"],
                "usage": {"type": "ai_credits", "amount": 10_000_000_000}}],
            "artifacts": [{"provider": "github", "type": "pull",
                           "data": {"id": self.pull["id"], "global_id": self.pull["node_id"]}},
                          {"provider": "github", "type": "branch",
                           "data": {"base_ref": "main", "head_ref": self.pull["head"]["ref"]}}],
        }
        self.transport.values.update({
            f"agents/repos/{self.repository}/tasks/TASK1": self.task,
            f"{self.prefix}/pulls": [self.pull],
            f"{self.prefix}/pulls/7": self.pull,
            f"{self.prefix}/issues/7": self.issue,
            f"{self.prefix}/pulls/7/commits": [
                {"sha": self.pull["head"]["sha"], "commit": {"message": "Repair boundary; Refs radical/aspire#900"}}],
            f"{self.prefix}/git/ref/heads/fix-7": {
                "ref": "refs/heads/fix-7", "object": {"sha": self.pull["head"]["sha"]}},
            f"{self.prefix}/issues/900/timeline": [
                {"event": "cross-referenced", "source": {"type": "issue", "issue": self.issue}}],
        })

    def test_import_uses_issue_identity_not_pull_identity_and_is_idempotent(self):
        first = execution.import_pr(self.api, self.read)
        second = execution.import_pr(self.api, self.read)
        self.assertEqual(first, second)
        self.assertEqual("tracked", first["outcome"])
        self.assertEqual(1, len(self.api.ledger["chains"]))
        chain = self.api.ledger["chains"][0]
        self.assertEqual("pr", chain["kind"])
        self.assertEqual("handoff_needed", chain["handoff"]["phase"])
        self.assertIsNone(chain["handoff"]["confirmedAt"])
        self.assertEqual([], chain["operations"])
        self.assertEqual(self.issue["id"], self.api.ledger["workItems"][0]["assignments"][0]["execution"]["pr"]["issue_id"])
        self.assertTrue(all(method == "PATCH" for method, _, _ in self.transport.writes))

    def test_import_only_reads_exact_head_inventory_even_when_broad_inventory_exceeds_bound(self):
        original = Transport.__call__
        expected = self.repository.split("/", 1)[0] + ":" + self.pull["head"]["ref"]
        policy = work_item_github.CloudWorkItemTransport("fixture", 99, 500, self.control)
        def filtered(instance, method, endpoint, body):
            if method == "GET" and endpoint.split("?")[0] == f"{self.prefix}/pulls":
                policy.validate_endpoint(method, endpoint, body)
                query = parse_qs(urlparse(endpoint).query)
                if query.get("head") != [expected]:
                    return Response([dict(self.pull, body="x" * 1_000_001)], {})
            return original(instance, method, endpoint, body)
        with patch.object(Transport, "__call__", filtered):
            self.assertEqual("tracked", execution.import_pr(self.api, self.read)["outcome"])
        inventory = [endpoint for method, endpoint, _ in self.transport.reads
                     if method == "GET" and endpoint.split("?")[0] == f"{self.prefix}/pulls"]
        self.assertEqual(2, len(inventory))
        self.assertTrue(all(parse_qs(urlparse(endpoint).query)["head"] == [expected] for endpoint in inventory))

    def test_real_monitor_blocks_resumed_worker_and_receiver_never_publishes_again(self):
        self.assertEqual("tracked", execution.import_pr(self.api, self.read)["outcome"])
        chain = self.api.ledger["chains"][0]
        monitored = self.api.observe(chain)
        self.assertTrue(monitored["managed"])
        self.assertEqual("handoff_needed", chain["handoff"]["phase"])
        self.task["state"] = self.task["sessions"][0]["state"] = "in_progress"
        monitored = self.api.observe(chain)
        self.assertIsNotNone(monitored["attention"])
        self.assertEqual("handoff_pending", chain["handoff"]["phase"])
        self.assertGreater(state.worker_slots(self.api.ledger), 0)
        emitted = []
        self.assertEqual("cloud_owned", work_item_receiver.receive(self.api, self.read, emitted.append)["outcome"])
        self.assertEqual([], emitted)

    def test_each_timeline_identity_component_is_required(self):
        original = deepcopy(self.issue)
        for key, wrong in (("id", self.pull["id"]), ("number", 8), ("node_id", "FOREIGN"),
                           ("repository_url", "https://api.github.com/repos/foreign/repo"),
                           ("pull_request", {"url": "https://api.github.com/repos/foreign/repo/pulls/7"})):
            with self.subTest(key=key):
                link = dict(original, **{key: wrong})
                self.transport.values[f"{self.prefix}/issues/900/timeline"] = [
                    {"event": "cross-referenced", "source": {"type": "issue", "issue": link}}]
                self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
                self.assertEqual([], self.api.ledger["chains"])

    def test_missing_wrong_type_and_duplicate_links_are_not_confused(self):
        path = f"{self.prefix}/issues/900/timeline"
        for timeline in ([], [{"event": "cross-referenced", "source": {"type": "pull", "issue": self.issue}}]):
            self.transport.values[path] = timeline
            self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
        link = {"event": "cross-referenced", "source": {"type": "issue", "issue": self.issue}}
        self.transport.values[path] = [link, deepcopy(link)]
        self.assertEqual("tracked", execution.import_pr(self.api, self.read)["outcome"])

    def test_wrong_task_session_identity_restores_unknown_holds(self):
        original = deepcopy(self.task)
        mutations = [("id", "FOREIGN"), ("creator", {"id": 1}), ("repository", {"id": 1}),
                     ("session_count", 2), ("sessions", []), ("artifacts", None)]
        for key, wrong in mutations:
            with self.subTest(key=key):
                self.transport.values[f"agents/repos/{self.repository}/tasks/TASK1"] = dict(original, **{key: wrong})
                self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
                self.assertGreater(state.worker_slots(self.api.ledger), 0)
                self.assertGreater(state.repository_spend(self.api.ledger, self.api.clock()), 0)
        for key, wrong in (("task_id", "FOREIGN"), ("user", {"id": 1}), ("base_ref", "release"),
                           ("prompt", "Untrusted claimed completion"), ("state", "in_progress")):
            changed = deepcopy(original)
            changed["sessions"][0][key] = wrong
            self.transport.values[f"agents/repos/{self.repository}/tasks/TASK1"] = changed
            self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
            self.assertEqual([], self.api.ledger["chains"])

    def test_missing_ambiguous_and_foreign_artifacts_never_import(self):
        original = deepcopy(self.task["artifacts"])
        for artifacts in ([], original + [original[0]], [original[0]], [
                dict(original[0], data={"id": 999999}), original[1]],
                [original[0], dict(original[1], data={"base_ref": "main", "head_ref": "foreign"})]):
            self.task["artifacts"] = artifacts
            self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
            self.assertEqual([], self.api.ledger["chains"])

    def test_closing_body_and_commits_wrong_ref_and_nondraft_block_import(self):
        for key, wrong in (("draft", False), ("state", "closed"), ("issue_url", "https://evil.invalid/7"),
                           ("body", "Fixes #900"), ("body", "Unrelated PR")):
            original = self.pull[key]
            self.pull[key] = wrong
            self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
            self.assertEqual([], self.api.ledger["chains"])
            self.pull[key] = original
        self.transport.values[f"{self.prefix}/pulls/7/commits"][0]["commit"]["message"] = "Closes #900"
        self.assertEqual("needs_human", execution.import_pr(self.api, self.read)["outcome"])
        self.assertEqual([], self.api.ledger["chains"])

    def test_billing_age_not_refreshed_and_missing_usage_reholds(self):
        record = self.api.ledger["workItems"][0]
        saved = record["assignments"][0]["execution"]
        execution.refresh(self.api, record, record["assignments"][0])
        timestamp = saved["worker_at"]
        execution.refresh(self.api, record, record["assignments"][0])
        self.assertEqual(timestamp, saved["worker_at"])
        self.assertEqual(10, saved["worker_actual"])
        self.assertEqual(0, saved["worker_reserved"])
        self.task["sessions"][0].pop("usage")
        execution.refresh(self.api, record, record["assignments"][0])
        self.assertGreater(saved["worker_reserved"], 0)

    def test_bidirectional_binding_and_sticky_handoff_cannot_be_forged(self):
        execution.import_pr(self.api, self.read)
        ledger = deepcopy(self.api.ledger)
        for field, wrong in (("sourceWorkItem", {"item_id": "foreign", "assignment_id": self.assignment["id"]}),
                             ("handoff", None), ("child", 9), ("node", "foreign")):
            changed = deepcopy(ledger)
            changed["chains"][0][field] = wrong
            with self.assertRaises((ValueError, TypeError)):
                state.validate(changed)
        changed = deepcopy(ledger)
        changed["chains"][0].pop("sourceWorkItem")
        with self.assertRaisesRegex(ValueError, "reverse binding"):
            state.validate(changed)
        changed = deepcopy(ledger)
        changed["chains"][0]["handoff"]["phase"] = "initial"
        with self.assertRaises(ValueError):
            state.validate(changed)

    def test_cloud_checkpoint_is_replay_safe_but_not_worker_attestation(self):
        execution.import_pr(self.api, self.read)
        assignment = self.api.ledger["workItems"][0]["assignments"][0]
        claimed = result(assignment)
        claimed["worker_id"] = "SESSION1"
        checked = validation(assignment)
        checked["resulting_head"] = self.pull["head"]["sha"]
        first = work_item_receiver.accept(self.api, self.read, claimed, "SESSION1", checked)
        self.assertEqual(first, work_item_receiver.accept(self.api, self.read, claimed, "SESSION1", checked))
        checked["resulting_head"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "head differs"):
            work_item_receiver.accept(self.api, self.read, claimed, "SESSION1", checked)
        self.assertEqual("cloud_owned", work_item_receiver.receive(self.api, self.read, self.fail)["outcome"])

    def test_real_task_post_and_saved_id_precede_missing_details(self):
        record = self.api.ledger["workItems"][0]
        saved = record["assignments"][0]["execution"]
        saved.update(approval=None, state="reserved", task_id=None, worker_reserved=0)
        self.api.persist()
        original = Transport.__call__
        def transport(instance, method, endpoint, body):
            if method == "POST" and endpoint == f"agents/repos/{self.repository}/tasks":
                instance.writes.append((method, endpoint, body))
                self.assertEqual("sending", state.parse(instance.comments[0]["body"])["workItems"][0]["assignments"][0]["execution"]["state"])
                return Response({"id": "TASK2", "state": "queued", "created_at": "2026-10-08T00:00:00Z"}, {}, 201)
            return original(instance, method, endpoint, body)
        with patch.object(Transport, "__call__", transport):
            self.assertEqual("TASK2", execution.execute(self.api, self.read, lambda: self.approval)["task_id"])
        self.assertEqual("TASK2", state.parse(self.transport.comments[0]["body"])["workItems"][0]["assignments"][0]["execution"]["task_id"])
        self.assertEqual("needs_human", execution.observe(self.api, self.read)["outcome"])
        self.assertEqual("sent", execution.execute(self.api, self.read, lambda: self.approval)["outcome"])
        self.assertEqual(1, len([call for call in self.transport.writes if call[0] == "POST"]))

    def test_execution_refreshes_two_resumed_workers_before_admission(self):
        self.resumed_capacity()

    def test_execution_refreshes_workers_again_at_final_post_boundary(self):
        self.resumed_capacity(final_boundary=True)

    def test_unreadable_workers_restore_capacity_before_admission(self):
        self.resumed_capacity(unreadable=True)

    def resumed_capacity(self, *, final_boundary=False, unreadable=False):
        execution.observe(self.api, self.read)
        first = self.api.ledger["workItems"][0]
        second = deepcopy(first)
        second["id"] = second["control"]["id"] = "second-item"
        second["control"]["issue"].update(number=901, node_id="ISSUE901")
        assignment = second["assignments"][0]
        assignment["basis"] = work_items.basis(second["control"])
        saved = assignment["execution"]
        saved.update(task_id="TASK2", session_id="SESSION2", pr=None)
        saved["approval"] = execution.approval_template(second, assignment)
        self.api.ledger["workItems"].append(second)
        other_task = deepcopy(self.task)
        other_task.update(id="TASK2", state="completed" if final_boundary else "in_progress")
        other_task["sessions"][0].update(
            id="SESSION2", task_id="TASK2", state=other_task["state"],
            prompt=execution.request(self.api, second, assignment)["prompt"])
        self.transport.values[f"agents/repos/{self.repository}/tasks/TASK2"] = other_task
        if not final_boundary:
            self.task["state"] = self.task["sessions"][0]["state"] = "in_progress"
        if unreadable:
            for identity in ("TASK1", "TASK2"):
                self.transport.values[f"agents/repos/{self.repository}/tasks/{identity}"] = {}
        self.api.persist()
        self.assertEqual(0, state.worker_slots(self.api.ledger))
        third = deepcopy(self.control)
        third.update(id="third-item")
        third["issue"].update(number=902, node_id="ISSUE902")
        self.transport.values[f"{self.prefix}/issues/902"] = {
            "number": 902, "node_id": "ISSUE902", "state": "open", "labels": []}
        self.api.control = deepcopy(third)
        read = lambda: deepcopy(third)
        approval = execution.prepare(self.api, read)["approval_template"]
        if final_boundary:
            original = self.api.start_task
            def resumed(body, before_send):
                for remote in (self.task, other_task):
                    remote["state"] = remote["sessions"][0]["state"] = "in_progress"
                return original(body, before_send)
            with patch.object(self.api, "start_task", side_effect=resumed):
                self.assertEqual("uncertain", execution.execute(self.api, read, lambda: approval)["outcome"])
        else:
            with self.assertRaisesRegex(ValueError, "capacity exhausted"):
                execution.execute(self.api, read, lambda: approval)
        self.assertEqual(3, state.worker_slots(self.api.ledger))
        self.assertEqual([], [call for call in self.transport.writes if call[0] == "POST"])

    def test_imported_quiescence_binds_session_and_current_pr_branch(self):
        execution.import_pr(self.api, self.read)
        chain = self.api.ledger["chains"][0]
        for target, key, wrong in (
                (self.task["sessions"][0], "head_ref", "foreign-branch"),
                (self.pull["head"], "ref", "foreign-branch"),
                (self.pull, "id", 99999)):
            with self.subTest(key=key):
                original = target[key]
                target[key] = wrong
                with self.assertRaisesRegex(ValueError, "branch|identity"):
                    pilot_handoff.confirm(
                        self.api, 7, self.pull["head"]["sha"], self.api.clock(), app_enabled=True,
                        address_reviews=True, fix_ci=True, resolve_conflicts=True, merge_disabled=True)
                self.assertIsNone(chain["handoff"]["confirmedAt"])
                target[key] = original
                self.assertTrue(self.api.observe(chain)["managed"])
                self.api.persist()


class UpstreamMappingTests(MappingTests):
    repository = "microsoft/aspire"

    def test_actual_local_transport_monitors_unlabeled_import_and_confirms_only_when_quiescent(self):
        self.assertEqual("tracked", execution.import_pr(self.api, self.read)["outcome"])
        fixture = self.transport
        def offline(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            return fixture(method, endpoint, body)
        with patch.object(live.HTTPTransport, "__call__", offline), \
                patch.object(local.LocalGitHub, "enabled", return_value=True), \
                patch.object(local.result_collector, "LocalCollector", return_value=None):
            api = local.LocalGitHub("fixture", 99, 500, "TRACKER99", write=True, revision="c" * 40,
                                   binding=bindings.UPSTREAM_ALL)
            api.read_authority()
            chain = api.ledger["chains"][0]
            observed = api.observe(chain)
            self.assertTrue(observed["managed"])
            self.assertEqual(("handoff-needed", chain["id"]), pilot_reminders.blocker(chain, observed))
            api.persist()
            pilot_handoff.confirm(api, 7, self.pull["head"]["sha"], api.clock(), app_enabled=True,
                                  address_reviews=True, fix_ci=True, resolve_conflicts=True, merge_disabled=True)
            self.assertEqual("watching", chain["handoff"]["phase"])
            self.task["state"] = self.task["sessions"][0]["state"] = "in_progress"
            with self.assertRaisesRegex(ValueError, "still active"):
                pilot_handoff.confirm(api, 7, self.pull["head"]["sha"], api.clock(), app_enabled=True,
                                      address_reviews=True, fix_ci=True, resolve_conflicts=True, merge_disabled=True)
            with self.assertRaisesRegex(ValueError, "prohibits PR repair"):
                api.repair_authority(chain)
            self.assertTrue(all(method == "PATCH" for method, _, _ in fixture.writes))

    def test_actual_local_monitor_and_confirm_reject_reassociated_branch_artifacts(self):
        execution.import_pr(self.api, self.read)
        fixture = self.transport
        def offline(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            return fixture(method, endpoint, body)
        with patch.object(live.HTTPTransport, "__call__", offline), \
                patch.object(local.LocalGitHub, "enabled", return_value=True), \
                patch.object(local.result_collector, "LocalCollector", return_value=None), \
                redirect_stderr(io.StringIO()):
            api = local.LocalGitHub("fixture", 99, 500, "TRACKER99", write=True, revision="c" * 40,
                                   binding=bindings.UPSTREAM_ALL)
            api.read_authority()
            chain = api.ledger["chains"][0]
            for target, key, wrong in (
                    (self.task["sessions"][0], "head_ref", "foreign-branch"),
                    (self.task["artifacts"][0]["data"], "id", 99999),
                    (self.task["artifacts"][1]["data"], "head_ref", "foreign-branch"),
                    (self.pull["head"], "ref", "foreign-branch")):
                with self.subTest(key=key):
                    original = target[key]
                    target[key] = wrong
                    observed = api.observe(chain)
                    self.assertFalse(observed["managed"])
                    self.assertIn("identity changed", observed["attention"])
                    self.assertEqual("handoff_pending", chain["handoff"]["phase"])
                    self.assertGreater(state.worker_slots(api.ledger), 0)
                    self.assertGreater(state.repository_spend(api.ledger, api.clock()), 0)
                    with self.assertRaises(ValueError):
                        pilot_handoff.confirm(
                            api, 7, self.pull["head"]["sha"], api.clock(), app_enabled=True,
                            address_reviews=True, fix_ci=True, resolve_conflicts=True, merge_disabled=True)
                    self.assertIsNone(chain["handoff"]["confirmedAt"])
                    saved = state.parse(fixture.comments[0]["body"])
                    self.assertEqual("handoff_pending", saved["chains"][0]["handoff"]["phase"])
                    target[key] = original
                    self.assertTrue(api.observe(chain)["managed"])
                    api.persist()
            self.assertTrue(all(method == "PATCH" for method, _, _ in fixture.writes))


class EntrypointTests(unittest.TestCase):
    def test_pull_head_filter_rejects_foreign_owner_and_malformed_refs(self):
        for repository in ("radical/aspire", "microsoft/aspire"):
            item = control()
            item["issue"]["repository"] = repository
            transport = work_item_github.CloudWorkItemTransport("fixture", 99, 500, item)
            owner = repository.split("/", 1)[0]
            base = f"repos/{repository}/pulls?"
            transport.validate_endpoint("GET", base + urlencode({
                "state": "all", "head": owner + ":copilot/fix-7", "page": 1, "per_page": 100}), None)
            for head in ("foreign:fix-7", owner + ":", owner + ":../fix", owner + ":fix..7",
                         owner + ":fix//7", owner + ":fix.lock", owner + ":fix/",
                         owner + ":fix:7", owner + ":fix 7", owner + ":.hidden"):
                with self.subTest(repository=repository, head=head), self.assertRaises(ValueError):
                    transport.validate_endpoint("GET", base + urlencode({"state": "all", "head": head}), None)
            with self.assertRaises(ValueError):
                transport.validate_endpoint("GET", base + urlencode([
                    ("head", owner + ":fix-7"), ("head", owner + ":fix-8")]), None)

    def test_prepare_cli_reserves_and_emits_approval_without_remote_launch(self):
        api = CloudAuthority()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = root / "item.json"
            contracts.write_json(item, control())
            with patch.object(local, "selected_token", return_value="fixture"), \
                    patch.object(local, "command", return_value="c" * 40), \
                    patch.object(local, "require_source"), patch.object(local, "require_idle_actions"), \
                    patch.object(local, "authority_lock", return_value=nullcontext()) as lock, \
                    patch.object(work_item_github, "CloudWorkItemGitHub", return_value=api), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(0, execution.main([
                    "prepare", "--item", str(item), "--tracker", "99", "--authority", "500",
                    "--tracker-node", "TRACKER99", "--workdir", str(root / "output")]))
            lock.assert_called_once()
            self.assertEqual("prepared", contracts.read_json(root / "output" / "receipt.json")["outcome"])
            self.assertEqual([], api.posts)

    def test_cloud_transport_rejects_every_noncanonical_mutation(self):
        for repository in ("radical/aspire", "microsoft/aspire"):
            item = control()
            item["issue"]["repository"] = repository
            transport = work_item_github.CloudWorkItemTransport("fixture", 99, 500, item)
            for method, endpoint, body in [
                ("POST", f"agents/repos/{repository}/tasks", {}),
                ("POST", f"repos/{repository}/issues/900/comments", {"body": "No"}),
                ("PATCH", f"repos/{repository}/issues/900", {"assignees": ["copilot"]}),
                ("POST", f"repos/{repository}/pulls", {}),
                ("POST", f"repos/{repository}/pulls/7/requested_reviewers", {"reviewers": ["Copilot"]}),
                ("PUT", f"repos/{repository}/pulls/7/merge", {}),
                ("POST", f"repos/{repository}/issues/7/labels", {"labels": ["shepherd-adopted"]}),
                ("POST", f"repos/{repository}/actions/runs/100/rerun", {}),
            ]:
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    transport.validate_endpoint(method, endpoint, body)
