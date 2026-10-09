from copy import deepcopy
import unittest

import work_items as items
import pilot_state


def control():
    return {
        "schema_version": 1, "id": "example-item", "revision": 1,
        "issue": {"repository": "radical/aspire", "number": 900, "node_id": "ISSUE900"},
        "reported_kind": "workflow_failure", "requested_route": "workflow_failure",
        "action": "run",
        "occurrence": {"repository": "radical/aspire", "run_id": 100, "run_attempt": 1,
                       "head_sha": "a" * 40, "job_id": 200, "artifact_id": None},
        "scope": "Fix only the reproduced cause of the pinned failure.",
        "authority": {"local_edits": True, "issue_comment": False, "draft_pr": True,
                      "destination": {"target_repository": "radical/aspire", "base": "ci-shepherd",
                                      "head_repository": "radical/aspire", "branch": "example-fix"}},
        "pr_review": None,
    }


def result(assignment):
    return {
        "schema_version": 1, "item_id": "example-item", "assignment_id": assignment["id"],
        "evaluated_revision": assignment["revision"], "worker_id": "session-1",
        "actual_classification": "product_bug_candidate", "transience": "not_established",
        "outcome": "fixed", "same_failure": True,
        "evidence": ["Reproduced a bad boundary check."], "changed_files": ["src/example.py"],
        "tests": [{"command": "python3 -m unittest test_example", "result": "passed"}],
        "pr_proposal": {"title": "Fix boundary check", "body": "A reproduced boundary check failed. Refs #900."},
        "summary": "Fixed the reproduced boundary check.",
    }


def validation(assignment):
    return {
        "schema_version": 1, "item_id": "example-item", "assignment_id": assignment["id"],
        "evaluated_revision": assignment["revision"], "resulting_head": "b" * 40,
        "product_bug": True, "same_failure": True, "in_scope": True,
        "tests": [{"command": "python3 -m unittest test_example", "result": "passed"}],
        "evidence": ["The assertion failed without the fix and passes with it."],
    }


