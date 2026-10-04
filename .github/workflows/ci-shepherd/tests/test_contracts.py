import json
import unittest
from copy import deepcopy

from helpers import FakeClock, FakeGitHub, RECONCILIATION_RUN, observation, reconciliation_decision, reconciliation_evidence, subject
import round as contracts
import receipts


class ReconciliationContractTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.github = FakeGitHub()

    def prepare(self, kind="pr"):
        self.github = FakeGitHub(observation(kind))
        return contracts.prepare_reconciliation(self.github, subject(kind), subject(kind), RECONCILIATION_RUN, self.clock, self.github.scope)

    def test_adopted_issue_and_pr_allow_wait(self):
        for kind in ("issue", "pr"):
            packet = self.prepare(kind)
            decision = reconciliation_decision(packet, "wait")
            contracts.validate_reconciliation_decision(packet, decision, RECONCILIATION_RUN)
            result = contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope)
            self.assertEqual(result["outcome"], "wait")
            self.assertEqual(self.github.writes, [])

    def test_unadopted_and_hands_off_are_paused(self):
        for labels, expected in (([], "adoption"), (["shepherd-adopted", "shepherd-hands-off"], "hands-off")):
            for kind in ("issue", "pr"):
                self.github = FakeGitHub(observation(kind))
                self.github.snapshot["subjects"][0]["labels"] = labels
                with self.assertRaisesRegex(ValueError, expected):
                    contracts.prepare_reconciliation(self.github, subject(kind), subject(kind), RECONCILIATION_RUN, self.clock, self.github.scope)
                self.assertEqual(self.github.writes, [])

    def test_closed_vocabulary_and_typed_arguments(self):
        packet = self.prepare()
        for action in ("wait", "repair-pr", "rerun-transient", "checkpoint"):
            contracts.validate_reconciliation_decision(packet, reconciliation_decision(packet, action), RECONCILIATION_RUN)
        packet = self.prepare("issue")
        contracts.validate_reconciliation_decision(packet, reconciliation_decision(packet, "assign-issue"), RECONCILIATION_RUN)
        child = deepcopy(observation()["subjects"][0])
        child.update(subject=subject("pr", 8), managed=False, labels=[], nodeId="NODE8")
        self.github.snapshot["subjects"].append(child)
        packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
        contracts.validate_reconciliation_decision(packet, reconciliation_decision(packet, "adopt-pr"), RECONCILIATION_RUN)

    def test_rejects_mutation_bodies_bad_arguments_and_unbound_identity(self):
        packet = self.prepare()
        good = reconciliation_decision(packet)
        bad_values = [
            {**good, "body": "agent-authored instructions"},
            {**good, "action": "merge"},
            {**good, "arguments": {"feedbackIds": ["invented"]}},
            {**good, "arguments": {"feedbackIds": ["review-1"], "command": "anything"}},
            {**good, "subject": subject(number=9)},
            {**good, "evidenceIds": ["invented"]},
            {**good, "evidenceIds": ["subject-7", "subject-7"]},
            {**good, "basis": {**good["basis"], "packetId": "invented"}},
            {**good, "basis": {**good["basis"], "run": {**RECONCILIATION_RUN, "runId": "42"}}},
            {**good, "schemaVersion": True},
            {**good, "action": []},
            {**good, "evidenceIds": [{}]},
            {**good, "basis": []},
            {**reconciliation_decision(packet, "wait"), "arguments": {}},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                contracts.validate_reconciliation_decision(packet, bad, RECONCILIATION_RUN)

    def test_boolean_subject_number_cannot_alias_number_one(self):
        self.github = FakeGitHub()
        self.github.snapshot["root"]["number"] = 1
        self.github.snapshot["subjects"][0]["subject"]["number"] = 1
        self.github.snapshot["jobs"][0]["subject"]["number"] = 1
        self.github.scope = receipts.TrialScope(subject(number=1), None)
        packet = contracts.prepare_reconciliation(self.github, subject(number=1), subject(number=1), RECONCILIATION_RUN, self.clock, self.github.scope)
        decision = reconciliation_decision(packet)
        decision["subject"]["number"] = True
        with self.assertRaisesRegex(ValueError, "subject number"):
            contracts.validate_reconciliation_decision(packet, decision, RECONCILIATION_RUN)

    def test_typed_issue_adoption_and_checkpoint_use_the_same_guarded_core(self):
        for action in ("assign-issue", "adopt-pr", "checkpoint"):
            packet = self.prepare("issue")
            if action == "adopt-pr":
                child = deepcopy(observation()["subjects"][0])
                child.update(subject=subject(number=8), managed=False, labels=[], nodeId="CHILD8")
                self.github.snapshot["subjects"].append(child)
                packet = contracts.prepare_reconciliation(self.github, subject("issue"), subject("issue"), RECONCILIATION_RUN, self.clock, self.github.scope)
            decision = reconciliation_decision(packet, action)
            result = contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                                    executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
            self.assertEqual(result["outcome"], "confirmed")
            self.assertEqual(len(self.github.effects), 1)
            self.assertEqual(self.github.effects[0]["identity"]["arguments"], decision["arguments"])

    def test_duplicate_json_and_duplicate_decision_fields_fail(self):
        packet = self.prepare()
        text = json.dumps(reconciliation_decision(packet))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            contracts.loads(text[:-1] + ', "action": "wait"}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            contracts.loads(text.replace('"packetId":', '"packetId": "x", "packetId":', 1))

    def test_every_incomplete_inventory_fails_closed(self):
        for inventory in observation()["complete"]:
            self.github = FakeGitHub()
            self.github.snapshot["complete"][inventory] = False
            with self.subTest(inventory=inventory), self.assertRaisesRegex(ValueError, "incomplete"):
                contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock, self.github.scope)

    def test_oversized_host_packet_is_rejected_before_reasoning(self):
        self.github.snapshot["comments"].append({
            "id": 77, "user": {"id": 999, "login": "stranger"}, "body": "x" * (256 * 1024),
        })
        with self.assertRaisesRegex(ValueError, "packet exceeds size"):
            contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock, self.github.scope)
        self.assertEqual(self.github.writes, [])

    def test_head_feedback_identity_and_host_run_refresh(self):
        for field, value in (("revision", "c" * 40), ("nodeId", "OTHER"), ("feedback", []), ("state", "closed")):
            packet = self.prepare()
            self.github.snapshot["subjects"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contracts.apply_reconciliation(packet, reconciliation_decision(packet), RECONCILIATION_RUN,
                                               self.github, self.clock, self.github.scope, executor=self.github, dry_run=False)
            self.assertEqual(self.github.writes, [])
            self.assertEqual(self.github.effects, [])

    def test_linked_pr_hands_off_vetoes_entire_issue_chain(self):
        packet = self.prepare("issue")
        child = deepcopy(observation()["subjects"][0])
        child["nodeId"] = "CHILD-NODE"
        child["labels"].append("shepherd-hands-off")
        self.github.snapshot["subjects"].append(child)
        with self.assertRaisesRegex(ValueError, "hands-off"):
            contracts.apply_reconciliation(packet, reconciliation_decision(packet, "assign-issue"),
                                           RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                           executor=self.github, dry_run=False)
        self.assertEqual(self.github.writes, [])

    def test_no_handlers_and_local_dry_run_never_start_trial(self):
        packet = self.prepare()
        result = contracts.apply_reconciliation(packet, reconciliation_decision(packet), RECONCILIATION_RUN,
                                               self.github, self.clock, self.github.scope)
        self.assertEqual(result["outcome"], "dry-run")
        with self.assertRaisesRegex(ValueError, "handler"):
            contracts.apply_reconciliation(packet, reconciliation_decision(packet), RECONCILIATION_RUN,
                                           self.github, self.clock, self.github.scope, dry_run=False)
        self.github.write_enabled = False
        with self.assertRaisesRegex(ValueError, "writer"):
            contracts.apply_reconciliation(packet, reconciliation_decision(packet), RECONCILIATION_RUN,
                                           self.github, self.clock, self.github.scope, executor=self.github, dry_run=False)
        self.assertEqual(self.github.writes, [])

    def test_live_core_requires_host_evidence_not_agent_permission_claims(self):
        packet = self.prepare()
        decision = reconciliation_decision(packet)
        for evidence in (None, {"tools": ["safeoutputs-submit_decision"]},
                         reconciliation_evidence(reconciliation_decision(packet, "wait"))):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                               executor=self.github, dry_run=False, evidence=evidence)
            self.assertEqual(self.github.writes, [])
            self.assertEqual(self.github.effects, [])

    def test_reordered_inventory_and_feedback_arguments_have_stable_identity(self):
        packet = self.prepare()
        self.github.snapshot["subjects"][0]["feedback"].append({
            "id": "review-2", "revision": "v2", "state": "open",
        })
        packet = contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock, self.github.scope)
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["review-2", "review-1"]})
        self.github.snapshot["subjects"][0]["feedback"].reverse()
        contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                       executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        packet = contracts.prepare_reconciliation(self.github, subject(), subject(), RECONCILIATION_RUN, self.clock, self.github.scope)
        decision = reconciliation_decision(packet, arguments={"feedbackIds": ["review-1", "review-2"]})
        result = contracts.apply_reconciliation(packet, decision, RECONCILIATION_RUN, self.github, self.clock, self.github.scope,
                                                executor=self.github, dry_run=False, evidence=reconciliation_evidence(decision))
        self.assertEqual(result["outcome"], "replay")
        self.assertEqual(len(self.github.effects), 1)


if __name__ == "__main__":
    unittest.main()
