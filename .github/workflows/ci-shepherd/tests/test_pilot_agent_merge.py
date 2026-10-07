"""Behavioral ownership boundaries using the existing task-service fixture."""

from contextlib import redirect_stderr, redirect_stdout
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
import io
import base64
import unittest
from unittest.mock import patch

from github import IncompleteInventory, LostResponse
from helpers import FakeClock, WorkspaceTest, compiled_step, reconciliation_evidence, result_capable
from test_pilot_github import pr
from test_pilot_lifecycle import LifecycleTransport, RUN, PREFIX, TASKS, decision
import local
import pilot
import pilot_github as github
import pilot_handoff as handoff
import pilot_patch
import pilot_reminders as reminders
import pilot_state as state
import pilot_binding as bindings
import hosted
import live
import run_report
import round as contracts


class AgentMergeTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.transport = LifecycleTransport()
        self.pull = pr()
        self.pull.update(merged=False, merged_at=None)
        self.transport.values[PREFIX + "/issues"] = [dict(self.pull, pull_request={})]
        self.transport.values[PREFIX + "/pulls/7"] = self.pull
        self.transport.values[PREFIX + "/issues/7/comments"] = [{
            "id": 20, "body": "Repair the source", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]

    def fresh(self, manual=True, collector=False):
        api = github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        api.pr_handoff = "manual" if manual else None
        if collector:
            result_capable(api)
        return api

    def prepare(self, api=None, present=False):
        api = api or self.fresh()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            packet = pilot.prepare(api, RUN, self.clock(), present=present)
        return api, packet

    def chain(self):
        return state.parse(self.transport.comments[0]["body"])["chains"][0]

    def task_writes(self):
        return [write for write in self.transport.writes if write[1] == TASKS]

    def test_direct_pr_stops_before_legacy_reads_and_never_reserves_or_repairs(self):
        original = self.transport.__call__

        def transport(method, endpoint, body):
            if any(part in endpoint for part in ("/check-runs", "/status", "/reviews", "/requested_reviewers",
                                                "/contents/", "/git/")) or endpoint == "graphql":
                self.fail("transferred PR entered the legacy repair path: " + endpoint)
            if method != "GET" and (endpoint == TASKS or "/git/" in endpoint):
                self.fail("transferred PR acquired a second coding owner")
            return original(method, endpoint, body)

        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock, api.pr_handoff = self.clock, "manual"
        for _ in range(3):
            _, packet = self.prepare(api)
            self.assertIsNone(packet)
        chain = self.chain()
        self.assertEqual("handoff_needed", chain["handoff"]["phase"])
        self.assertEqual("radical", chain["handoff"]["responsible"])
        self.assertEqual((0, [], {}), (chain["rounds"], chain["operations"], chain["dispositions"]))

    def test_persisted_transfer_survives_flag_removal_reopen_and_readoption(self):
        self.prepare()
        frozen = deepcopy(self.chain()["operations"])
        for lifecycle, labels in (
            ("closed", []), ("open", []), ("open", [{"name": "shepherd-adopted"}]),
        ):
            self.pull.update(state=lifecycle, labels=labels)
            _, packet = self.prepare(self.fresh(manual=False, collector=True))
            self.assertIsNone(packet)
            self.assertIn(self.chain()["handoff"]["phase"], {"closed", "handoff_needed"})
            self.assertEqual(frozen, self.chain()["operations"])
        self.assertEqual([], self.task_writes())

    def test_saved_active_and_unknown_tasks_hold_pending_without_changing_legacy_history(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        frozen = deepcopy(self.chain()["operations"])
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        task = self.transport.values.pop(TASKS + "/TASK1")
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        task["state"] = task["sessions"][0]["state"] = "completed"
        self.transport.values[TASKS + "/TASK1"] = task
        self.prepare()
        self.assertEqual("handoff_needed", self.chain()["handoff"]["phase"])
        self.assertEqual(frozen, self.chain()["operations"])
        self.assertEqual(1, len(self.task_writes()))

    def test_confirmed_waiting_review_blocks_transfer_until_fresh_published_completion(self):
        from test_pilot_reviews import BOT, ReviewTests
        self.transport, self.clock = ReviewTests().api()
        self.transport.values[PREFIX + "/pulls/7"].update(merged=False, merged_at=None)
        self.prepare(self.fresh(manual=False, collector=True))
        frozen = deepcopy(self.chain()["reviews"])
        self.assertEqual("waiting", frozen[0]["state"])
        for requested in ([BOT], []):
            self.transport.values[PREFIX + "/pulls/7"]["requested_reviewers"] = requested
            self.prepare()
            self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
            self.assertIn("review request unresolved", self.chain()["handoff"]["attention"])
            self.assertEqual(frozen, self.chain()["reviews"])
        self.transport.values[PREFIX + "/pulls/7/reviews"] = [{
            "id": 60, "user": BOT, "commit_id": "a" * 40, "state": "COMMENTED", "body": "",
            "submitted_at": "2026-10-04T00:01:00Z"}]
        self.clock.advance(minutes=1)
        self.prepare()
        self.assertEqual("handoff_needed", self.chain()["handoff"]["phase"])
        self.assertEqual(frozen, self.chain()["reviews"])
        self.assertEqual(1, len([effect for effect in self.transport.writes
                                 if effect[1].endswith("/requested_reviewers")]))
        self.assertEqual([], self.task_writes())

    def test_reserved_legacy_effect_prevents_ready_transfer_after_last_authorization(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        chain, operation = api.ledger["chains"][0], api.ledger["chains"][0]["operations"][0]
        api.guard(chain, packet["observation"])
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        with self.assertRaises(ValueError):
            api.guard(chain, packet["observation"])
        self.assertEqual([], self.task_writes())
        self.assertEqual("reserved", self.chain()["operations"][0]["state"])

    def test_stale_native_packet_cannot_dispatch_after_conversion_or_change_frozen_history(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.prepare()
        frozen = deepcopy(self.chain()["operations"])
        fresh = self.fresh(manual=False, collector=True)
        result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("handoff; no repair", result["outcome"])
        self.assertEqual([], self.task_writes())
        frozen[0]["state"] = "no-send"
        self.assertEqual(frozen, self.chain()["operations"])
        self.prepare()
        self.assertEqual("handoff_needed", self.chain()["handoff"]["phase"])

    def test_reminder_deduplicates_and_controller_comments_do_not_reset_progress(self):
        self.prepare(present=True)
        progress = self.chain()["handoff"]["progressAt"]
        self.clock.advance(seconds=61)
        self.prepare(present=True)
        self.prepare(present=True)
        pings = [body["body"] for method, endpoint, body in self.transport.writes
                 if method == "POST" and endpoint.endswith("/7/comments") and "human-reminder" in body["body"]]
        self.assertEqual(1, len(pings))
        self.assertEqual(progress, self.chain()["handoff"]["progressAt"])
        self.assertEqual("confirmed", self.chain()["reminder"]["sendState"])

    def test_unknown_merge_evidence_is_not_ready_or_closed(self):
        self.pull.pop("merged")
        self.prepare(present=True)
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        self.assertEqual([], self.task_writes())
        self.assertNotIn("reminder", self.chain())

    def test_configuration_is_explicit_fork_only_and_rejects_unknown_modes(self):
        environment = {"CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_TRACKER": "99",
                       "CI_SHEPHERD_AUTHORITY_COMMENT": "500", "CI_SHEPHERD_TRACKER_NODE": "TRACKER99",
                       "CI_SHEPHERD_PR_HANDOFF": "manual"}
        self.assertEqual("manual", pilot.configuration(environment)["prHandoff"])
        environment["CI_SHEPHERD_PR_HANDOFF"] = "automatic"
        with self.assertRaises(ValueError):
            pilot.configuration(environment)

    def issue(self, quarantine=False, number=8):
        value = {"id": 1000 + number, "number": number, "node_id": f"NODE{number}", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}, {"name": "bug"}],
                 "title": "Fixture issue", "body": "Implement a focused fix",
                 "html_url": f"https://github.com/radical/aspire/issues/{number}"}
        if quarantine:
            value["labels"].append({"name": "quarantined-test"})
        self.transport.values[PREFIX + "/issues"] = [value]
        self.transport.values[PREFIX + f"/issues/{number}"] = value
        return value

    def finish_initial(self):
        task = self.transport.values[TASKS + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        child = pr(9)
        child.update(merged=False, merged_at=None, commits=1)
        self.transport.values[PREFIX + "/pulls"] = [child]
        self.transport.values[PREFIX + "/pulls/9"] = child
        self.transport.values[PREFIX + "/pulls/9/commits"] = [
            {"sha": "a" * 40, "commit": {"message": "Mitigate flaky fixture\n\nRefs #8"}}]
        self.transport.values[PREFIX + "/git/ref/heads/fix-9"] = {
            "ref": "refs/heads/fix-9", "object": {"sha": "a" * 40}}
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": 1009, "global_id": "NODE9"}},
            {"provider": "github", "type": "branch", "data": {"head_ref": "fix-9", "base_ref": "main"}}]
        return child, task

    def start_initial(self, lost=False):
        self.issue()
        api, packet = self.prepare()
        self.assertIsNotNone(packet, "Initial issue qualification must not require a PR result collector")
        self.transport.lose_send_response = lost
        outcome = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        return packet, outcome

    def test_initial_issue_dispatches_once_without_result_collector_or_legacy_accounting(self):
        packet, outcome = self.start_initial()
        self.assertEqual("waiting", outcome["outcome"])
        self.assertEqual("TASK1", self.chain()["handoff"]["taskId"])
        self.assertEqual((0, []), (self.chain()["rounds"], self.chain()["operations"]))
        self.assertTrue(self.task_writes()[0][2]["create_pull_request"])
        prompt = self.task_writes()[0][2]["prompt"]
        self.assertIn("draft PR", prompt)
        self.assertIn("[NO-MERGE]", prompt)
        self.assertIn("'Refs #N'", prompt)
        self.assertIn("handoff phase initial", prompt)
        self.prepare()
        child, task = self.finish_initial()
        self.prepare()
        self.assertEqual((9, "NODE9", "handoff_needed"), (
            self.chain()["child"], self.chain()["childNode"], self.chain()["handoff"]["phase"]))
        self.prepare(self.fresh(manual=False))
        self.assertEqual(1, len(self.task_writes()))
        self.assertEqual([], [effect for effect in self.transport.writes if effect[1].endswith("/9/labels")])

    def test_actual_local_initial_sweep_presents_then_dispatches_once_with_matching_guard(self):
        self.issue()
        api = self.fresh()
        api.token, api.enabled = "fixture", lambda: True
        calls = []

        def executor(directory, packet, token):
            calls.append(packet)
            directory.mkdir()
            contracts.write_json(directory / "usage.json", {"ai_credits": 2})
            return reconciliation_evidence(decision(packet))

        with patch.object(live, "clock", self.clock), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            outcome = local.run_sweep(api, self.work, RUN, {}, executor=executor)
        self.assertEqual("waiting", outcome["outcome"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(self.task_writes()))
        self.assertIsNotNone(self.chain()["statusId"])
        self.assertEqual("TASK1", self.chain()["handoff"]["taskId"])

    def test_actual_local_sweeps_bound_initial_workers_until_verified_release(self):
        issues = [self.issue(number=number) for number in (8, 10, 11)]
        self.transport.values[PREFIX + "/issues"] = issues
        calls = []
        runs = []

        def executor(directory, packet, token):
            calls.append(packet["observation"]["number"])
            directory.mkdir()
            contracts.write_json(directory / "usage.json", {"ai_credits": 2})
            return reconciliation_evidence(decision(packet))

        def sweep():
            api = self.fresh()
            api.token, api.enabled = "fixture", lambda: True
            directory = self.work / f"sweep-{len(runs)}"
            directory.mkdir()
            runs.append(directory)
            return local.run_sweep(api, directory, RUN, {}, executor=executor)

        with patch.object(live, "clock", self.clock), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual("waiting", sweep()["outcome"])
            self.assertEqual("waiting", sweep()["outcome"])
            for _ in range(2):
                self.assertEqual("observed; no inference", sweep()["outcome"])
            self.assertEqual([8, 10], calls)
            self.assertEqual(2, len(self.task_writes()))
            self.finish_initial()
            self.assertEqual("waiting", sweep()["outcome"])
        self.assertEqual([8, 10, 11], calls)
        self.assertEqual(3, len(self.task_writes()))
        self.assertEqual("handoff_needed", self.chain()["handoff"]["phase"])
        self.assertEqual(2, state.worker_slots(state.parse(self.transport.comments[0]["body"])))

    def test_actual_local_terminal_unmapped_tasks_release_slots_without_replacement_or_handoff(self):
        issues = [self.issue(number=number) for number in (8, 10, 11)]
        self.transport.values[PREFIX + "/issues"] = issues
        calls, runs = [], []

        def executor(directory, packet, token):
            calls.append(packet["observation"]["number"])
            directory.mkdir()
            contracts.write_json(directory / "usage.json", {"ai_credits": 2})
            return reconciliation_evidence(decision(packet))

        def sweep():
            api = self.fresh()
            api.token, api.enabled = "fixture", lambda: True
            directory = self.work / f"sweep-{len(runs)}"
            directory.mkdir()
            runs.append(directory)
            return local.run_sweep(api, directory, RUN, {}, executor=executor)

        with patch.object(live, "clock", self.clock), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for _ in range(2):
                self.assertEqual("waiting", sweep()["outcome"])
            before = state.parse(self.transport.comments[0]["body"])
            for number in (1, 2):
                task = self.transport.values[TASKS + f"/TASK{number}"]
                task["state"] = task["sessions"][0]["state"] = "completed"
            self.assertEqual("waiting", sweep()["outcome"])
            self.assertEqual("observed; no inference", sweep()["outcome"])
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual([8, 10, 11], calls)
        self.assertEqual(3, len(self.task_writes()))
        self.assertEqual(1, state.worker_slots(after))
        self.assertEqual(state.repository_spend(before, self.clock()), state.repository_spend(after, self.clock()))
        for number, task_id in ((8, "TASK1"), (10, "TASK2")):
            chain = state.find_chain(after, number)
            self.assertIsNone(chain["child"])
            self.assertEqual(("initial", "known", task_id), tuple(
                chain["handoff"][key] for key in ("phase", "sendState", "taskId")))
            self.assertIn("no unique verified PR", chain["handoff"]["attention"])

    def test_terminal_capacity_receipt_is_revoked_on_unknown_or_nonterminal_task_evidence(self):
        for evidence in ("running", "active-session", "missing-session", "foreign-task", "unavailable"):
            with self.subTest(evidence=evidence):
                self.transport = LifecycleTransport()
                self.start_initial()
                task = self.transport.values[TASKS + "/TASK1"]
                task["state"] = task["sessions"][0]["state"] = "completed"
                api, packet = self.prepare()
                self.assertIsNone(packet)
                self.assertEqual(0, state.worker_slots(api.ledger))
                saved = deepcopy(task)
                if evidence == "running":
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
                elif evidence == "active-session":
                    task["sessions"][0]["state"] = "in_progress"
                elif evidence == "missing-session":
                    task["sessions"] = []
                elif evidence == "foreign-task":
                    task["creator"]["id"] = 1
                else:
                    del self.transport.values[TASKS + "/TASK1"]
                api, packet = self.prepare()
                self.assertIsNone(packet)
                self.assertEqual(1, state.worker_slots(api.ledger))
                self.assertFalse(self.chain()["handoff"].get("taskTerminal", False))
                self.assertIsNotNone(self.chain()["handoff"]["attention"])
                self.assertEqual("TASK1", self.chain()["handoff"]["taskId"])
                self.assertEqual("initial", self.chain()["handoff"]["phase"])
                self.assertEqual(1, len(self.task_writes()))
                self.transport.values[TASKS + "/TASK1"] = saved
                api, _ = self.prepare()
                self.assertEqual(0, state.worker_slots(api.ledger))

    def test_resumed_unmapped_tasks_reclaim_capacity_at_final_initial_post_guard(self):
        issues = [self.issue(number=number) for number in (8, 10, 11)]
        self.transport.values[PREFIX + "/issues"] = issues
        for _ in range(2):
            api, packet = self.prepare()
            pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        for number in (1, 2):
            task = self.transport.values[TASKS + f"/TASK{number}"]
            task["state"] = task["sessions"][0]["state"] = "completed"
        api, packet = self.prepare()
        self.assertEqual(11, packet["observation"]["number"])
        original = api.adoption_effect_guard
        checks = []

        def guard():
            checks.append(True)
            if len(checks) == 2:
                for number in (1, 2):
                    task = self.transport.values[TASKS + f"/TASK{number}"]
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
            original()

        with patch.object(api, "adoption_effect_guard", guard), redirect_stderr(io.StringIO()):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("human", result["outcome"])
        self.assertIn("capacity exhausted", result["error"])
        self.assertEqual(2, len(self.task_writes()))
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(2, state.worker_slots(after))
        for number in (8, 10):
            self.assertFalse(state.find_chain(after, number)["handoff"].get("taskTerminal", False))

    def test_resumed_compact_tasks_block_unconverted_legacy_final_post(self):
        issues = [self.issue(number=number) for number in (8, 10)]
        self.transport.values[PREFIX + "/issues"] = issues
        for _ in issues:
            api, packet = self.prepare()
            pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        for number in (1, 2):
            task = self.transport.values[TASKS + f"/TASK{number}"]
            task["state"] = task["sessions"][0]["state"] = "completed"
        self.transport.values[PREFIX + "/issues"] = [*issues, dict(self.pull, pull_request={})]
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.assertEqual(7, packet["observation"]["number"])
        original = api.guard
        resumed = []

        def guard(chain, observation, **kwargs):
            if chain["operations"][-1]["state"] == "sent" and not resumed:
                resumed.append(True)
                for number in (1, 2):
                    task = self.transport.values[TASKS + f"/TASK{number}"]
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
            return original(chain, observation, **kwargs)

        with patch.object(api, "guard", guard), redirect_stderr(io.StringIO()):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual([True], resumed)
        self.assertEqual("no-send", result["outcome"])
        self.assertEqual(2, len(self.task_writes()))
        after = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(2, state.worker_slots(after))
        self.assertEqual("no-send", state.find_chain(after, 7)["operations"][-1]["state"])
        for number in (8, 10):
            self.assertFalse(state.find_chain(after, number)["handoff"].get("taskTerminal", False))

    def test_mixed_legacy_and_compact_uncertain_send_block_another_initial_worker(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        issue = self.issue()
        self.transport.values[PREFIX + "/issues"] = [dict(self.pull, pull_request={}), issue]
        self.transport.lose_send_response = True
        api, packet = self.prepare()
        self.assertIsNotNone(packet, "one legacy worker leaves one compact slot")
        result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("uncertain", result["outcome"])
        third = self.issue(number=10)
        self.transport.values[PREFIX + "/issues"] = [dict(self.pull, pull_request={}), issue, third]
        for _ in range(2):
            api, packet = self.prepare()
            self.assertIsNone(packet)
        self.assertEqual(2, state.worker_slots(api.ledger))
        self.assertEqual(2, len(self.task_writes()))
        self.assertIsNone(state.find_chain(api.ledger, 8)["handoff"]["taskId"])
        self.assertEqual("idle", state.find_chain(api.ledger, 10)["handoff"]["sendState"])

    def test_initial_settlement_rechecks_capacity_without_double_counting_own_reservation(self):
        self.issue()
        api, packet = self.prepare()
        for number in (10, 11):
            chain = state.adopt(api.ledger, number, "issue", f"NODE{number}")
            handoff.enroll(api, chain, self.clock())
            chain["handoff"]["sendState"] = "uncertain"
        api.persist()
        result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("human", result["outcome"])
        self.assertIn("capacity exhausted", result["error"])
        self.assertEqual([], self.task_writes())
        self.assertEqual("human", self.chain()["handoff"]["sendState"])
        self.assertEqual(2, state.worker_slots(state.parse(self.transport.comments[0]["body"])))

    def test_capacity_taken_after_sent_receipt_still_prevents_initial_post(self):
        self.issue()
        api, packet = self.prepare()
        original = api.adoption_effect_guard
        checks = []

        def guard():
            checks.append(True)
            if len(checks) == 2:
                self.assertEqual("sent", self.chain()["handoff"]["sendState"])
                for number in (10, 11):
                    chain = state.adopt(api.ledger, number, "issue", f"NODE{number}")
                    handoff.enroll(api, chain, self.clock())
                    chain["handoff"]["sendState"] = "uncertain"
                api.persist()
            original()

        with patch.object(api, "adoption_effect_guard", guard):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual(2, len(checks))
        self.assertEqual("human", result["outcome"])
        self.assertIn("capacity exhausted", result["error"])
        self.assertEqual([], self.task_writes())
        self.assertEqual("human", self.chain()["handoff"]["sendState"])
        self.assertEqual(2, state.worker_slots(state.parse(self.transport.comments[0]["body"])))

    def test_compact_workers_also_block_unconverted_legacy_worker_admission(self):
        issues = [self.issue(number=number) for number in (8, 10)]
        self.transport.values[PREFIX + "/issues"] = issues
        for _ in issues:
            api, packet = self.prepare()
            self.assertIsNotNone(packet)
            pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.transport.values[PREFIX + "/issues"] = [*issues, dict(self.pull, pull_request={})]
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.assertIsNone(packet)
        legacy = state.find_chain(api.ledger, 7)
        self.assertNotIn("handoff", legacy)
        self.assertEqual([], legacy["operations"])
        self.assertIn("capacity exhausted", api.admission_reasons[legacy["id"]])
        self.assertEqual(2, len(self.task_writes()))

    def test_local_report_shows_compact_initial_task_without_claiming_legacy_spend(self):
        before = state.parse(self.transport.comments[0]["body"])
        packet, outcome = self.start_initial()
        after = state.parse(self.transport.comments[0]["body"])
        report = run_report.render(RUN, "fork", before, after, outcome, packet,
                                   "2026-10-04T00:00:00Z", "2026-10-04T00:00:01Z")
        self.assertIn(f"https://github.com/radical/aspire/tasks/{outcome['taskId']}", report)
        self.assertIn("New saved initial implementation task", report)
        self.assertIn("Initial qualification/implementation usage is not collected", report)
        self.assertEqual([], after["chains"][0]["operations"])

    def test_lost_initial_post_never_redispatches_or_claims_quiescence(self):
        packet, outcome = self.start_initial(lost=True)
        self.assertEqual("uncertain", outcome["outcome"])
        for _ in range(3):
            self.prepare()
        self.assertEqual("initial", self.chain()["handoff"]["phase"])
        self.assertEqual("uncertain", self.chain()["handoff"]["sendState"])
        self.assertIsNone(self.chain()["handoff"]["taskId"])
        self.assertEqual(1, len(self.task_writes()))

    def test_terminal_task_with_active_session_cannot_finish_initial_handoff(self):
        self.start_initial()
        child, task = self.finish_initial()
        task["sessions"][0]["state"] = "in_progress"
        self.prepare()
        self.assertIsNone(self.chain()["child"])
        self.assertEqual("initial", self.chain()["handoff"]["phase"])
        self.assertIsNotNone(self.chain()["handoff"]["attention"])

    def test_manual_confirmation_checks_exact_pr_head_and_never_enables_an_owner(self):
        self.prepare()
        api = self.fresh(manual=False)
        with self.assertRaises(ValueError):
            local.confirm_handoff(api, 7, "b" * 40, self.clock(), app_enabled=True, merge_disabled=True)
        with self.assertRaises(ValueError):
            local.confirm_handoff(api, 7, "a" * 40, self.clock(), app_enabled=True, merge_disabled=False)
        local.confirm_handoff(self.fresh(manual=False), 7, "a" * 40, self.clock(),
                              app_enabled=True, merge_disabled=True)
        self.assertEqual("watching", self.chain()["handoff"]["phase"])
        self.assertEqual([], self.task_writes())

    def test_watching_stale_reminder_resets_only_after_substantive_head_progress(self):
        self.prepare()
        local.confirm_handoff(self.fresh(), 7, "a" * 40, self.clock(),
                              app_enabled=True, merge_disabled=True)
        self.prepare(present=True)
        self.clock.advance(seconds=61)
        self.prepare(present=True)
        first = self.chain()["reminder"]["id"]
        self.pull["head"]["sha"] = "b" * 40
        self.prepare(present=True)
        self.assertNotEqual(first, self.chain()["reminder"]["id"])
        self.assertEqual("observed", self.chain()["reminder"]["sendState"])
        self.assertEqual(self.clock().isoformat().replace("+00:00", "Z"), self.chain()["handoff"]["progressAt"])

    def test_quarantine_associations_are_unsupported_even_without_closing_keywords(self):
        self.start_initial()
        self.transport.values[PREFIX + "/issues/8"]["labels"].append({"name": "quarantined-test"})
        self.finish_initial()
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        self.assertIn("unverified", self.chain()["handoff"]["attention"])
        commits = self.transport.values[PREFIX + "/pulls/9/commits"]
        commits[0]["commit"]["message"] = "Fix flaky fixture\n\nCLOSES: #8"
        self.prepare()
        self.assertIn("keyword", self.chain()["handoff"]["attention"])
        self.assertEqual("open", self.transport.values[PREFIX + "/issues/8"]["state"])

    def test_closed_unmerged_never_mutates_origin_labels_or_closes_issue(self):
        self.start_initial()
        child, _ = self.finish_initial()
        child["state"] = "closed"
        self.prepare(present=True)
        self.assertEqual("closed", self.chain()["handoff"]["phase"])
        self.assertEqual("open", self.transport.values[PREFIX + "/issues/8"]["state"])
        self.assertEqual([], [effect for effect in self.transport.writes if effect[0] == "DELETE"])

    def test_verified_merge_removes_only_existing_adoption_label_once(self):
        self.start_initial()
        child, _ = self.finish_initial()
        child.update(state="closed", merged=True, merged_at="2026-10-04T00:01:00Z")
        original = self.transport.__call__

        def transport(method, endpoint, body):
            if method == "DELETE":
                self.assertEqual((PREFIX + "/issues/8/labels/shepherd-adopted", None), (endpoint, body))
                self.transport.writes.append((method, endpoint, body))
                origin = self.transport.values[PREFIX + "/issues/8"]
                origin["labels"] = [label for label in origin["labels"] if label["name"] != "shepherd-adopted"]
                return github.Response(deepcopy(origin["labels"]), {}, 200)
            return original(method, endpoint, body)

        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        self.prepare(api)
        self.prepare(api)
        self.assertEqual("merged", self.chain()["handoff"]["phase"])
        self.assertEqual("confirmed", self.chain()["handoff"]["mergeLabel"])
        self.assertEqual(1, len([effect for effect in self.transport.writes if effect[0] == "DELETE"]))
        self.assertEqual([{"name": "bug"}], self.transport.values[PREFIX + "/issues/8"]["labels"])
        self.assertEqual("open", self.transport.values[PREFIX + "/issues/8"]["state"])

    def test_transfer_after_final_legacy_guard_holds_pending_even_if_post_happens(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        chain, operation = api.ledger["chains"][0], api.ledger["chains"][0]["operations"][0]
        original = api.guard
        guards = []

        def guard(*args, **kwargs):
            fresh = original(*args, **kwargs)
            guards.append(fresh)
            if len(guards) == 2:
                self.prepare()
                self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
                self.assertEqual("sent", self.chain()["operations"][0]["state"])
            return fresh

        with patch.object(api, "guard", guard), redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):
                pilot.dispatch(api, chain, operation, packet, self.clock())
        self.assertEqual(2, len(guards))
        self.assertEqual(1, len(self.task_writes()))
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        self.assertIn("unresolved", self.chain()["handoff"]["attention"])

    def test_transfer_before_final_guard_settles_definite_no_send_not_an_unknown_post(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        chain, operation = api.ledger["chains"][0], api.ledger["chains"][0]["operations"][0]
        original = api.guard
        guards = []

        def guard(*args, **kwargs):
            guards.append(True)
            if len(guards) == 2:
                self.prepare()
                self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
            return original(*args, **kwargs)

        with patch.object(api, "guard", guard), redirect_stderr(io.StringIO()):
            outcome = pilot.dispatch(api, chain, operation, packet, self.clock())
        self.assertEqual("no-send", outcome["outcome"])
        self.assertEqual([], self.task_writes())
        self.assertEqual("no-send", self.chain()["operations"][0]["state"])
        self.prepare()
        self.assertEqual("handoff_needed", self.chain()["handoff"]["phase"])

    def test_real_lost_legacy_post_remains_unknown_not_definite_no_send(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.transport.lose_send_response = True
        outcome = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("uncertain", outcome["outcome"])
        self.prepare()
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        self.assertEqual("uncertain", self.chain()["operations"][0]["state"])
        self.assertEqual(1, len(self.task_writes()))

    def test_stale_valid_inline_proposal_cannot_read_source_or_publish_git_objects(self):
        self.transport.values[PREFIX + "/pulls/7/files"] = [
            {"sha": "d" * 40, "filename": pilot_patch.SOURCE, "status": "modified"}]
        for path, content in {pilot_patch.SOURCE: "def normalize_label(value):\n    return value\n",
                              pilot_patch.TEST: "import unittest\n"}.items():
            self.transport.values[f"{PREFIX}/contents/{path}"] = {
                "type": "file", "path": path, "size": len(content.encode()), "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode()}
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        value = decision(packet)
        value.update(action="patch", replacement="def normalize_label(value):\n    return value.strip()\n")
        result = pilot.settle(api, packet, reconciliation_evidence(value), 2, self.clock())
        self.assertEqual("validate", result["outcome"])
        self.prepare()
        self.transport.reads.clear()
        with self.assertRaisesRegex(ValueError, "handoff"):
            pilot_patch.publish(api, api.ledger["chains"][0], packet["observation"], result["proposal"], {})
        self.assertEqual([], [read for read in self.transport.reads if "/contents/" in read[1]])
        self.assertEqual([], [effect for effect in self.transport.writes if "/git/" in effect[1]])

    def test_hosted_stale_publication_settles_only_unsent_admission_before_validation_reads(self):
        _, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.prepare()
        before = self.chain()["operations"][0]
        trusted = self.work / "trusted"
        trusted.mkdir()
        contracts.write_json(trusted / "packet.json", packet)
        contracts.write_json(trusted / "local-request.json", {"proposal": {}, "dispositions": {}})
        result_path = self.work / "published.json"
        with patch.dict(pilot.os.environ, {"CI_SHEPHERD_ENABLE": "true"}), \
                patch.object(contracts, "host_run", return_value=RUN), \
                patch.object(pilot, "hosted_api", return_value=self.fresh()), \
                patch.object(live, "clock", self.clock), \
                patch.object(pilot_patch, "publish", side_effect=AssertionError("no validation/source/publication")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = pilot.main(["publish", "--trusted", str(trusted), "--result", str(result_path),
                                 "--evidence", str(self.work / "missing-evidence.json")])
        self.assertEqual(0, result)
        self.assertEqual("handoff; no repair", contracts.read_json(result_path)["outcome"])
        before["state"] = "no-send"
        self.assertEqual(before, self.chain()["operations"][0])
        self.assertEqual([], self.task_writes())

    def test_initial_artifact_ref_mismatch_blocks_handoff_and_never_redispatches(self):
        self.start_initial()
        self.finish_initial()
        self.transport.values[PREFIX + "/git/ref/heads/fix-9"]["object"]["sha"] = "b" * 40
        self.prepare()
        self.assertIsNone(self.chain()["child"])
        self.assertIsNotNone(self.chain()["handoff"]["attention"])
        self.assertEqual(1, len(self.task_writes()))

    def test_lost_reminder_response_reconciles_owned_receipt_without_reposting(self):
        self.prepare(present=True)
        original = self.transport.__call__

        def transport(method, endpoint, body):
            response = original(method, endpoint, body)
            if method == "POST" and "human-reminder" in body.get("body", ""):
                raise LostResponse("posted comment; receipt lost")
            return response

        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        self.clock.advance(seconds=61)
        self.prepare(api, present=True)
        self.prepare(api, present=True)
        self.assertEqual("confirmed", self.chain()["reminder"]["sendState"])
        self.assertEqual(1, len([effect for effect in self.transport.writes
                               if effect[0] == "POST" and "human-reminder" in effect[2].get("body", "")]))

    def test_clock_rollback_and_unknown_observation_do_not_send_stale_reminders(self):
        self.prepare(present=True)
        self.clock.advance(seconds=-1)
        self.prepare(present=True)
        self.assertEqual("observed", self.chain()["reminder"]["sendState"])
        self.clock.advance(seconds=62)
        self.pull.pop("merged")
        self.prepare(present=True)
        self.assertEqual("observed", self.chain()["reminder"]["sendState"])
        self.assertEqual([], [effect for effect in self.transport.writes
                              if effect[0] == "POST" and "human-reminder" in effect[2].get("body", "")])

    def test_transport_allows_only_exact_fork_adoption_removal(self):
        transport = github.PilotTransport("fixture", write=True)
        transport.validate_endpoint("DELETE", PREFIX + "/issues/8/labels/shepherd-adopted", None)
        for endpoint in ("repos/microsoft/aspire/issues/8/labels/shepherd-adopted",
                         PREFIX + "/issues/121/labels/shepherd-adopted",
                         PREFIX + "/issues/8/labels/bug",
                         PREFIX + "/issues/8/labels/shepherd-adopted?extra=true"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                transport.validate_endpoint("DELETE", endpoint, None)
        with self.assertRaises(ValueError):
            github.PilotTransport("fixture", write=True, binding=bindings.UPSTREAM_ALL).validate_endpoint(
                "DELETE", PREFIX + "/issues/8/labels/shepherd-adopted", None)

    def test_real_transport_accepts_and_recognizes_both_handoff_reminders(self):
        self.prepare(present=True)
        transport = github.PilotTransport("fixture", write=True)
        value = self.chain()["reminder"]
        for kind in ("handoff-needed", "watching-stale"):
            with self.subTest(kind=kind):
                value["kind"] = kind
                body = reminders.render(value, "radical/aspire", 7)
                transport.validate_endpoint("POST", PREFIX + "/issues/7/comments", {"body": body})
                self.assertTrue(reminders.valid_body(body, "radical/aspire", 7))

    def test_existing_legacy_issue_worker_drains_and_maps_child_without_another_post(self):
        self.issue()
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        frozen = deepcopy(self.chain()["operations"])
        self.prepare()
        self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
        self.finish_initial()
        self.prepare()
        self.assertEqual((9, "NODE9", "handoff_needed"), (
            self.chain()["child"], self.chain()["childNode"], self.chain()["handoff"]["phase"]))
        self.assertEqual(frozen, self.chain()["operations"])
        self.assertEqual(1, len(self.task_writes()))

    def test_child_mapping_then_unreadable_pr_retains_child_identity_for_status(self):
        self.start_initial()
        self.finish_initial()
        original = self.transport.__call__
        reads = []

        def transport(method, endpoint, body):
            if method == "GET" and endpoint == PREFIX + "/pulls/9":
                reads.append(endpoint)
                if len(reads) == 2:
                    raise IncompleteInventory("PR disappeared after child mapping")
            return original(method, endpoint, body)

        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        _, packet = self.prepare(api, present=True)
        self.assertIsNone(packet)
        self.assertEqual((9, "NODE9"), (self.chain()["child"], self.chain()["childNode"]))
        self.assertIn("disappeared", self.chain()["handoff"]["attention"])
        self.assertEqual(1, len(self.task_writes()))

    def test_actual_readonly_observe_keeps_unmapped_completed_task_coherent_without_writes(self):
        self.start_initial()
        self.finish_initial()
        before = self.transport.comments[0]["body"]
        self.transport.writes.clear()
        output = io.StringIO()
        from test_local import LocalTests
        helper = LocalTests("test_api_contract_requires_explicit_closed_subject_before_credentials")
        with patch.object(local, "command", side_effect=helper.command), \
                patch.object(local.github, "PilotTransport", return_value=self.transport), \
                patch.object(live, "clock", self.clock), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = local.main(["observe", "--target", "fork",
                                 "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                                 "--workdir", str(self.work)])
        self.assertEqual(0, result)
        self.assertEqual([], self.transport.writes)
        self.assertEqual(before, self.transport.comments[0]["body"])
        self.assertIsNone(self.chain()["child"])
        self.assertIn("Read-only", output.getvalue())

    def test_definite_child_mapping_publication_failure_keeps_fallback_identity_coherent(self):
        self.start_initial()
        self.finish_initial()
        api = self.fresh()
        original = api.persist
        rejected = []

        def persist():
            if api.ledger["chains"][0]["child"] == 9 and not rejected:
                rejected.append(True)
                raise ValueError("Mapping publication rejected before effect")
            return original()

        api.persist = persist
        _, packet = self.prepare(api, present=True)
        self.assertIsNone(packet)
        self.assertEqual((9, "NODE9"), (self.chain()["child"], self.chain()["childNode"]))
        self.assertIn("Mapping publication rejected", self.chain()["handoff"]["attention"])
        self.assertEqual(1, len(self.task_writes()))

    def test_opt_in_at_settlement_without_an_intervening_sweep_blocks_old_pr_packet(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        fresh = self.fresh(collector=True)
        result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("handoff; no repair", result["outcome"])
        self.assertEqual([], self.task_writes())
        self.assertEqual("no-send", self.chain()["operations"][0]["state"])

    def test_confirm_cli_authenticates_existing_operator_without_native_engine(self):
        self.prepare()
        from test_local import LocalTests
        helper = LocalTests("test_api_contract_requires_explicit_closed_subject_before_credentials")
        with patch.object(local, "command", side_effect=helper.command) as commands, \
                patch.object(local.github, "PilotTransport", return_value=self.transport), \
                patch.object(local, "authority_lock", return_value=nullcontext()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = local.main(["confirm-handoff", "--target", "fork", "--pr", "7",
                                 "--expected-head", "a" * 40, "--app-enabled", "--merge-disabled",
                                 "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                                 "--workdir", str(self.work)])
        self.assertEqual(0, result)
        self.assertEqual("watching", self.chain()["handoff"]["phase"])
        self.assertEqual([], [call for call in commands.call_args_list if call.args[0][0] == "copilot"])

    def test_confirm_cli_rejects_missing_app_assertions_before_credentials(self):
        arguments = ["confirm-handoff", "--target", "fork", "--pr", "7", "--expected-head", "a" * 40,
                     "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                     "--workdir", str(self.work)]
        with patch.object(local, "command", side_effect=AssertionError("must not read credentials")), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            local.main(arguments)
        self.assertEqual(2, rejected.exception.code)

    def test_local_core_respects_the_configured_45_minute_reminder_without_default_workflow_changes(self):
        with patch.dict(local.os.environ, {"CI_SHEPHERD_REMINDER_DELAY_SECONDS": "2700"}), \
                patch.object(local.github, "PilotTransport", return_value=self.transport):
            api = local.LocalGitHub("fixture", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
        self.assertEqual(2700, api.reminder_delay)
        api.clock, api.pr_handoff, api.enabled = self.clock, "manual", lambda: True
        with patch.object(local, "require_source"), patch.object(local, "require_idle_actions"):
            self.prepare(api, present=True)
            self.clock.advance(seconds=2699)
            self.prepare(api, present=True)
            self.assertEqual("observed", self.chain()["reminder"]["sendState"])
            self.clock.advance(seconds=1)
            self.prepare(api, present=True)
            self.assertEqual("confirmed", self.chain()["reminder"]["sendState"])
            self.prepare(api, present=True)
        reminders_sent = [effect for effect in self.transport.writes
                          if effect[0] == "POST" and "human-reminder" in effect[2].get("body", "")]
        self.assertEqual(1, len(reminders_sent))

    def test_emitted_prepare_settle_and_publish_receive_explicit_manual_mode(self):
        from test_pilot_hosted import expression
        context = {"vars.CI_SHEPHERD_ENABLE": "true", "vars.CI_SHEPHERD_TRACKER": "99",
                   "vars.CI_SHEPHERD_AUTHORITY_COMMENT": "500", "vars.CI_SHEPHERD_TRACKER_NODE": "TRACKER99",
                   "vars.CI_SHEPHERD_PR_HANDOFF": "manual", "github.event_name": "workflow_dispatch",
                   "inputs.target": "fork"}
        for name in ("Prepare host-owned envelope", "Settle native billing before authorizing an action",
                     "Publish without checking out or executing PR code"):
            keys = {"CI_SHEPHERD_ENABLE", "CI_SHEPHERD_TRACKER", "CI_SHEPHERD_AUTHORITY_COMMENT",
                    "CI_SHEPHERD_TRACKER_NODE", "CI_SHEPHERD_PR_HANDOFF"}
            environment = {key: expression(value, context) if value.startswith("${{") else value
                           for key, value in compiled_step(name)["env"].items() if key in keys}
            self.assertEqual("manual", environment.get("CI_SHEPHERD_PR_HANDOFF"), name)
            self.assertEqual("manual", pilot.configuration(environment)["prHandoff"])

    def test_hosted_transferred_pr_outputs_skip_native_and_local_runner_never_executes(self):
        config = {"CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_TRACKER": "99",
                  "CI_SHEPHERD_AUTHORITY_COMMENT": "500", "CI_SHEPHERD_TRACKER_NODE": "TRACKER99",
                  "CI_SHEPHERD_PR_HANDOFF": "manual", "SHEPHERD_MODE": "pilot",
                  "GITHUB_OUTPUT": str(self.work / "output")}
        with patch.dict(pilot.os.environ, config, clear=True), \
                patch.object(contracts, "host_run", return_value=RUN), \
                patch.object(hosted, "require_host"), \
                patch.object(github, "PilotTransport", return_value=self.transport), \
                patch.object(live, "clock", self.clock), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = hosted.main(["prepare", "--workdir", str(self.work / "hosted")])
        self.assertEqual(0, result)
        self.assertIsNone(contracts.read_json(self.work / "hosted/trusted/packet.json"))
        outputs = (self.work / "output").read_text().splitlines()
        self.assertEqual(["active=false", "pilot=false"], outputs[:2])
        api = self.fresh(manual=False)
        api.enabled = lambda: True
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = local.run_sweep(api, self.work, RUN, {}, executor=lambda *_: self.fail("no native inference"))
        self.assertEqual("observed; no inference", result["outcome"])
        self.assertEqual([], self.task_writes())

    def test_initial_pre_post_guard_rejection_is_definitive_and_never_sends(self):
        self.issue()
        api, packet = self.prepare()
        original = api.adoption_effect_guard
        checks = []

        def guard():
            checks.append(True)
            if len(checks) == 2:
                self.transport.values[PREFIX + "/issues/8"]["labels"] = []
            original()

        with patch.object(api, "adoption_effect_guard", guard):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual("human", result["outcome"])
        self.assertEqual("human", self.chain()["handoff"]["sendState"])
        self.assertEqual([], self.task_writes())
        self.assertIsNone(self.chain()["handoff"]["taskId"])

    def test_quarantine_partial_commit_inventory_is_unknown_not_safe(self):
        self.start_initial()
        self.transport.values[PREFIX + "/issues/8"]["labels"].append({"name": "quarantined-test"})
        child, _ = self.finish_initial()
        for count in (2, 251, None):
            child["commits"] = count
            self.prepare()
            self.assertEqual("handoff_pending", self.chain()["handoff"]["phase"])
            self.assertIn("unavailable", self.chain()["handoff"]["attention"])
        self.assertEqual(1, len(self.task_writes()))

    def test_quiescent_transferred_workers_release_live_slots_without_rewriting_history(self):
        second = pr(10)
        second.update(merged=False, merged_at=None)
        self.transport.values[PREFIX + "/pulls/10"] = second
        self.transport.values[PREFIX + "/issues"].append(dict(second, pull_request={}))
        self.transport.values[PREFIX + "/issues/10/comments"] = [{
            "id": 21, "body": "Repair this source too", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        for _ in range(2):
            api, packet = self.prepare(self.fresh(manual=False, collector=True))
            self.assertIsNotNone(packet)
            pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        before = state.parse(self.transport.comments[0]["body"])
        self.assertEqual(2, state.worker_slots(before))
        api, _ = self.prepare()
        self.assertEqual(2, state.worker_slots(api.ledger), "active converted workers still occupy slots")
        for number in (1, 2):
            task = self.transport.values[TASKS + f"/TASK{number}"]
            task["state"] = task["sessions"][0]["state"] = "completed"
        api, packet = self.prepare()
        self.assertIsNone(packet)
        self.assertEqual(["handoff_needed", "handoff_needed"],
                         [chain["handoff"]["phase"] for chain in api.ledger["chains"]])
        self.assertEqual([chain["operations"] for chain in before["chains"]],
                         [chain["operations"] for chain in api.ledger["chains"]])
        self.assertEqual(0, state.worker_slots(api.ledger))
        self.assertEqual(state.repository_spend(before, self.clock()),
                         state.repository_spend(api.ledger, self.clock()),
                         "Quiescence frees live slots, not unknown historical billing reservations")
        legacy = state.adopt(api.ledger, 11, "pr", "NODE11")
        self.assertIs(legacy, state.select(api.ledger, {11: {"actionable": True}}))

    def test_origin_hands_off_added_after_first_merge_read_prevents_label_removal(self):
        self.start_initial()
        child, _ = self.finish_initial()
        child.update(state="closed", merged=True, merged_at="2026-10-04T00:01:00Z")
        original = self.transport.__call__
        origin_reads = []

        def transport(method, endpoint, body):
            if method == "GET" and endpoint == PREFIX + "/issues/8":
                origin_reads.append(True)
                if len(origin_reads) == 4:
                    self.transport.values[endpoint]["labels"].append({"name": "shepherd-hands-off"})
            return original(method, endpoint, body)

        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        with self.assertRaises(ValueError):
            self.prepare(api)
        self.assertEqual([], [effect for effect in self.transport.writes if effect[0] == "DELETE"])
        self.assertIn({"name": "shepherd-adopted"}, self.transport.values[PREFIX + "/issues/8"]["labels"])

    def test_closed_origin_late_stop_controls_still_prevent_merged_label_delete(self):
        for stop in ("hands-off", "adoption-removed"):
            with self.subTest(stop=stop):
                self.transport = LifecycleTransport()
                self.start_initial()
                child, _ = self.finish_initial()
                self.prepare()
                child.update(state="closed", merged=True, merged_at="2026-10-04T00:01:00Z")
                self.transport.values[PREFIX + "/issues/8"]["state"] = "closed"
                original = self.transport.__call__
                origin_reads = []

                def transport(method, endpoint, body):
                    if method == "GET" and endpoint == PREFIX + "/issues/8":
                        origin_reads.append(True)
                        if len(origin_reads) == 4:
                            labels = self.transport.values[endpoint]["labels"]
                            if stop == "hands-off":
                                labels.append({"name": "shepherd-hands-off"})
                            else:
                                labels[:] = [label for label in labels if label["name"] != "shepherd-adopted"]
                    return original(method, endpoint, body)

                api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
                api.clock = self.clock
                with self.assertRaises(ValueError):
                    self.prepare(api)
                self.assertEqual([], [effect for effect in self.transport.writes if effect[0] == "DELETE"])
                self.assertEqual("closed", self.transport.values[PREFIX + "/issues/8"]["state"])

    def test_known_billed_converted_history_allows_independent_legacy_admission(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        task = self.transport.values[TASKS + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 1500000000}
        self.prepare(self.fresh(manual=False, collector=True))
        self.prepare()
        second = pr(10)
        second.update(merged=False, merged_at=None)
        self.transport.values[PREFIX + "/pulls/10"] = second
        self.transport.values[PREFIX + "/issues"].append(dict(second, pull_request={}))
        self.transport.values[PREFIX + "/issues/10/comments"] = [{
            "id": 21, "body": "Repair independent source", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        self.assertIsNotNone(packet)
        self.assertEqual(10, packet["observation"]["number"])
        self.assertEqual(0, state.worker_slots(api.ledger))

    def test_unknown_frozen_credit_hold_has_honest_independent_legacy_status(self):
        api, packet = self.prepare(self.fresh(manual=False, collector=True))
        pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        task = self.transport.values[TASKS + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        self.prepare()
        api = self.fresh(manual=False)
        api.read_authority()
        legacy = state.adopt(api.ledger, 10, "pr", "NODE10")
        self.transport.values[PREFIX + "/pulls/10"] = dict(pr(10), merged=False, merged_at=None)
        original = state.REPOSITORY_ALLOWANCE
        with patch.object(state, "REPOSITORY_ALLOWANCE", 500):
            observed = api.observe(legacy)
            self.assertIn("Legacy credit evidence unresolved", api.next_action(legacy, observed))
        self.assertEqual(original, state.REPOSITORY_ALLOWANCE)
        self.assertEqual(0, state.worker_slots(api.ledger))

    def test_repeated_manual_confirmation_does_not_reset_stale_progress_or_receipt(self):
        self.prepare()
        local.confirm_handoff(self.fresh(), 7, "a" * 40, self.clock(),
                              app_enabled=True, merge_disabled=True)
        self.prepare(present=True)
        self.clock.advance(seconds=61)
        self.prepare(present=True)
        before = self.chain()
        local.confirm_handoff(self.fresh(), 7, "a" * 40, self.clock(),
                              app_enabled=True, merge_disabled=True)
        self.assertEqual(before["handoff"]["progressAt"], self.chain()["handoff"]["progressAt"])
        self.assertEqual(before["reminder"], self.chain()["reminder"])

    def test_same_head_closed_reopened_watching_pr_starts_new_reminder_episode(self):
        self.prepare()
        local.confirm_handoff(self.fresh(), 7, "a" * 40, self.clock(),
                              app_enabled=True, merge_disabled=True)
        self.prepare(present=True)
        self.clock.advance(seconds=61)
        self.prepare(present=True)
        previous = self.chain()["reminder"]["id"]
        self.pull["state"] = "closed"
        self.prepare(present=True)
        self.clock.advance(seconds=1)
        self.pull["state"] = "open"
        self.prepare(present=True)
        self.assertEqual("watching", self.chain()["handoff"]["phase"])
        self.assertNotEqual(previous, self.chain()["reminder"]["id"])
        self.assertEqual("observed", self.chain()["reminder"]["sendState"])


if __name__ == "__main__":
    unittest.main()