class WorkItemTests(unittest.TestCase):
    def setUp(self):
        self.control = control()
        self.record = items.new_record(self.control)

    def claim(self):
        assignment = items.claim(self.record, self.control)
        if assignment is not None:
            assignment["delivery"] = "delivered"
        return assignment

    def finish(self):
        assignment = self.claim()
        items.checkpoint(self.record, result(assignment), "session-1", validation(assignment))
        return assignment

    def test_closed_contract_rejects_unknown_types_and_fields(self):
        for path, value in (("revision", True), ("id", "../escape"), ("reported_kind", "diagnosed_bug"),
                            ("requested_route", "shell"), ("extra", True)):
            with self.subTest(path=path):
                changed = deepcopy(self.control)
                changed[path] = value
                with self.assertRaises(ValueError):
                    items.validate_control(changed)
        for key in ("run_id", "run_attempt", "job_id"):
            changed = deepcopy(self.control)
            changed["occurrence"][key] = True
            with self.assertRaises(ValueError):
                items.validate_control(changed)

    def test_workflow_failure_requires_pinned_occurrence_and_issue(self):
        for key in ("occurrence", "issue"):
            changed = deepcopy(self.control)
            changed[key] = None
            with self.assertRaises(ValueError):
                items.validate_control(changed)

    def test_draft_pr_permission_requires_explicit_destination(self):
        changed = deepcopy(self.control)
        changed["authority"]["destination"] = None
        with self.assertRaisesRegex(ValueError, "destination"):
            items.validate_control(changed)

    def test_result_cannot_turn_readonly_assignment_into_an_authorized_fix(self):
        self.control["authority"]["local_edits"] = False
        self.record = items.new_record(self.control)
        self.finish()
        self.assertIsNone(items.preview(self.record, self.control))

    def test_failed_host_test_never_produces_preview_or_request(self):
        assignment = self.claim()
        checked = validation(assignment)
        checked["tests"][0]["result"] = "failed"
        items.checkpoint(self.record, result(assignment), "session-1", checked)
        self.assertIsNone(items.preview(self.record, self.control))
        self.assertIsNone(items.pr_request(self.record, self.control))

    def test_saved_boundary_contract_rejects_unknown_and_inconsistent_receipts(self):
        self.finish()
        preview = items.preview(self.record, self.control)
        for key, value in (("unexpected", 1), ("assignment_id", "foreign"),
                           ("destination", None), ("title", 42)):
            with self.subTest(key=key):
                record = deepcopy(self.record)
                record["preview"][key] = value
                with self.assertRaises(ValueError):
                    items.validate_record(record)
        self.assertEqual(preview, self.record["preview"])

    def test_checkpoint_rejects_definitely_undelivered_assignment(self):
        assignment = items.claim(self.record, self.control)
        with self.assertRaisesRegex(ValueError, "delivery"):
            items.checkpoint(self.record, result(assignment), "session-1", None)

    def test_worker_packet_carries_complete_result_contract_and_effective_authority(self):
        assignment = self.claim()
        packet = items.worker_packet(self.record, assignment, {"comment_id": 500})
        self.assertEqual(set(result(assignment)), set(packet["result_contract"]))
        self.assertEqual(assignment["id"], packet["result_contract"]["assignment_id"])
        self.assertEqual(assignment["revision"], packet["result_contract"]["evaluated_revision"])
        self.assertFalse(packet["effective_policy"]["github_writes"])

    def test_pr_proposal_cannot_supply_tracker_closing_references(self):
        for closing in ("Fixes #900", "Closes radical/aspire#900", "Resolves: #900",
                        "Fixes https://github.com/radical/aspire/issues/900"):
            with self.subTest(closing=closing):
                record = items.new_record(self.control)
                assignment = items.claim(record, self.control)
                assignment["delivery"] = "delivered"
                claim = result(assignment)
                claim["pr_proposal"]["body"] = closing
                items.checkpoint(record, claim, "session-1", validation(assignment))
                self.assertIsNone(items.preview(record, self.control))

    def test_reconcile_never_accepts_revision_reuse_or_identity_change(self):
        before = deepcopy(self.record)
        for key in ("scope", "requested_route"):
            changed = deepcopy(self.control)
            changed[key] = "product_bug"
            with self.assertRaisesRegex(ValueError, "revision"):
                items.reconcile(self.record, changed)
        changed = deepcopy(self.control)
        changed["revision"] = 2
        changed["occurrence"]["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "identity"):
            items.reconcile(self.record, changed)
        self.assertEqual(before, self.record)

    def test_reentry_and_route_edit_never_duplicate_active_assignment(self):
        assignment = self.claim()
        self.assertIsNone(self.claim())
        changed = deepcopy(self.control)
        changed.update(revision=2, requested_route="product_bug")
        items.reconcile(self.record, changed)
        self.assertIsNone(items.claim(self.record, changed))
        items.checkpoint(self.record, result(assignment), "session-1", validation(assignment))
        replacement = items.claim(self.record, changed)
        self.assertEqual((2, "product_bug"), (replacement["revision"], replacement["route"]))
        self.assertNotEqual(assignment["id"], replacement["id"])
        self.assertIsNone(items.claim(self.record, changed))
        self.assertEqual(1, self.record["assignments"][0]["result"]["evaluated_revision"])
        first = items.worker_packet(self.record, assignment, {})
        second = items.worker_packet(self.record, replacement, {})
        self.assertEqual("workflow-failure/v1", first["specialist"])
        self.assertEqual("product-bug/v1", second["specialist"])
        self.assertNotEqual(first["prompt"], second["prompt"])
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertFalse(second["effective_policy"]["github_writes"])

    def test_known_undelivered_reservation_can_be_replaced_after_control_edit(self):
        original = items.claim(self.record, self.control)
        changed = deepcopy(self.control)
        changed.update(revision=2, requested_route="flaky_test")
        items.reconcile(self.record, changed)
        self.assertEqual("abandoned", original["delivery"])
        replacement = items.claim(self.record, changed)
        self.assertEqual("flaky_test", replacement["route"])
        self.assertEqual(2, len(self.record["assignments"]))
        self.assertIsNone(items.claim(self.record, changed))

    def test_uncertain_delivery_keeps_assignment_on_route_edit(self):
        original = items.claim(self.record, self.control)
        original["delivery"] = "delivering"
        changed = deepcopy(self.control)
        changed.update(revision=2, requested_route="flaky_test")
        items.reconcile(self.record, changed)
        self.assertIsNone(items.claim(self.record, changed))
        self.assertEqual("delivering", original["delivery"])

    def test_paused_revision_accepts_checkpoint_but_cannot_dispatch(self):
        assignment = self.claim()
        changed = deepcopy(self.control)
        changed.update(revision=2, action="pause")
        items.reconcile(self.record, changed)
        items.checkpoint(self.record, result(assignment), "session-1", None)
        self.assertIsNone(items.claim(self.record, changed))

    def test_result_attribution_and_duplicate_checkpoint_are_exact(self):
        assignment = self.claim()
        for key, value in (("worker_id", "foreign"), ("assignment_id", "foreign"),
                           ("evaluated_revision", 2), ("item_id", "foreign")):
            candidate = result(assignment)
            candidate[key] = value
            with self.assertRaises(ValueError):
                items.checkpoint(self.record, candidate, "session-1", None)
        completed = result(assignment)
        items.checkpoint(self.record, completed, "session-1", None)
        items.checkpoint(self.record, completed, "session-1", None)
        completed["summary"] = "Different"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            items.checkpoint(self.record, completed, "session-1", None)

    def test_product_bug_from_flaky_intake_needs_host_validation_and_approval(self):
        self.control["reported_kind"] = "flaky_test"
        self.record = items.new_record(self.control)
        assignment = self.finish()
        preview = items.preview(self.record, self.control)
        self.assertEqual("pr_preview", preview["kind"])
        self.assertIsNone(items.pr_request(self.record, self.control))
        approved = deepcopy(self.control)
        approved.update(revision=2, action="publish", pr_review={
            "assignment_id": assignment["id"], "preview_revision": 1,
            "title": preview["title"], "body": preview["body"]})
        items.reconcile(self.record, approved)
        request = items.pr_request(self.record, approved)
        self.assertTrue(request["draft"])
        self.assertEqual("b" * 40, request["resulting_head"])
        self.assertEqual(self.control["authority"]["destination"], request["destination"])

    def test_claims_and_failed_validation_never_authorize_pr_preview(self):
        for field, value in (("actual_classification", "flaky_test_candidate"),
                             ("outcome", "inconclusive"), ("outcome", "out_of_scope"),
                             ("same_failure", False), ("tests", []), ("changed_files", [])):
            with self.subTest(field=field, value=value):
                record = items.new_record(self.control)
                assignment = items.claim(record, self.control)
                assignment["delivery"] = "delivered"
                claim = result(assignment)
                claim[field] = value
                items.checkpoint(record, claim, "session-1", validation(assignment))
                self.assertIsNone(items.preview(record, self.control))
        for key in ("product_bug", "same_failure", "in_scope"):
            record = items.new_record(self.control)
            assignment = items.claim(record, self.control)
            assignment["delivery"] = "delivered"
            checked = validation(assignment)
            checked[key] = False
            items.checkpoint(record, result(assignment), "session-1", checked)
            self.assertIsNone(items.preview(record, self.control))
        record = items.new_record(self.control)
        assignment = items.claim(record, self.control)
        assignment["delivery"] = "delivered"
        items.checkpoint(record, result(assignment), "session-1", None)
        self.assertIsNone(items.preview(record, self.control))

    def test_approval_cannot_survive_policy_route_or_content_drift(self):
        assignment = self.finish()
        preview = items.preview(self.record, self.control)
        for change in ("route", "scope", "destination", "title", "head"):
            with self.subTest(change=change):
                record = deepcopy(self.record)
                approved = deepcopy(self.control)
                approved.update(revision=2, action="publish", pr_review={
                    "assignment_id": assignment["id"], "preview_revision": 1,
                    "title": preview["title"], "body": preview["body"]})
                if change == "route":
                    approved["requested_route"] = "product_bug"
                elif change == "scope":
                    approved["scope"] = "Another fix"
                elif change == "destination":
                    approved["authority"]["destination"]["base"] = "main"
                elif change == "title":
                    approved["pr_review"]["title"] = "Another title"
                else:
                    record["assignments"][-1]["validation"]["resulting_head"] = "c" * 40
                    with self.assertRaisesRegex(ValueError, "preview evidence"):
                        items.reconcile(record, approved)
                    continue
                items.reconcile(record, approved)
                self.assertIsNone(items.pr_request(record, approved))

    def test_optional_ledger_storage_preserves_legacy_shape(self):
        ledger = pilot_state.new_ledger()
        original = pilot_state.render(ledger)
        self.assertEqual(ledger, pilot_state.parse(original))
        ledger["workItems"] = [self.record]
        self.assertEqual(ledger, pilot_state.parse(pilot_state.render(ledger)))
        ledger["workItems"].append(deepcopy(self.record))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            pilot_state.validate(ledger)
