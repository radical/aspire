import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import worker_gate

NOW = "2026-11-20T00:00:00Z"


def base_state():
    return {
        "now": NOW,
        "runInputs": {"complete": True, "items": [{"kind": "cron"}], "pendingFeedback": 0},
        "quota": {"restRemaining": 4000, "graphqlRemaining": 4000},
        "slots": {"complete": True, "active": []},
        "handoffs": {"complete": True, "items": []},
        "ready": {"complete": True, "issues": []},
    }


def ready(number, *labels):
    return {"number": number, "labels": list(labels)}


class GateTests(unittest.TestCase):
    def gate(self, state):
        return worker_gate.decide_gate(state)

    def gate_cli(self, state):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(state))), redirect_stdout(out):
            self.assertEqual(worker_gate.main(["worker_gate.py", "gate"]), 0)
        return json.loads(out.getvalue())

    def test_idle_cron_tick_exits(self):
        self.assertEqual(self.gate(base_state())["decision"], "EXIT")

    def test_non_cron_input_on_any_page_is_work(self):
        # A later inbox page carrying a closed-PR event must not be hidden by a
        # first page that only holds the cron tick.
        state = base_state()
        state["runInputs"]["items"] = [{"kind": "cron"}, {"kind": "pull_request.closed", "pr": 123}]
        self.assertEqual(self.gate(state)["decision"], "WORK")

    def test_pending_feedback_is_work(self):
        state = base_state()
        state["runInputs"]["pendingFeedback"] = 1
        self.assertEqual(self.gate(state)["decision"], "WORK")

    def test_partial_sections_never_exit(self):
        for key in ("runInputs", "slots", "handoffs", "ready"):
            with self.subTest(section=key):
                state = base_state()
                state[key]["complete"] = False
                self.assertEqual(self.gate_cli(state)["decision"], "RECONCILE")

    def test_missing_or_malformed_input_never_exits(self):
        cases = {
            "missing slots": lambda s: s.pop("slots"),
            "bad now": lambda s: s.update(now="yesterday"),
            "missing quota": lambda s: s["quota"].pop("graphqlRemaining"),
            "bad feedback": lambda s: s["runInputs"].update(pendingFeedback="0"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                state = base_state()
                mutate(state)
                self.assertEqual(self.gate_cli(state)["decision"], "RECONCILE")

    def test_unparseable_stdin_is_reconcile(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("{not json")), redirect_stdout(out):
            worker_gate.main(["worker_gate.py", "gate"])
        self.assertEqual(json.loads(out.getvalue())["decision"], "RECONCILE")

    def test_low_quota_is_reconcile_not_exit(self):
        state = base_state()
        state["quota"]["restRemaining"] = 499
        self.assertEqual(self.gate(state)["decision"], "RECONCILE")

    def test_conflicting_slot_owners_reconcile(self):
        state = base_state()
        state["slots"]["active"] = [{"issue": 7, "priority": "P1"}, {"issue": 7, "priority": "P1"}]
        self.assertEqual(self.gate_cli(state)["decision"], "RECONCILE")

    def test_slotless_due_wait_is_work(self):
        # External waits hold no slot but still own the item; a due one must wake.
        state = base_state()
        state["handoffs"]["items"] = [
            {"issue": 9, "state": "waiting", "unresolvedEffects": False, "dueAt": "2026-11-19T23:59:00Z"}
        ]
        self.assertEqual(self.gate(state)["decision"], "WORK")

    def test_future_wait_with_full_record_exits(self):
        state = base_state()
        state["handoffs"]["items"] = [
            {"issue": 9, "state": "waiting", "unresolvedEffects": False, "dueAt": "2026-11-20T03:00:00Z"}
        ]
        self.assertEqual(self.gate(state)["decision"], "EXIT")

    def test_unresolved_or_unknown_effects_are_work(self):
        for effects in (True, None):
            with self.subTest(effects=effects):
                state = base_state()
                record = {"issue": 9, "state": "yielded", "dueAt": "2026-11-20T03:00:00Z"}
                if effects is not None:
                    record["unresolvedEffects"] = effects
                state["handoffs"]["items"] = [record]
                self.assertEqual(self.gate(state)["decision"], "WORK")

    def test_live_handoff_without_due_reconciles(self):
        state = base_state()
        state["handoffs"]["items"] = [{"issue": 9, "state": "yielded", "unresolvedEffects": False}]
        self.assertEqual(self.gate(state)["decision"], "RECONCILE")

    def test_terminal_handoff_is_ignored(self):
        state = base_state()
        state["handoffs"]["items"] = [{"issue": 9, "state": "done"}]
        self.assertEqual(self.gate(state)["decision"], "EXIT")

    def test_p0_waiting_with_capacity_is_work(self):
        state = base_state()
        state["ready"]["issues"] = [ready(42, "cif:ready", "cif:p0", "cif")]
        decision = self.gate(state)
        self.assertEqual((decision["decision"], decision["candidates"]), ("WORK", [42]))

    def test_p0_waiting_without_capacity_is_not_fast_exit(self):
        state = base_state()
        state["slots"]["active"] = [{"issue": 1, "priority": "P0"}, {"issue": 2, "priority": "P0"}]
        state["ready"]["issues"] = [ready(42, "cif", "cif:p0", "cif:ready")]
        self.assertEqual(self.gate(state)["decision"], "RECONCILE")

    def test_p1_waiting_with_room_is_work_and_without_room_exits(self):
        state = base_state()
        state["ready"]["issues"] = [ready(50, "cif", "cif:ready")]
        self.assertEqual(self.gate(state)["decision"], "WORK")
        full = copy.deepcopy(state)
        full["slots"]["active"] = [{"issue": n, "priority": "P1"} for n in (1, 2, 3)]
        self.assertEqual(self.gate(full)["decision"], "EXIT")

    def test_owned_or_unready_issues_are_not_candidates(self):
        state = base_state()
        state["slots"]["active"] = [{"issue": 42, "priority": "P0"}]
        state["ready"]["issues"] = [ready(42, "cif", "cif:p0", "cif:ready"), ready(43, "cif", "cif:p0")]
        self.assertEqual(self.gate(state)["decision"], "EXIT")


def wake_state():
    return {
        "release": {
            "verified": True, "issue": 10, "runId": "run-1",
            "claimNonce": "nonce-1", "releasedSlotEtag": "etag-1",
        },
        "slotsAfterRelease": {"complete": True, "active": [{"issue": 11, "priority": "P1"}]},
        "handoffs": {"complete": True, "items": []},
        "ready": {"complete": True, "issues": [ready(42, "cif", "cif:p0", "cif:ready")]},
    }


class WakeTests(unittest.TestCase):
    def test_verified_release_with_waiting_p0_wakes(self):
        decision = worker_gate.decide_wake(wake_state())
        self.assertTrue(decision["wake"])
        self.assertEqual(decision["waitingP0"], [42])

    def test_request_key_is_stable_for_retry_and_distinct_per_release(self):
        first = worker_gate.decide_wake(wake_state())["requestId"]
        self.assertEqual(worker_gate.decide_wake(wake_state())["requestId"], first)
        other = wake_state()
        other["release"]["releasedSlotEtag"] = "etag-2"
        self.assertNotEqual(worker_gate.decide_wake(other)["requestId"], first)

    def test_unverified_release_never_wakes(self):
        state = wake_state()
        state["release"]["verified"] = False
        self.assertFalse(worker_gate.decide_wake(state)["wake"])

    def test_only_a_different_p0_counts(self):
        state = wake_state()
        state["ready"]["issues"] = [ready(10, "cif", "cif:p0", "cif:ready")]
        self.assertFalse(worker_gate.decide_wake(state)["wake"])

    def test_p0_owned_by_handoff_does_not_wake(self):
        state = wake_state()
        state["handoffs"]["items"] = [{"issue": 42, "state": "waiting"}]
        self.assertFalse(worker_gate.decide_wake(state)["wake"])

    def test_no_visible_p0_capacity_does_not_wake(self):
        state = wake_state()
        state["slotsAfterRelease"]["active"] = [
            {"issue": 1, "priority": "P0"}, {"issue": 2, "priority": "P0"}
        ]
        self.assertFalse(worker_gate.decide_wake(state)["wake"])

    def test_source_still_holding_slot_does_not_wake(self):
        state = wake_state()
        state["slotsAfterRelease"]["active"].append({"issue": 10, "priority": "P0"})
        self.assertFalse(worker_gate.decide_wake(state)["wake"])

    def test_partial_state_does_not_wake(self):
        state = wake_state()
        state["ready"]["complete"] = False
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(state))), redirect_stdout(out):
            worker_gate.main(["worker_gate.py", "wake"])
        self.assertFalse(json.loads(out.getvalue())["wake"])


class ProposedTriggerTests(unittest.TestCase):
    def setUp(self):
        with open("triggers.proposed.json") as handle:
            self.config = json.load(handle)

    def matches(self, trigger, event, added, labels):
        if trigger.get("event") != event or added not in trigger.get("labels", []):
            return False
        return any(set(required).issubset(labels) for required in trigger["itemLabels"])

    def test_ready_issue_wakes_whichever_label_lands_last(self):
        labels = {"cif", "cif:p0", "cif:ready"}
        for last in ("cif:ready", "cif:p0"):
            with self.subTest(last=last):
                self.assertTrue(any(
                    self.matches(t, "issues.labeled", last, labels) for t in self.config["triggers"]))

    def test_p0_label_alone_does_not_wake(self):
        self.assertFalse(any(
            self.matches(t, "issues.labeled", "cif:p0", {"cif", "cif:p0"}) for t in self.config["triggers"]))

    def test_pr_triggers_are_preserved(self):
        with open("triggers.json") as handle:
            baseline = json.load(handle)["triggers"]
        proposed = self.config["triggers"]
        for trigger in baseline:
            if trigger["event"].startswith(("pull_request", "issue_comment")):
                self.assertIn(trigger, proposed)


if __name__ == "__main__":
    unittest.main()
