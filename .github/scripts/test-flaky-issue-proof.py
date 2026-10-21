import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("issue_proof", ROOT / ".github/scripts/flaky-issue-proof.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
CONFIG = {
    "version": 1, "target": "hosting-json", "max_rounds": 2,
    "max_fix_attempts": 2, "max_executions": 12, "retry_probe": False,
    "resume_delay_seconds": 0, "cancel_probe": False,
}


def issue(config=None):
    return {
        "number": 42, "state": "open", "user": {"login": "radical"},
        "labels": [{"name": proof.LABEL}],
        "body": "Fork-only validation.\n```json\n" + json.dumps(config or CONFIG) + "\n```",
    }


def state():
    return {
        "version": 1, "issue_number": 42, "issue_body": issue()["body"],
        "config": CONFIG.copy(), "phase": "awaiting-agent", "stage": "start",
        "agent_revision": "a" * 40, "agent_run_id": "123",
        "agents": ["123"], "workers": [], "candidates": [], "requested_executions": 0,
    }


def proposal():
    return {
        "version": 1, "issue_number": 42, "state_revision": "a" * 40,
        "action": "reproduce", "explanation": "Read the exact source; request one baseline.",
        "evidence_read": ["test-source.cs"], "iterations": 1, "runners": 1,
    }


def receipt(failed=0, phase="reproduce", count=1):
    return {
        "run_id": "555", "phase": phase, "candidate_sha": "b" * 40,
        "executed": count, "failed": failed, "other_failed": 0,
        "iterations_per_runner": count, "runners": 1,
        "iterations": [{"iteration": i, "failed": failed} for i in range(1, count + 1)],
    }


class IssueProofChecks(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": proof.REPOSITORY, "GITHUB_RUN_ID": "900",
            "ISSUE_NUMBER": "42",
        })
        self.environment.start()
        proof.configure(issue())

    def tearDown(self):
        self.environment.stop()

    def test_owner_enrollment_accepts_bounded_contract(self):
        self.assertEqual(CONFIG, proof.configuration(issue()))

    def test_rejects_unenrolled_foreign_closed_and_pr_issues(self):
        for changes in (
            {"state": "closed"}, {"user": {"login": "outsider"}},
            {"labels": []}, {"pull_request": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                proof.configuration({**issue(), **changes})

    def test_rejects_unknown_and_incomplete_contracts(self):
        for changes in (
            {"target": "../shell"}, {"unknown": True}, {"max_rounds": 0},
            {"max_executions": 999}, {"max_fix_attempts": True},
            {"resume_delay_seconds": 121}, {"retry_probe": "true"},
            {"cancel_probe": 1}, {"version": True}, {"retry_probe": True}, {"target": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                proof.configuration(issue({**CONFIG, **changes}))

    def test_rejects_ambiguous_multiple_contracts(self):
        data = issue()
        data["body"] += "\n```json\n{}\n```"
        with self.assertRaises(ValueError):
            proof.configuration(data)

    def test_rejects_unsafe_identifiers(self):
        for value in ("0", "-1", "4\n2", "../42", "42;echo", 42, "9" * 20):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proof.number(value)

    def test_rejects_secret_shaped_publication(self):
        for value in ("gho_" + "x" * 30, "https://example.invalid/?sig=secret", "github_pat_secret", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proof.safe_text(value)

    def test_proposal_is_bound_to_issue_and_revision(self):
        proof.validate_proposal(state(), proposal())
        for changes in (
            {"issue_number": 43}, {"state_revision": "b" * 40}, {"action": "shell"},
            {"version": True}, {"evidence_read": []}, {"explanation": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                proof.validate_proposal(state(), {**proposal(), **changes})

    def test_worker_requests_reserve_exact_parallel_executions(self):
        request = proof.worker_request(state(), "reproduce", "b" * 40, 2, 3)
        self.assertEqual(6, request["reserved_executions"])
        self.assertEqual("900", request["parent_run_id"])

    def test_execution_budget_cannot_be_exceeded(self):
        data = state()
        data["requested_executions"] = 11
        with self.assertRaisesRegex(ValueError, "remaining"):
            proof.worker_request(data, "verify", "b" * 40, 1, 2)

    def test_rejects_invalid_matrix_dimensions(self):
        for iterations, runners in ((0, 1), (1, 0), (4, 1), (1, 4), (True, 1), (1, "2")):
            with self.subTest(iterations=iterations, runners=runners), self.assertRaises(ValueError):
                proof.worker_request(state(), "reproduce", "b" * 40, iterations, runners)

    def test_cancel_probe_reserves_no_fake_test_execution(self):
        request = proof.worker_request(state(), "cancel-probe", "b" * 40, 1, 1)
        self.assertEqual(0, request["reserved_executions"])

    def test_state_read_uses_one_immutable_revision(self):
        data = state()
        document = {"content": base64.b64encode(json.dumps(data).encode()).decode()}
        with patch.object(proof, "optional", return_value={"object": {"sha": "f" * 40}}), \
                patch.object(proof, "api", return_value=document) as api:
            restored, revision = proof.load("42")
        self.assertEqual(data, restored)
        self.assertEqual("f" * 40, revision)
        api.assert_called_once_with("contents/state.json?ref=" + "f" * 40)

    def test_state_update_is_a_nonforced_cas(self):
        results = [{"sha": "blob"}, {"sha": "tree"}, {"sha": "new"}, {}]
        with patch.object(proof, "api", side_effect=results) as api:
            self.assertEqual("new", proof.save(state(), "old"))
        commit = api.call_args_list[2].args[1]
        self.assertEqual(["old"], commit["parents"])
        self.assertEqual(("git/refs/heads/memory/flaky-issue-controller-42", {"sha": "new", "force": False}, "PATCH"), api.call_args_list[3].args)

    def test_dispatch_requires_server_receipt_and_never_retries(self):
        with patch.object(proof, "api", return_value=None) as api, self.assertRaisesRegex(ValueError, "blindly"):
            proof.dispatch(proof.AGENT, {"issue_number": "42"})
        self.assertEqual(1, api.call_count)
        self.assertTrue(api.call_args.args[1]["return_run_details"])

    def test_dispatch_records_actual_run_id(self):
        with patch.object(proof, "api", return_value={"workflow_run_id": 9123}):
            self.assertEqual("9123", proof.dispatch(proof.AGENT, {}))

    def test_empty_missing_and_unrelated_evidence_cannot_pass(self):
        self.assertTrue(proof.complete_evidence(receipt()))
        for changes in (
            {"executed": 0}, {"iterations": []}, {"other_failed": 1},
            {"evidence_error": "TRX missing"}, {"runners": 2},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(proof.complete_evidence({**receipt(), **changes}))

    def test_pr_gate_rejects_untested_and_wrong_commit_candidates(self):
        data = state()
        data["candidate"] = {"candidate_sha": "b" * 40, "source_blob": "blob", "is_probe": False}
        for evidence in ([], [receipt(phase="reproduce")], [receipt(failed=1, phase="verify", count=3)]):
            data["workers"] = evidence
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                proof.require_verified(data, {"reviewed_source_blob": "blob"})

    def test_pr_gate_requires_published_source_review(self):
        data = state()
        data["candidate"] = {"candidate_sha": "b" * 40, "source_blob": "blob", "is_probe": False}
        data["workers"] = [receipt(phase="verify", count=9)]
        with self.assertRaisesRegex(ValueError, "review"):
            proof.require_verified(data, {"reviewed_source_blob": "wrong"})

    def test_nonreproduction_cannot_hide_a_failure(self):
        data = state()
        data["workers"] = [receipt(failed=1)]
        with patch.object(proof, "save") as save, patch.object(proof, "terminal_comment"), self.assertRaises(ValueError):
            proof.finish(data, "old", "not-reproduced", "No failure.")
        save.assert_not_called()

    def test_duplicate_worker_callback_does_not_dispatch(self):
        data = state()
        with patch.object(proof, "agent_dispatch") as dispatch:
            proof.collect(data, "old", "555")
        dispatch.assert_not_called()

    def test_stale_agent_callback_does_not_dispatch(self):
        with patch.object(proof, "proposal_for") as proposal:
            proof.handle_agent(state(), "old", "999")
        proposal.assert_not_called()

    def test_foreign_cancel_cannot_reach_cancel_api(self):
        data = state()
        data["stage"] = "awaiting-cancellation"
        data["worker"] = {"run_id": "555", "phase": "cancel-probe"}
        request = {**proposal(), "action": "cancel", "worker_run_id": "666"}
        with patch.object(proof, "proposal_for", return_value=request), \
                patch.object(proof, "api") as api, self.assertRaisesRegex(ValueError, "unrelated"):
            proof.handle_agent(data, "old", "123")
        api.assert_not_called()

    def test_one_matching_baseline_is_required_before_publication(self):
        data = state()
        data["config"]["target"] = "openai-controlled"
        with patch.object(proof, "api") as api, self.assertRaisesRegex(ValueError, "baseline"):
            proof.publish(data, "old", proposal())
        api.assert_not_called()

    def test_passing_target_cannot_publish(self):
        with patch.object(proof, "api") as api, self.assertRaisesRegex(ValueError, "not authorized"):
            proof.publish(state(), "old", proposal())
        api.assert_not_called()

    def test_agent_budget_stops_before_any_dispatch(self):
        data = state()
        data["agents"] = [str(i) for i in range(10)]
        with patch.object(proof, "dispatch") as dispatch, self.assertRaisesRegex(ValueError, "ten-agent"):
            proof.agent_dispatch(data, "old")
        dispatch.assert_not_called()

    def test_owned_agent_is_watched_and_consumed_without_requiring_callback(self):
        data = state()
        with patch.object(proof, "save", side_effect=["intent", "recorded"]), \
                patch.object(proof, "dispatch", return_value="999"), \
                patch.object(proof.repair.real, "wait_completed") as wait, \
                patch.object(proof, "handle_agent") as handle:
            proof.agent_dispatch(data, "previous")
        wait.assert_called_once_with("999", True)
        handle.assert_called_once_with(data, "recorded", "999")

    def test_failed_agent_still_records_blocker_without_a_callback(self):
        data = state()
        with patch.dict(os.environ, {"EVENT": "intake"}), \
                patch.object(proof, "api", return_value=issue()), \
                patch.object(proof, "load", side_effect=[(None, None), (data, "recorded")]), \
                patch.object(proof, "save", return_value="revision"), \
                patch.object(proof, "dispatch", return_value="999"), \
                patch.object(proof.repair.real, "wait_completed", side_effect=ValueError("model failed")), \
                patch.object(proof, "finish") as finish, patch.object(proof.sys, "argv", ["proof", "advance"]):
            with self.assertRaisesRegex(ValueError, "model failed"):
                proof.main()
        self.assertEqual("blocked", finish.call_args.args[2])
        self.assertIn("model failed", finish.call_args.args[3])

    def test_missing_proposal_is_an_explicit_controller_error(self):
        data = state()
        run = {"display_title": "Issue investigator #42 [" + data["agent_revision"] + "]"}
        with tempfile.TemporaryDirectory() as directory, patch.object(proof, "ROOT", Path(directory)), \
                patch.object(proof, "run_identity", return_value=run), \
                patch.object(proof.repair.real, "wait_completed"), \
                patch.object(proof.repair, "download"), self.assertRaisesRegex(ValueError, "without a proposal"):
            proof.proposal_for(data, "123")

    def test_terminal_comment_is_idempotent_and_automated(self):
        data = state()
        data.update(phase="not-reproduced", explanation="Both exact baselines passed.")
        marker = "<!-- flaky-issue-result:42 -->"
        with tempfile.TemporaryDirectory() as directory, patch.object(proof, "ROOT", Path(directory)), \
                patch.object(proof, "api", return_value=[{"body": marker}]) as api:
            proof.terminal_comment(data)
        api.assert_called_once()

    def test_paused_checkpoint_is_resumable_without_reprocessing_worker(self):
        data = state()
        data.update(phase="paused", resume_after=0)
        with patch.object(proof, "api", return_value=issue()), \
                patch.object(proof, "load", return_value=(data, "revision")), \
                patch.object(proof, "agent_dispatch") as dispatch, patch.object(proof.time, "sleep"):
            proof.advance("42", "resume")
        dispatch.assert_called_once_with(data, "revision")
        self.assertIn("resumed_at", data)

    def test_intake_existing_issue_does_not_create_second_investigation(self):
        with patch.object(proof, "api", return_value=issue()), \
                patch.object(proof, "load", return_value=(state(), "revision")), \
                patch.object(proof, "agent_dispatch") as dispatch:
            proof.advance("42", "intake")
        dispatch.assert_not_called()

    def test_run_id_receipt_prevents_a_duplicate_worker_from_execution(self):
        data = state()
        data["worker"] = {"run_id": "different"}
        with patch.object(proof, "api", return_value=issue()), \
                patch.object(proof, "load", return_value=(data, "revision")), self.assertRaisesRegex(ValueError, "different run"):
            proof.worker_state("42")

    def test_matrix_provides_absolute_arcade_project_path(self):
        data = state()
        request = {
            "phase": "reproduce", "parent_run_id": "900", "candidate_sha": "b" * 40,
            "configuration": {"runners": 2},
        }
        title = "Issue worker #42 reproduce [900] " + "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_WORKSPACE": "/workspace/repo"}), \
                    patch.object(proof, "worker_state", return_value=(data, request)), \
                    patch.object(proof, "run_identity", return_value={"display_title": title}):
                proof.matrix("42")
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(
            "/workspace/repo/candidate/tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj",
            values["project"],
        )
        self.assertEqual({"include": [{"index": 1}, {"index": 2}]}, json.loads(values["matrix"]))


if __name__ == "__main__":
    unittest.main()
