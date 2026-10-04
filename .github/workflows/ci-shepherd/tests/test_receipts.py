import json
import unittest
from copy import deepcopy

from helpers import FakeClock, FakeGitHub, RECONCILIATION_RUN, observation, reconciliation_decision, reconciliation_evidence, subject
import receipts
import round as contracts


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.github = FakeGitHub()

    def packet(self):
        return contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock, self.github.scope)

    def apply(self, packet=None, action="repair-pr"):
        packet = packet or self.packet()
        decision = reconciliation_decision(packet, action)
        return contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN,
                                              self.github, self.clock, self.github.scope, executor=self.github, dry_run=False,
                                              evidence=reconciliation_evidence(decision))

    def record(self):
        return receipts.read_record(self.github.snapshot, self.github.actor)[1]

    def replace_record(self, record):
        self.github.snapshot["comments"][0]["body"] = receipts.render_record(record)

    def finish_worker(self, state="completed"):
        self.github.snapshot["workers"][-1]["state"] = state
        self.github.snapshot["subjects"][0]["revision"] = chr(ord(self.github.snapshot["subjects"][0]["revision"][0]) + 1) * 40
        self.github.snapshot["jobs"][0]["headSha"] = self.github.snapshot["subjects"][0]["revision"]

    def test_reconstructed_remote_record_preserves_budget_owner_and_trial(self):
        self.apply()
        original = self.record()
        self.finish_worker()
        replacement = FakeGitHub(self.github.snapshot)
        self.github = replacement
        self.clock.advance(hours=1)
        self.apply()
        current = self.record()
        self.assertEqual(current["repairBatches"], 2)
        for key in ("trialId", "trialStartedAt", "expiresAt", "root"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(len(self.github.snapshot["comments"]), 1)
        self.assertEqual(self.github.writes[0][0], "edit")

    def test_replay_has_no_duplicate_effect_or_status_write(self):
        packet = self.packet()
        self.apply(packet)
        count = len(self.github.writes)
        result = self.apply(packet)
        self.assertEqual(result["outcome"], "replay")
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(len(self.github.writes), count)
        self.assertEqual(self.record()["operations"][0]["state"], "confirmed")

    def test_three_cumulative_repairs_survive_new_heads_and_processes(self):
        for _ in range(3):
            self.apply()
            self.finish_worker()
        with self.assertRaisesRegex(ValueError, "repair budget"):
            self.apply()
        self.assertEqual(len(self.github.effects), 3)
        self.assertEqual(self.record()["repairBatches"], 3)

    def reserve_without_dispatch(self):
        packet = self.packet()
        def interrupt(github, count):
            if count == 6:
                raise ValueError("interrupted before consumed intent")
        self.github.before_refresh = interrupt
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.apply(packet)
        self.github.before_refresh = None
        self.assertEqual(self.record()["operations"][0]["state"], "reserved")
        self.assertEqual(self.github.effects, [])
        return self.github

    def second_root(self):
        snapshot = observation()
        snapshot["root"]["number"] = 8
        snapshot["subjects"][0]["subject"]["number"] = 8
        snapshot["jobs"][0]["subject"]["number"] = 8
        snapshot["managedPullRequests"] = [8]
        return FakeGitHub(snapshot)

    def test_second_root_is_rejected_while_authorized_root_has_reservation(self):
        authorized = self.reserve_without_dispatch()
        second = self.second_root()
        with self.assertRaisesRegex(ValueError, "authorized root"):
            packet = contracts.prepare_reconciliation(second, subject(number=8), subject(number=8), RECONCILIATION_RUN, self.clock, authorized.scope)
            decision = reconciliation_decision(packet)
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, second, self.clock, authorized.scope,
                                           executor=second, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(second.writes, [])
        self.assertEqual(second.effects, [])
        self.assertEqual(authorized.snapshot["workers"], [])
        self.assertEqual(self.record()["operations"][0]["state"], "reserved")

    def test_second_root_cannot_restart_expired_authorized_trial(self):
        authorized = self.reserve_without_dispatch()
        original = self.record()
        self.clock.advance(hours=25)
        second = self.second_root()
        with self.assertRaisesRegex(ValueError, "authorized root"):
            packet = contracts.prepare_reconciliation(second, subject(number=8), subject(number=8), RECONCILIATION_RUN, self.clock, authorized.scope)
            decision = reconciliation_decision(packet)
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, second, self.clock, authorized.scope,
                                           executor=second, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(second.writes, [])
        self.assertEqual(second.effects, [])
        self.assertEqual(self.record(), original)
        self.assertEqual(authorized.snapshot["workers"], [])

    def test_apply_independently_rejects_packet_for_another_authorized_root(self):
        authorized = self.reserve_without_dispatch()
        second = self.second_root()
        packet = contracts.prepare_reconciliation(second, subject(number=8), subject(number=8),
                                                  RECONCILIATION_RUN, self.clock, second.scope)
        decision = reconciliation_decision(packet)
        with self.assertRaisesRegex(ValueError, "authorized root"):
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, second, self.clock, authorized.scope,
                                           executor=second, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(second.writes, [])
        self.assertEqual(second.effects, [])
        self.assertIsNone(second.scope.trial)

    def test_reconstructed_scope_preserves_tuple_and_rejects_remote_time_shift(self):
        self.reserve_without_dispatch()
        pinned = self.github.scope.trial
        original = self.record()
        self.clock.advance(hours=2)
        self.github = FakeGitHub(self.github.snapshot)
        self.github.scope = receipts.TrialScope(subject(), pinned)
        self.assertEqual(self.apply()["outcome"], "needs-human")
        self.assertEqual(self.github.scope.trial, pinned)
        self.assertEqual(self.record(), original)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])
        self.github.scope = receipts.TrialScope(subject(), None)
        with self.assertRaisesRegex(ValueError, "immutable trusted trial binding"):
            self.packet()
        self.github.scope = receipts.TrialScope(subject(), pinned)
        shifted = deepcopy(original)
        shifted["trialStartedAt"] = "2026-10-05T00:00:00Z"
        shifted["expiresAt"] = "2026-10-06T00:00:00Z"
        self.replace_record(shifted)
        with self.assertRaisesRegex(ValueError, "immutable trusted trial binding"):
            self.packet()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_pinned_trial_cannot_reinitialize_after_authority_disappears(self):
        self.apply()
        pinned = self.github.scope.trial
        self.github.snapshot["comments"] = []
        self.github.snapshot["workers"] = []
        self.github.snapshot["history"] = {"recordIds": [], "publicationAttempts": [], "associatedOperationIds": []}
        self.github = FakeGitHub(self.github.snapshot)
        self.github.scope = receipts.TrialScope(subject(), pinned)
        self.clock.advance(hours=25)
        with self.assertRaisesRegex(ValueError, "trusted trial record missing"):
            self.packet()
        self.assertEqual(self.github.scope.trial, pinned)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_trial_scope_clock_starts_only_at_authorized_live_initialization(self):
        packet = self.packet()
        wait = reconciliation_decision(packet, "wait")
        contracts.apply_reconciliation(packet, wait, RECONCILIATION_RUN, self.github, self.clock, self.github.scope)
        decision = reconciliation_decision(packet)
        contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope)
        self.assertIsNone(self.github.scope.trial)
        self.assertEqual(self.github.writes, [])
        self.clock.advance(hours=25)
        self.apply()
        self.assertEqual(self.github.scope.trial["trialStartedAt"], "2026-10-05T01:00:00Z")
        self.assertEqual(self.github.scope.trial["expiresAt"], "2026-10-06T01:00:00Z")
        self.assertEqual(self.github.scope.trial, receipts.trial_tuple(self.record()))

    def test_apply_clock_rollback_before_first_publication_authorizes_nothing(self):
        for rollback_read in (2, 3, 4, 5):
            self.github = FakeGitHub()
            self.clock = FakeClock()
            packet = self.packet()
            self.clock.advance(minutes=1)
            lower = self.clock()
            self.clock.advance(minutes=4)
            high_water = self.clock()
            # Prepare 00:00, observe 00:05, then roll back to 00:01 while the
            # packet is still valid. Exercise limit/init/refresh clock reads.
            self.clock.readings = [high_water] * (rollback_read - 1) + [lower]
            with self.subTest(read=rollback_read):
                with self.assertRaisesRegex(ValueError, "clock moved backwards"):
                    self.apply(packet)
                self.assertEqual(self.github.writes, [])
                self.assertEqual(self.github.effects, [])

    def test_reconstructed_pinned_trial_observed_before_start_cannot_mutate(self):
        self.apply()
        self.finish_worker()
        original = self.record()
        pinned = self.github.scope.trial
        self.github = FakeGitHub(self.github.snapshot)
        self.github.scope = receipts.TrialScope(subject(), pinned)
        self.clock.advance(minutes=-1)
        with self.assertRaisesRegex(ValueError, "clock precedes trial start"):
            self.apply()
        self.assertEqual(self.record(), original)
        self.assertEqual(self.github.scope.trial, pinned)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_receipt_limits_reject_clock_before_authoritative_trial_start(self):
        packet = self.packet()
        decision = reconciliation_decision(packet)
        record = receipts.new_record(self.github.snapshot, self.clock(), self.github.scope)
        self.clock.advance(minutes=-1)
        with self.assertRaisesRegex(ValueError, "clock precedes trial start"):
            receipts.ensure_limits(self.github.snapshot, record, receipts.operation_identity(packet, decision), self.clock())
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_scope_is_required_and_cannot_be_an_agent_json_claim(self):
        with self.assertRaises(TypeError):
            contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock)
        packet = self.packet()
        decision = reconciliation_decision(packet)
        with self.assertRaises(TypeError):
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock)
        with self.assertRaisesRegex(ValueError, "explicit trusted singleton trial scope"):
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock,
                                           {"root": subject(), "trial": None}, executor=self.github,
                                           dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_existing_pr_repair_at_three_managed_prs_does_not_allocate_slot(self):
        self.github.snapshot["managedPullRequests"] = [7, 8, 9]
        result = self.apply()
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(self.record()["repairBatches"], 1)

    def test_issue_assignment_and_new_adoption_cannot_allocate_fourth_slot(self):
        for action in ("assign-issue", "adopt-pr"):
            self.github = FakeGitHub(observation("issue"))
            self.github.snapshot["managedPullRequests"] = [9, 10, 11]
            if action == "adopt-pr":
                child = deepcopy(observation()["subjects"][0])
                child.update(subject=subject(number=8), managed=False, labels=[], nodeId="CHILD8")
                self.github.snapshot["subjects"].append(child)
            packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
            decision = reconciliation_decision(packet, action)
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "managed PR capacity"):
                contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                               executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
            self.assertEqual(self.github.effects, [])
            self.assertEqual(self.github.writes, [])

    def test_adopting_already_counted_pr_does_not_allocate_fourth_slot(self):
        self.github = FakeGitHub(observation("issue"))
        self.github.snapshot["managedPullRequests"] = [8, 9, 10]
        child = deepcopy(observation()["subjects"][0])
        child.update(subject=subject(number=8), managed=False, labels=[], nodeId="CHILD8")
        self.github.snapshot["subjects"].append(child)
        packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
        decision = reconciliation_decision(packet, "adopt-pr")
        result = contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                                executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(self.github.snapshot["managedPullRequests"], [8, 9, 10])

    def test_two_reruns_per_stable_logical_job_head_not_run_id(self):
        for run_id in (10, 11):
            self.github.snapshot["jobs"][0]["runId"] = run_id
            packet = self.packet()
            decision = reconciliation_decision(packet, "rerun-transient",
                                               {"runId": run_id, "jobId": 20, "logicalJob": "tests / linux"})
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                           executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.github.snapshot["jobs"][0]["runId"] = 12
        packet = self.packet()
        decision = reconciliation_decision(packet, "rerun-transient",
                                           {"runId": 12, "jobId": 20, "logicalJob": "tests / linux"})
        with self.assertRaisesRegex(ValueError, "rerun budget"):
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                           executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(self.record()["reruns"][0]["count"], 2)
        self.assertEqual(len(self.github.effects), 2)

    def test_new_head_rerun_key_does_not_reset_trial_or_repair_budget(self):
        self.apply(action="rerun-transient")
        original = self.record()
        self.github.snapshot["subjects"][0]["revision"] = "c" * 40
        self.github.snapshot["jobs"][0]["headSha"] = "c" * 40
        self.apply(action="rerun-transient")
        current = self.record()
        self.assertEqual([counter["count"] for counter in current["reruns"]], [1, 1])
        self.assertEqual(current["trialId"], original["trialId"])
        self.assertEqual(current["expiresAt"], original["expiresAt"])

    def test_root_issue_takeover_pauses_managed_pr_and_no_progress_write(self):
        self.github = FakeGitHub(observation("issue"))
        child = deepcopy(observation()["subjects"][0])
        child.update(subject=subject(number=8), nodeId="CHILD8")
        self.github.snapshot["subjects"].append(child)
        packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject(number=8), RECONCILIATION_RUN, self.clock, self.github.scope)
        self.github.snapshot["subjects"][0]["labels"].append("shepherd-hands-off")
        decision = reconciliation_decision(packet)
        with self.assertRaisesRegex(ValueError, "hands-off"):
            contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                           executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.effects, [])

    def test_prior_managed_pr_cannot_be_hidden_as_unmanaged_or_omitted(self):
        self.github = FakeGitHub(observation("issue"))
        child = deepcopy(observation()["subjects"][0])
        child.update(subject=subject(number=8), nodeId="CHILD8")
        self.github.snapshot["subjects"].append(child)
        packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject(number=8), RECONCILIATION_RUN, self.clock, self.github.scope)
        decision = reconciliation_decision(packet)
        contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                       executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.github.snapshot["subjects"][1].update(managed=False, labels=[])
        with self.assertRaisesRegex(ValueError, "adoption"):
            contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
        self.github.snapshot["subjects"].pop()
        with self.assertRaisesRegex(ValueError, "subject identity"):
            contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
        self.assertEqual(len(self.github.effects), 1)

    def test_invalid_effect_response_retains_consumed_intent_without_claiming_ids(self):
        packet = self.packet()
        def invalid(operation):
            self.github.effects.append(operation)
            return {"id": "invented", "kind": "worker", "body": "extra"}
        self.github.execute = invalid
        with self.assertRaises(ValueError):
            self.apply(packet)
        record = self.record()
        self.assertEqual(record["operations"][0]["state"], "consumed")
        self.assertIsNone(record["operations"][0]["result"])
        self.assertEqual(self.apply(packet)["outcome"], "needs-human")
        self.assertEqual(len(self.github.effects), 1)

    def test_lost_status_edit_without_publication_does_not_repeat_effect(self):
        packet = self.packet()
        original_publish = self.github.publish_status
        def publish(root, body, comment_id, guard):
            if any(operation["state"] == "confirmed" for operation in receipts.parse_body(body)["operations"]):
                from github import LostResponse
                guard()
                self.github.writes.append(("edit", comment_id, body))
                raise LostResponse("confirmation edit did not return")
            return original_publish(root, body, comment_id, guard)
        self.github.publish_status = publish
        with self.assertRaisesRegex(ValueError, "needs-human"):
            self.apply(packet)
        self.assertEqual(self.record()["operations"][0]["state"], "consumed")
        self.github.publish_status = original_publish
        recovered = self.apply(packet)
        self.assertEqual(recovered["outcome"], "confirmed")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["effects"], [])
        self.assertEqual(self.record()["repairBatches"], 1)
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(len(self.github.snapshot["comments"]), 1)

    def test_packet_expiry_and_absolute_trial_expiry(self):
        packet = self.packet()
        self.clock.advance(minutes=11)
        with self.assertRaisesRegex(ValueError, "packet expired"):
            self.apply(packet)
        self.assertEqual(self.github.writes, [])
        self.apply()
        self.finish_worker()
        original = self.record()
        self.clock.advance(hours=24)
        with self.assertRaisesRegex(ValueError, "trial expired"):
            self.apply()
        self.assertEqual(self.record(), original)

    def test_unknown_nonterminal_and_multiple_workers_hold_capacity(self):
        for states in (["unknown"], ["queued"], ["in_progress"], ["new-api-state"], ["completed", "queued"], ["queued", "queued"]):
            self.github = FakeGitHub()
            self.github.snapshot["workers"] = [{
                "id": f"task-{index}", "state": state, "root": subject(number=99), "operationId": None,
            } for index, state in enumerate(states)]
            with self.subTest(states=states), self.assertRaisesRegex(ValueError, "capacity"):
                self.apply()
            self.assertEqual(self.github.effects, [])

    def test_default_unarchived_task_inventory_does_not_establish_capacity(self):
        self.github.snapshot["complete"]["workersArchived"] = False
        # The API's {"tasks": [...]} response need not include aggregate counts.
        # A completed unarchived lane cannot authorize omission of archived tasks.
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.apply()
        self.assertEqual(self.github.effects, [])
        self.assertEqual(self.github.writes, [])

    def test_aggregate_worker_completeness_without_archive_lanes_is_rejected(self):
        del self.github.snapshot["complete"]["workersArchived"]
        del self.github.snapshot["complete"]["workersUnarchived"]
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.github.effects, [])
        self.assertEqual(self.github.writes, [])

    def test_archived_nonterminal_and_unrecognized_task_states_hold_capacity(self):
        for state in ("queued", "in_progress", "idle", "waiting_for_user", "timed_out", "future-state"):
            self.github = FakeGitHub()
            # A host normalizer must include tasks from both archive lanes; an
            # archived task is not made terminal merely by being archived.
            self.github.snapshot["workers"] = [{
                "id": "archived-task", "state": state, "root": subject(number=99), "operationId": None,
            }]
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "capacity"):
                self.apply()
            self.assertEqual(self.github.effects, [])
            self.assertEqual(self.github.writes, [])

    def test_archive_visibility_loss_preserves_owned_reservation_and_counters(self):
        self.apply()
        previous = self.record()
        self.finish_worker()
        self.github = FakeGitHub(self.github.snapshot)
        self.github.snapshot["workers"] = []
        self.github.snapshot["complete"]["workersArchived"] = False
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.apply()
        self.assertEqual(self.record(), previous)
        self.assertEqual(previous["operations"][0]["result"], {"id": "task-1", "kind": "worker"})
        self.assertEqual(previous["repairBatches"], 1)
        self.assertEqual(self.github.effects, [])
        self.assertEqual(self.github.writes, [])

    def test_cancelled_and_terminal_are_not_unknown_workers(self):
        for state in ("completed", "cancelled", "failed"):
            self.github = FakeGitHub()
            self.github.snapshot["workers"] = [{"id": "old", "state": state, "root": subject(number=99), "operationId": None}]
            self.apply()
            self.assertEqual(len(self.github.effects), 1)

    def test_hard_takeover_before_each_write_stops_progress_publication(self):
        # Initialization, prepared, reserved, consumed, effect, confirmation all refresh.
        for veto_refresh in range(3, 9):
            self.github = FakeGitHub()
            packet = self.packet()
            def veto(github, count):
                if count == veto_refresh:
                    github.snapshot["subjects"][0]["labels"].append("shepherd-hands-off")
                    github.at_veto = (len(github.writes), len(github.effects))
            self.github.before_refresh = veto
            try:
                self.apply(packet)
            except ValueError:
                pass
            self.assertTrue(hasattr(self.github, "at_veto"))
            self.assertEqual((len(self.github.writes), len(self.github.effects)), self.github.at_veto)

    def test_status_create_response_loss_reconciles_without_duplicate_post(self):
        self.github.loss = "create-after"
        self.apply()
        self.assertEqual([write[0] for write in self.github.writes].count("create"), 1)
        self.assertEqual(len(self.github.snapshot["comments"]), 1)
        self.assertEqual(self.record()["repairBatches"], 1)

    def test_unestablished_status_create_needs_human_never_reposts(self):
        self.github.loss = "create-before"
        with self.assertRaisesRegex(ValueError, "needs-human"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "prior"):
            self.apply()
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(self.github.effects, [])

    def test_lost_edit_response_and_replay_keep_reservation(self):
        self.apply()
        self.finish_worker()
        self.github.loss = "edit-after"
        self.apply()
        self.assertEqual(self.record()["repairBatches"], 2)
        self.assertEqual(len(self.github.effects), 2)

    def test_lost_post_without_remote_identity_never_retries(self):
        packet = self.packet()
        self.github.effect_loss = True
        result = self.apply(packet)
        self.assertEqual(result["outcome"], "needs-human")
        self.assertEqual(self.record()["operations"][0]["state"], "uncertain")
        self.apply(packet)
        self.github.snapshot["subjects"][0]["revision"] = "c" * 40
        self.github.snapshot["jobs"][0]["headSha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "uncertain|capacity"):
            self.apply()
        self.assertEqual(len(self.github.effects), 1)

    def test_lost_post_with_exact_remote_identity_records_actual_id(self):
        self.github.effect_loss = self.github.effect_visible = True
        result = self.apply()
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(self.record()["operations"][0]["result"], {"id": "task-1", "kind": "worker"})
        self.assertEqual(len(self.github.effects), 1)

    def test_process_loss_recovery_on_new_head_confirms_prior_id_without_dispatch(self):
        self.apply()
        record = self.record()
        record["operations"][0].update(state="consumed", result=None)
        self.replace_record(record)
        self.github.snapshot["subjects"][0]["revision"] = "c" * 40
        self.github.snapshot["jobs"][0]["headSha"] = "c" * 40
        # A fresh process/packet sees remote operation correlation, not local
        # cache files or a guessed count increase in the task inventory.
        reconstructed = FakeGitHub(self.github.snapshot)
        self.github = reconstructed
        result = self.apply()
        self.assertTrue(result["recovered"])
        self.assertEqual(result["effects"], [])
        self.assertEqual(self.record()["operations"][0]["result"], {"id": "task-1", "kind": "worker"})
        self.assertEqual(self.record()["repairBatches"], 1)
        self.assertEqual(self.github.effects, [])
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.apply()

    def test_uncertain_prepared_reserved_and_consumed_block_new_operations(self):
        for state in ("prepared", "reserved", "consumed", "uncertain"):
            self.github = FakeGitHub()
            self.apply()
            self.finish_worker()
            record = self.record()
            record["operations"][0].update(state=state, result=None)
            if state == "prepared":
                record["repairBatches"] = 0
            self.replace_record(record)
            self.github.snapshot["workers"] = []
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "uncertain|capacity"):
                self.apply()
            self.assertEqual(len(self.github.effects), 1)

    def test_spoof_marker_ignored_but_malformed_authority_and_duplicates_fail(self):
        self.github.snapshot["comments"].append({
            "id": 77, "user": {"id": 999, "login": "stranger"},
            "body": "[automated] <!-- ci-shepherd:root:v1 -->\n{}",
        })
        self.apply()
        valid = deepcopy(self.github.snapshot["comments"][-1])
        self.github.snapshot["comments"].append({**valid, "id": 502})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.packet()
        self.github.snapshot["comments"].pop()
        for body in ("broken <!-- ci-shepherd:root:v1 -->", valid["body"].replace('"schemaVersion":1', '"schemaVersion":2')):
            self.github.snapshot["comments"][-1]["body"] = body
            with self.assertRaises(ValueError):
                self.packet()

    def test_deleted_prior_record_and_bad_counters_do_not_bootstrap(self):
        self.apply()
        valid = self.record()
        for field, value in (("root", subject(number=99)), ("repairBatches", 0), ("expiresAt", "2030-01-01T00:00:00Z")):
            bad = deepcopy(valid)
            bad[field] = value
            # Malformed remote state is not allowed through the renderer either.
            self.github.snapshot["comments"][0]["body"] = "[automated] CI Shepherd status\n" + receipts.MARKER + "\n" + json.dumps(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.packet()
        self.github.snapshot["comments"] = []
        with self.assertRaisesRegex(ValueError, "prior"):
            self.apply()
        self.assertEqual(len(self.github.effects), 1)

    def test_refresh_unavailable_at_every_write_or_effect_authorizes_nothing(self):
        for failure_refresh in range(3, 9):
            self.github = FakeGitHub()
            packet = self.packet()
            def fail(github, count):
                if count == failure_refresh:
                    github.at_failure = (len(github.writes), len(github.effects))
                    github.snapshot["complete"]["workers"] = False
            self.github.before_refresh = fail
            with self.subTest(refresh=failure_refresh), self.assertRaisesRegex(ValueError, "incomplete"):
                self.apply(packet)
            self.assertEqual((len(self.github.writes), len(self.github.effects)), self.github.at_failure)

    def test_feedback_or_adoption_changes_at_send_boundary_prevent_effect(self):
        for field, value, message in (("labels", [], "adoption"), ("feedback", [], "basis")):
            self.github = FakeGitHub()
            packet = self.packet()
            def change(github, count):
                if count == 7:
                    github.snapshot["subjects"][0][field] = value
            self.github.before_refresh = change
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                self.apply(packet)
            self.assertEqual(self.github.effects, [])
            self.assertEqual(self.record()["operations"][0]["state"], "consumed")
            self.assertEqual(self.record()["repairBatches"], 1)

    def test_confirmed_worker_disappearing_is_unknown_not_free_capacity(self):
        self.apply()
        self.github.snapshot["workers"] = []
        self.github.snapshot["subjects"][0]["revision"] = "c" * 40
        self.github.snapshot["jobs"][0]["headSha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "unknown worker holds capacity"):
            self.apply()
        self.assertEqual(len(self.github.effects), 1)

    def test_associated_terminal_worker_cannot_initialize_a_new_chain(self):
        self.github.snapshot["workers"] = [{"id": "prior", "state": "cancelled", "root": subject(), "operationId": None}]
        with self.assertRaisesRegex(ValueError, "prior chain"):
            self.apply()
        self.assertEqual(self.github.writes, [])

    def test_known_rejection_is_failed_not_uncertain_and_still_consumes_budget(self):
        self.github.reject_effect = True
        result = self.apply()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.record()["operations"][0]["state"], "failed")
        self.assertIsNone(self.record()["operations"][0]["result"])
        self.assertEqual(self.record()["repairBatches"], 1)
        count = len(self.github.writes)
        self.assertEqual(self.apply()["outcome"], "replay")
        self.assertEqual(len(self.github.writes), count)
        self.assertEqual(len(self.github.effects), 1)

    def test_issue_assignment_intent_is_once_per_chain_even_after_revision_change(self):
        self.github = FakeGitHub(observation("issue"))
        def assign():
            packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
            decision = reconciliation_decision(packet, "assign-issue")
            return contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                                  executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        assign()
        self.github.snapshot["workers"][0]["state"] = "completed"
        self.github.snapshot["subjects"][0]["revision"] = "issue-revision-2"
        count = len(self.github.writes)
        with self.assertRaisesRegex(ValueError, "assignment intent"):
            assign()
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(len(self.github.writes), count)

    def test_root_record_bound_and_unknown_schema_require_human_recovery(self):
        self.apply()
        record = self.record()
        oversized = deepcopy(record)
        oversized["operations"][0]["identity"]["feedback"] = [
            {"id": f"feedback-{index}", "revision": "x" * 250, "state": "open"} for index in range(80)
        ]
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            receipts.render_record(oversized)
        self.github.snapshot["comments"][0]["body"] = self.github.snapshot["comments"][0]["body"].replace(
            "<!-- ci-shepherd:root:v1 -->", "<!-- ci-shepherd:root:v2 -->",
        )
        with self.assertRaisesRegex(ValueError, "authoritative"):
            self.packet()

    def test_expiry_during_send_never_dispatches_or_clears_reservation(self):
        self.apply()
        self.finish_worker()
        self.clock.advance(hours=23, minutes=59)
        packet = self.packet()
        start = self.github.refreshes
        def expire(github, count):
            if count == start + 5:
                self.clock.advance(minutes=1)
        self.github.before_refresh = expire
        with self.assertRaisesRegex(ValueError, "trial expired"):
            self.apply(packet)
        self.assertEqual(len(self.github.effects), 1)
        self.assertEqual(self.record()["operations"][-1]["state"], "consumed")
        self.assertEqual(self.record()["repairBatches"], 2)

    def test_nonterminal_capacity_survives_takeover_and_expiry(self):
        self.github.effect_loss = True
        self.apply()
        original = self.record()
        self.github.snapshot["subjects"][0]["labels"].append("shepherd-hands-off")
        self.clock.advance(hours=24)
        with self.assertRaisesRegex(ValueError, "hands-off"):
            self.packet()
        self.assertEqual(self.record(), original)
        self.assertEqual(original["operations"][0]["state"], "uncertain")
        self.assertEqual(original["repairBatches"], 1)


if __name__ == "__main__":
    unittest.main()
