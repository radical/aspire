from __future__ import annotations

import hashlib
import copy
import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.retrospective import (
    build_retrospective_request,
    build_run_completion,
    normalize_retrospective_result,
    render_retrospective_markdown,
)
from ci_shepherd.assessment_batches import materialize_assessment, verify_assessment_completion
from ci_shepherd.lifecycle import snapshot_id_for
from tests.assessment_helpers import write_assessment_receipts


class RetrospectiveContextTests(unittest.TestCase):
    def test_seal_distinguishes_one_shot_pending_from_execution_and_failed_launch(self) -> None:
        rows = [
            ("prepared", "prepared", "not-dispatched"),
            ("dispatching", "dispatch-unconfirmed", "unknown"),
            ("failed", "not-launched", "not-launched"),
            ("abandoned", "abandoned", "unknown"),
        ]
        self.write(self.work / "investigation-plan.json", {
            **self.identity, "requests": [],
            "pendingInvestigations": [{"investigationId": f"investigation:{status}"} for status, _, _ in rows],
        })
        (self.state / "ledgers").mkdir()
        (self.state / "ledgers" / "investigation-sessions.jsonl").write_text(
            "".join(json.dumps({
                "repository": "owner/repo", "investigationId": f"investigation:{status}", "status": status,
                "launchMode": "one-shot", "attemptId": f"logical:{status}", "executionState": execution,
                "sessionId": None, "runtimeSessionId": None, "workerIdentityKind": "unknown",
            }) + "\n" for status, _, execution in rows), encoding="utf-8",
        )
        completion = build_run_completion(self.work, self.state, sealed_at="2026-09-09T00:00:00Z")
        self.assertEqual(
            {f"investigation:{status}": expected for status, expected, _ in rows},
            {row["investigationId"]: row["status"] for row in completion["investigationWork"]},
        )
        for row in completion["investigationWork"]:
            self.assertIsNone(row["runtimeSessionId"])
            self.assertEqual("unknown", row["workerIdentityKind"])
            self.assertEqual("one-shot", row["launchMode"])
        self.assertEqual(4, len(completion["missingInvestigationIds"]))
        self.assertEqual([], completion["investigationResults"])

    def setUp(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        self.scratch = TemporaryDirectory(dir=artifacts)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.work = self.root / "run"
        self.state = self.root / "state"
        self.work.mkdir()
        self.state.mkdir()
        self.snapshot = {"repository": "owner/repo", "collectedAt": "2026-09-08T16:35:56Z", "evidence": {}}
        self.identity = {"repository": "owner/repo", "snapshotId": snapshot_id_for(self.snapshot)}
        self.write(self.work / "cycle.json", {**self.identity, "stage": "completed"})
        self.write(self.work / "action-proposals.json", {
            **self.identity, "proposals": [{"actionId": "action:planned"}],
        })
        self.write(self.work / "investigation-plan.json", {
            **self.identity, "requests": [{"investigationId": "investigation:planned"}],
            "deferredRequests": [{"investigationId": "investigation:deferred",
                                  "reason": "per-cycle-investigation-budget"}],
        })
        self.write(self.work / "quarantine-session.json", self.identity)
        (self.work / "report.md").write_text("# Cycle report\n", encoding="utf-8")
        self.invocation = {
            "repository": "owner/repo", "runId": "run:current", "mode": "action-free",
            "grantsAllowed": False, "githubMutationsAllowed": False,
            "implementationChangesAllowed": False,
            "stateDirectory": str(self.state), "stateBootstrap": "new",
            "scope": "whole-invocation",
            "startedAt": "2026-09-08T16:35:56Z", "completedAt": "2026-09-08T17:03:24Z",
            "investigationBlockers": [{
                "investigationId": "investigation:planned", "status": "not-started",
                "reason": "Investigation checkout is not clean.",
            }],
        }
        self.write(self.root / "invocation.json", self.invocation)
        (self.root / "operator-report.md").write_text(
            "Five workers were not started; no writes were permitted.\n", encoding="utf-8",
        )
        self.context = {
            "schemaVersion": 1, **self.identity, "runId": "run:current",
            "cycleSha256": self.digest(self.work / "cycle.json"),
            "invocation": {"path": "invocation.json", "sha256": self.digest(self.root / "invocation.json")},
            "operatorReport": {"path": "operator-report.md", "sha256": self.digest(self.root / "operator-report.md")},
        }
        self.context_path = self.root / "context.json"
        self.write(self.context_path, self.context)

    @staticmethod
    def write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_cli_builds_validated_context_without_manual_hash_bindings(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        output = self.root / "generated-context.json"
        invocation_path = self.root / "invocation.json"
        report_path = self.root / "operator-report.md"
        before = {path: path.read_bytes() for path in (invocation_path, report_path, self.work / "cycle.json")}
        result = subprocess.run([
            sys.executable, str(script), "context", "--work-dir", str(self.work),
            "--invocation", str(invocation_path), "--operator-report", str(report_path),
            "--output", str(output),
        ], text=True, capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        context = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual({
            "schemaVersion": 1, **self.identity, "runId": self.invocation["runId"],
            "cycleSha256": self.digest(self.work / "cycle.json"),
            "invocation": {"path": str(invocation_path), "sha256": self.digest(invocation_path)},
            "operatorReport": {"path": str(report_path), "sha256": self.digest(report_path)},
        }, context)
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=output,
        )
        self.assertEqual("action-free", completion["context"]["invocation"]["mode"])
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_context_command_accepts_relative_inputs_and_an_absent_operator_report(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        result = subprocess.run([
            sys.executable, str(script), "context", "--work-dir", "run",
            "--invocation", "invocation.json", "--output", "review/context.json",
        ], cwd=self.root, text=True, capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        context_path = self.root / "review" / "context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"schemaVersion", "repository", "snapshotId", "runId", "cycleSha256", "invocation"},
            set(context),
        )
        self.assertEqual(str(self.root / "invocation.json"), context["invocation"]["path"])
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=context_path,
        )
        self.assertIsNone(completion["context"]["operatorReport"])

    def test_context_command_rejects_invalid_sources_before_writing(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        invocation = self.root / "invocation.json"
        for field, value in (
            ("repository", "unrelated/repo"), ("snapshotId", "unrelated-snapshot"),
            ("runId", ""), ("githubMutationsAllowed", True),
        ):
            with self.subTest(field=field):
                self.write(invocation, {**self.invocation, field: value})
                output = self.root / f"invalid-{field}.json"
                result = subprocess.run([
                    sys.executable, str(script), "context", "--work-dir", str(self.work),
                    "--invocation", str(invocation), "--output", str(output),
                ], text=True, capture_output=True, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(output.exists())
        self.write(invocation, self.invocation)
        original = invocation.read_bytes()
        alias = subprocess.run([
            sys.executable, str(script), "context", "--work-dir", str(self.work),
            "--invocation", str(invocation), "--output", str(invocation),
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(0, alias.returncode)
        self.assertIn("overwrite an input", alias.stderr)
        self.assertEqual(original, invocation.read_bytes())

    def add_assessment(self) -> dict[str, object]:
        issue = {"issueNumber": 1, "evidenceBundle": [{"id": "issue:1", "payload": {"body": "Evidence"}}]}
        judgment = {"issueNumber": 1, "recommendations": []}
        for name, document in {
            "input.json": self.snapshot,
            "assessment-input.json": {**self.identity, "issues": [issue]},
            "assessment-defaults.json": {"issues": [{"issueNumber": 1, "defaultJudgment": judgment}]},
            "agent-input.json": {},
            "review-selection.json": {"selected": [{"issueNumber": 1}]},
            "pull-request-review.json": {"tasks": []},
        }.items():
            self.write(self.work / name, document)
        assessment = materialize_assessment(self.work)
        self.write(self.work / "cycle.json", {
            **self.identity, "stage": "completed", "assessment": assessment,
        })
        return assessment

    def test_new_cycles_cannot_be_sealed_without_assessment_receipts(self) -> None:
        self.add_assessment()
        with self.assertRaisesRegex(ValueError, "Assessment receipts"):
            build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual([], list(self.state.iterdir()))

    def test_partial_new_assessment_metadata_is_not_treated_as_legacy(self) -> None:
        self.write(self.work / "cycle.json", {
            **self.identity, "stage": "completed", "previousAssessment": {"assessmentId": "previous"},
        })
        with self.assertRaises(ValueError):
            build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")

    def test_verified_assessment_is_frozen_and_stale_receipts_cannot_be_prepared(self) -> None:
        assessment = self.add_assessment()
        receipts = write_assessment_receipts(self.work)
        verified = verify_assessment_completion(self.work, assessment)
        self.write(self.work / "assessment-completion.json", verified)
        completion = build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual(verified, completion["assessmentCompletion"])
        self.write(self.root / "completion.json", completion)
        request = build_retrospective_request(
            self.work, reviewed_session_id="session:reviewed",
            completion_path=self.root / "completion.json",
        )
        self.assertEqual(
            verified,
            json.loads(request["frozenEvidence"]["assessment-completion.json"]["content"]),
        )
        self.assertIn("assessment-receipts.json", request["evidencePaths"])
        receipts["batches"] = []
        self.write(self.work / "assessment-receipts.json", receipts)
        with self.assertRaisesRegex(ValueError, "Assessment receipts"):
            build_retrospective_request(
                self.work, reviewed_session_id="session:reviewed",
                completion_path=self.root / "completion.json",
            )

    def test_assessment_counts_and_packet_paths_cannot_bypass_sealing_validation(self) -> None:
        assessment = self.add_assessment()
        write_assessment_receipts(self.work)
        verified = verify_assessment_completion(self.work, assessment)
        self.write(self.work / "assessment-completion.json", {**verified, "caseCount": 999})
        with self.assertRaisesRegex(ValueError, "Assessment completion"):
            build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.write(self.work / "assessment-completion.json", verified)
        packet = self.work / "assessment-batch-0001.json"
        external = self.root / "packet.json"
        external.write_bytes(packet.read_bytes())
        packet.unlink()
        packet.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symlink"):
            build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")

    def test_seal_checks_preserved_pre_expansion_assessment(self) -> None:
        previous = self.add_assessment()
        write_assessment_receipts(self.work)
        previous_completion = verify_assessment_completion(self.work, previous)
        self.write(self.work / "assessment-completion.json", previous_completion)
        for name in (
            "assessment-batches.json", "assessment-batch-0001.json",
            "assessment-receipts.json", "assessment-completion.json", "input.json",
        ):
            path = self.work / name
            (self.work / name.replace(".json", ".pre-expansion.json")).write_bytes(path.read_bytes())
        self.snapshot["expansions"] = [{"round": 1}]
        self.identity["snapshotId"] = snapshot_id_for(self.snapshot)
        current = self.add_assessment()
        write_assessment_receipts(self.work)
        current_completion = verify_assessment_completion(self.work, current)
        self.write(self.work / "assessment-completion.json", current_completion)
        self.write(self.work / "cycle.json", {
            **self.identity, "stage": "completed", "assessment": current,
            "previousAssessment": previous, "evidenceExpansionRound": 1,
        })
        for name in ("action-proposals.json", "investigation-plan.json", "quarantine-session.json"):
            document = json.loads((self.work / name).read_text(encoding="utf-8"))
            self.write(self.work / name, {**document, **self.identity})
        completion = build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual(current_completion, completion["assessmentCompletion"])
        self.assertEqual(previous_completion, completion["previousAssessmentCompletion"])
        self.write(self.work / "assessment-receipts.pre-expansion.json", {
            "schemaVersion": 1, "assessmentId": previous["assessmentId"], "batches": [],
        })
        with self.assertRaisesRegex(ValueError, "Assessment receipts"):
            build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")

    def test_freezes_action_free_launch_blockers_without_claiming_complete_outcomes(self) -> None:
        original = {path.name: path.read_bytes() for path in self.work.iterdir()}
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
            context_path=self.context_path,
        )
        self.write(self.root / "completion.json", completion)
        request = build_retrospective_request(
            self.work, reviewed_session_id="session:reviewed",
            completion_path=self.root / "completion.json",
        )
        frozen = json.loads(request["frozenEvidence"]["retrospective-context.json"]["content"])
        self.assertEqual("action-free", frozen["invocation"]["mode"])
        self.assertFalse(frozen["invocation"]["githubMutationsAllowed"])
        self.assertEqual("not-started", frozen["invocation"]["investigationBlockers"][0]["status"])
        self.assertEqual("unknown", frozen["invocation"]["timingCompleteness"])
        self.assertEqual("new", frozen["invocation"]["stateBootstrap"])
        self.assertIn("Five workers were not started", frozen["operatorReport"])
        self.assertEqual(["investigation:planned"], completion["missingInvestigationIds"])
        self.assertEqual(["action:planned"], completion["unrecordedActionIds"])
        self.assertIn("not solely readiness", request["workerPrompt"])
        self.assertIn("not evidence of an active session", request["workerPrompt"])
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.work.iterdir()})
        self.assertEqual([], list(self.state.iterdir()))

    def test_accepts_launch_blockers_with_only_investigation_id_and_reason(self) -> None:
        blocker = self.invocation["investigationBlockers"][0]
        blocker.pop("status")
        self.write(self.root / "invocation.json", self.invocation)
        self.context["invocation"]["sha256"] = self.digest(self.root / "invocation.json")
        self.write(self.context_path, self.context)
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
            context_path=self.context_path,
        )
        self.assertEqual(
            [{**blocker, "status": "not-started"}],
            completion["context"]["invocation"]["investigationBlockers"],
        )
        item = next(item for item in completion["investigationWork"]
                    if item["investigationId"] == blocker["investigationId"])
        self.assertEqual("not-started", item["status"])
        self.assertEqual(blocker["reason"], item["reason"])

    def test_worker_prompt_uses_the_trusted_request_path_without_embedding_evidence(self) -> None:
        self.write(self.root / "completion.json", build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
        ))
        prompt_lengths = []
        for size in (100, 100_000):
            evidence = "Frozen report content\n" + "x" * size
            (self.work / "report.md").write_text(evidence, encoding="utf-8")
            request = build_retrospective_request(
                self.work, reviewed_session_id="session:reviewed",
                completion_path=self.root / "completion.json",
            )
            request_path = self.root / f"request-{size}.json"
            self.write(request_path, request)
            prompt = request["workerPrompt"]
            self.assertIn("REQUEST_PATH", prompt)
            self.assertIn("You may read that exact request JSON file", prompt)
            self.assertIn("trusted launch envelope", prompt)
            self.assertIn("Do not reopen their original paths", prompt)
            self.assertEqual(
                evidence,
                json.loads(request_path.read_text(encoding="utf-8"))["frozenEvidence"]["report.md"]["content"],
            )
            prompt_lengths.append(len(prompt))
        self.assertEqual(prompt_lengths[0], prompt_lengths[1])
        self.assertLess(prompt_lengths[1], 8_000)

    def test_seal_binds_cycle_state_before_reading_any_ledger(self) -> None:
        self.write(self.work / "cycle.json", {
            **self.identity, "stage": "completed", "stateDirectory": str(self.state),
        })
        self.write(self.state / "action-results.json", {
            "schemaVersion": 1, "repository": self.identity["repository"],
            "results": [{"actionId": "action:planned", "outcome": "executed"}],
        })
        correct = build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual([{"actionId": "action:planned", "outcome": "executed"}], correct["actionResults"])
        unrelated = self.root / "unrelated-state"
        unrelated.mkdir()
        with self.assertRaisesRegex(ValueError, "cycle.json stateDirectory"):
            build_run_completion(self.work, unrelated, sealed_at="2026-09-08T18:00:00Z")
        (unrelated / "action-results.json").write_text("{malformed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cycle.json stateDirectory"):
            build_run_completion(self.work, unrelated, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual({"status": "verified", "source": "cycle.json"}, correct["stateBinding"])

    def test_recorded_cycle_state_must_be_an_absolute_non_symlink_path(self) -> None:
        link = self.root / "state-link"
        link.symlink_to(self.state, target_is_directory=True)
        for value in (None, "state", str(link), str(self.root / "unused" / ".." / "state")):
            with self.subTest(state_directory=value):
                self.write(self.work / "cycle.json", {
                    **self.identity, "stage": "completed", "stateDirectory": value,
                })
                with self.assertRaises(ValueError):
                    build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")

    def test_legacy_state_binding_is_disclosed_without_rewriting_old_seals(self) -> None:
        completion = build_run_completion(self.work, self.state, sealed_at="2026-09-08T18:00:00Z")
        self.assertEqual("unavailable", completion["stateBinding"]["status"])
        self.assertIn("not bound to the run", completion["stateBinding"]["reason"])
        legacy = {key: value for key, value in completion.items() if key not in {"stateBinding", "stateDirectory"}}
        legacy_path = self.root / "legacy-completion.json"
        self.write(legacy_path, legacy)
        original = legacy_path.read_bytes()
        request = build_retrospective_request(
            self.work, reviewed_session_id="session:reviewed", completion_path=legacy_path,
        )
        self.assertEqual(
            legacy,
            json.loads(request["frozenEvidence"]["run-completion.json"]["content"]),
        )
        self.assertIn("missing or unavailable stateBinding", request["workerPrompt"])
        self.assertEqual(original, legacy_path.read_bytes())

    def test_result_must_match_the_frozen_evidence_not_only_the_snapshot(self) -> None:
        self.write(self.root / "completion.json", build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=self.context_path,
        ))
        request = build_retrospective_request(
            self.work, reviewed_session_id="session:reviewed",
            completion_path=self.root / "completion.json",
        )
        result = {
            "schemaVersion": 1, **self.identity, "runId": "run:current",
            "reviewedSessionId": "session:reviewed", "evidenceDigest": "0" * 64,
            "summary": "Writes were prohibited and workers never started.",
            "observations": [], "watchItems": [], "successfulSafeguards": [],
        }
        with self.assertRaisesRegex(ValueError, "evidenceDigest"):
            normalize_retrospective_result(request, result)
        result["evidenceDigest"] = request["evidenceDigest"]
        (self.root / "operator-report.md").write_text("Changed source", encoding="utf-8")
        self.assertEqual(request["evidenceDigest"], normalize_retrospective_result(request, result)["evidenceDigest"])
        changed = copy.deepcopy(request)
        changed["frozenEvidence"]["retrospective-context.json"]["content"] = "{}"
        with self.assertRaisesRegex(ValueError, "digest"):
            normalize_retrospective_result(changed, result)

    def test_cli_imports_context_into_a_new_review_without_rewriting_the_run(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        original = {path.name: path.read_bytes() for path in self.work.iterdir()}
        completion = self.root / "new-completion.json"
        request_path = self.root / "request.json"
        for arguments in (
            ["seal", "--work-dir", str(self.work), "--state-dir", str(self.state),
             "--context", str(self.context_path), "--sealed-at", "2026-09-08T18:00:00Z",
             "--output", str(completion)],
            ["prepare", "--work-dir", str(self.work), "--completion", str(completion),
             "--context", str(self.context_path), "--reviewed-session-id", "session:reviewed",
             "--output", str(request_path)],
        ):
            result = subprocess.run([sys.executable, str(script), *arguments],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertIn("retrospective-context.json", request["evidencePaths"])
        self.assertEqual("run:current", request["runId"])
        self.assertIn(request["evidenceDigest"], request["workerPrompt"])
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.work.iterdir()})
        result_path = self.root / "review-result.json"
        self.write(result_path, {
            "schemaVersion": 1, **self.identity, "runId": request["runId"],
            "reviewedSessionId": request["reviewedSessionId"], "evidenceDigest": request["evidenceDigest"],
            "summary": "Action-free launch failures were preserved.",
            "observations": [], "watchItems": [], "successfulSafeguards": [],
        })
        result = subprocess.run([
            sys.executable, str(script), "finalize", "--request", str(request_path),
            "--result", str(result_path), "--json-output", str(self.root / "review.json"),
            "--markdown-output", str(self.root / "review.md"),
        ], text=True, capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        report = self.root / "operator-report.md"
        original_report = report.read_bytes()
        denied = subprocess.run([
            sys.executable, str(script), "finalize", "--request", str(request_path),
            "--result", str(result_path), "--json-output", str(report),
            "--markdown-output", str(self.root / "denied.md"),
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(0, denied.returncode)
        self.assertIn("overwrite an input", denied.stderr)
        self.assertEqual(original_report, report.read_bytes())
        self.assertFalse((self.root / "denied.md").exists())

    def test_seal_separates_launch_failures_deferred_plans_and_registered_sessions(self) -> None:
        plan = json.loads((self.work / "investigation-plan.json").read_text(encoding="utf-8"))
        plan["requests"].append({"investigationId": "investigation:failed"})
        plan["activeInvestigations"] = [{"investigationId": "investigation:active"}]
        self.write(self.work / "investigation-plan.json", plan)
        (self.state / "ledgers").mkdir()
        (self.state / "ledgers" / "investigation-sessions.jsonl").write_text(
            "".join(json.dumps({
                "repository": "owner/repo", "investigationId": identity,
                "sessionId": f"session:{status}", "status": status,
                "failureReason": reason,
            }) + "\n" for identity, status, reason in (
                ("investigation:active", "started", None),
                ("investigation:failed", "failed", "Worker launch rejected"),
            )), encoding="utf-8",
        )
        (self.state / "ledgers" / "investigation-results.jsonl").write_text(json.dumps({
            "repository": "unrelated/repo", "investigationId": "investigation:planned",
            "outcome": "recovered",
        }) + "\n", encoding="utf-8")
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=self.context_path,
        )
        self.assertEqual({
            "investigation:planned": "not-started",
            "investigation:deferred": "deferred",
            "investigation:active": "active",
            "investigation:failed": "failed",
        }, {item["investigationId"]: item["status"] for item in completion["investigationWork"]})
        self.assertEqual(
            ["investigation:active", "investigation:failed", "investigation:planned"],
            completion["missingInvestigationIds"],
        )
        self.assertEqual([], completion["investigationResults"])

    def test_seal_refuses_to_overwrite_an_imported_operator_report(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        report = self.root / "operator-report.md"
        original = report.read_bytes()
        result = subprocess.run([
            sys.executable, str(script), "seal", "--work-dir", str(self.work),
            "--state-dir", str(self.state), "--context", str(self.context_path),
            "--sealed-at", "2026-09-08T18:00:00Z", "--output", str(report),
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("overwrite an input", result.stderr)
        self.assertEqual(original, report.read_bytes())

    def test_context_identity_paths_and_declared_facts_are_validated(self) -> None:
        mutations = (
            ("context", "repository", "another/repo"),
            ("context", "snapshotId", "another-snapshot"),
            ("context", "runId", "another-run"),
            ("context", "cycleSha256", "0" * 64),
            ("artifact", "path", "../invocation.json"),
            ("artifact", "sha256", "0" * 64),
            ("invocation", "repository", "another/repo"),
            ("invocation", "snapshotId", "another-snapshot"),
            ("invocation", "mode", []),
            ("invocation", "grantsAllowed", "false"),
            ("invocation", "githubMutationsAllowed", True),
            ("invocation", "stateDirectory", str(self.root / "unrelated-state")),
            ("invocation", "stateBootstrap", []),
            ("invocation", "timingCompleteness", "complete"),
            ("invocation", "timingCompleteness", []),
            ("invocation", "investigationBlockers", [{"status": "not-started"}]),
        )
        for scope, field, value in mutations:
            with self.subTest(scope=scope, field=field):
                context, invocation = copy.deepcopy(self.context), copy.deepcopy(self.invocation)
                if scope == "context":
                    context[field] = value
                elif scope == "artifact":
                    context["invocation"][field] = value
                else:
                    invocation[field] = value
                self.write(self.root / "invocation.json", invocation)
                if scope == "invocation":
                    context["invocation"]["sha256"] = self.digest(self.root / "invocation.json")
                self.write(self.context_path, context)
                with self.assertRaises(ValueError):
                    build_run_completion(
                        self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
                        context_path=self.context_path,
                    )

    def test_resealing_preserves_the_original_completion(self) -> None:
        original = build_run_completion(self.work, self.state, sealed_at="2026-09-08T17:00:00Z")
        destination = self.work / "run-completion.json"
        self.write(destination, original)
        original_bytes = destination.read_bytes()
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_retrospective.py"
        result = subprocess.run([
            sys.executable, str(script), "seal", "--work-dir", str(self.work),
            "--state-dir", str(self.state), "--context", str(self.context_path),
            "--sealed-at", "2026-09-08T18:00:00Z", "--output", str(destination),
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(original_bytes, destination.read_bytes())

    def test_context_sources_and_directories_cannot_traverse_symlinks(self) -> None:
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        for source in (str(link / "invocation.json"), "linked/invocation.json"):
            with self.subTest(source=source):
                context = copy.deepcopy(self.context)
                context["invocation"]["path"] = source
                self.write(self.context_path, context)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    build_run_completion(
                        self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
                        context_path=self.context_path,
                    )
        with self.assertRaisesRegex(ValueError, "symlink"):
            build_run_completion(link / "run", self.state, sealed_at="2026-09-08T18:00:00Z")

    def test_optional_context_gaps_remain_unknown_but_malformed_ledgers_fail(self) -> None:
        invocation = {"repository": "owner/repo", "runId": "run:current"}
        self.write(self.root / "invocation.json", invocation)
        self.context.pop("operatorReport")
        self.context["invocation"]["sha256"] = self.digest(self.root / "invocation.json")
        self.write(self.context_path, self.context)
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=self.context_path,
        )
        context = completion["context"]
        self.assertEqual(
            ("unknown", None, "unknown", "unknown", None),
            (context["invocation"]["mode"], context["invocation"]["grantsAllowed"],
             context["invocation"]["stateBootstrap"], context["invocation"]["timingCompleteness"],
             context["operatorReport"]),
        )
        self.assertEqual({"planned", "deferred"}, {item["status"] for item in completion["investigationWork"]})
        self.assertEqual([], completion["actionResults"])
        (self.state / "ledgers").mkdir()
        (self.state / "ledgers" / "investigation-results.jsonl").write_text("{broken\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_run_completion(
                self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=self.context_path,
            )

    def test_prepare_rejects_context_for_changed_cycle_and_changed_sealed_context(self) -> None:
        completion = build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z", context_path=self.context_path,
        )
        self.write(self.root / "completion.json", completion)
        (self.root / "operator-report.md").write_text("Revised report", encoding="utf-8")
        self.context["operatorReport"]["sha256"] = self.digest(self.root / "operator-report.md")
        self.write(self.context_path, self.context)
        with self.assertRaisesRegex(ValueError, "differs from the sealed context"):
            build_retrospective_request(
                self.work, reviewed_session_id="session:reviewed",
                completion_path=self.root / "completion.json", context_path=self.context_path,
            )
        self.write(self.work / "cycle.json", {**self.identity, "stage": "completed", "changed": True})
        with self.assertRaisesRegex(ValueError, "cycle digest"):
            build_retrospective_request(
                self.work, reviewed_session_id="session:reviewed",
                completion_path=self.root / "completion.json",
            )

    def test_prepare_rejects_oversized_combined_context_before_a_worker_runs(self) -> None:
        self.write(self.root / "completion.json", build_run_completion(
            self.work, self.state, sealed_at="2026-09-08T18:00:00Z",
        ))
        (self.root / "operator-report.md").write_text("x" * (2 * 1024 * 1024 - 100), encoding="utf-8")
        self.context["operatorReport"]["sha256"] = self.digest(self.root / "operator-report.md")
        self.write(self.context_path, self.context)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            build_retrospective_request(
                self.work, reviewed_session_id="session:reviewed", context_path=self.context_path,
                completion_path=self.root / "completion.json",
            )


class RetrospectiveTests(unittest.TestCase):
    def test_builds_bounded_request_from_completed_run_artifacts(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "run-completion.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "sealedAt": "2026-08-29T12:30:00Z",
                        "actionResults": [],
                        "investigationResults": [],
                        "missingInvestigationIds": [],
                        "unrecordedActionIds": [],
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "action-proposals.json").write_text("{}\n", encoding="utf-8")
            (work_dir / "operator-log.jsonl").write_text(
                '{"event":"action-failed"}\n',
                encoding="utf-8",
            )

            request = build_retrospective_request(
                work_dir,
                reviewed_session_id="session-123",
            )

            self.assertEqual("owner/repo", request["repository"])
            self.assertEqual("session-123", request["reviewedSessionId"])
            self.assertEqual(
                [
                    "action-proposals.json",
                    "cycle.json",
                    "report.md",
                    "run-completion.json",
                ],
                request["evidencePaths"],
            )
            self.assertIn("fresh, read-only reviewer", request["workerPrompt"])
            self.assertIn("Do not access GitHub", request["workerPrompt"])
            self.assertIn("Do not edit code", request["workerPrompt"])
            self.assertIn("Return only JSON", request["workerPrompt"])
            self.assertIn(
                "cite each evidence path exactly as the bare filename listed",
                request["workerPrompt"],
            )

    def test_rejects_run_that_has_not_completed_post_action_reconciliation(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "post-action reconciliation",
            ):
                build_retrospective_request(
                    work_dir,
                    reviewed_session_id="session-123",
                )

    def test_rejects_findings_that_cite_evidence_outside_request(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["report.md"],
        }
        result = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "summary": "The run completed with one actionable process gap.",
            "observations": [
                {
                    "severity": "medium",
                    "category": "process",
                    "title": "Action required manual recovery",
                    "detail": "The operator retried a failed action manually.",
                    "recommendation": "Record retry classification in the executor.",
                    "evidencePaths": ["missing.log"],
                }
            ],
            "watchItems": [],
            "successfulSafeguards": [],
        }

        with self.assertRaisesRegex(
            ValueError,
            "outside the retrospective request",
        ):
            normalize_retrospective_result(request, result)

    def test_rejects_result_for_a_different_run(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["report.md"],
        }
        result = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-30T12:00:00Z",
            "reviewedSessionId": "session-123",
            "summary": "This result came from a different run.",
            "observations": [],
            "watchItems": [],
            "successfulSafeguards": [],
        }

        with self.assertRaisesRegex(ValueError, "identity"):
            normalize_retrospective_result(request, result)

    def test_renders_normalized_retrospective_as_markdown(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["operator-log.jsonl", "report.md"],
        }
        result = normalize_retrospective_result(
            request,
            {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                "reviewedSessionId": "session-123",
                "summary": "The run completed and exposed one retry gap.",
                "observations": [
                    {
                        "severity": "medium",
                        "category": "reliability",
                        "title": "Transient failures need classification",
                        "detail": "The operator log contains an unclassified failure.",
                        "recommendation": "Classify retryable transport errors.",
                        "evidencePaths": ["operator-log.jsonl"],
                    }
                ],
                "watchItems": [
                    {
                        "condition": "The same transport error recurs.",
                        "reason": "Repeated failures may require a bounded retry.",
                        "evidencePaths": ["operator-log.jsonl"],
                    }
                ],
                "successfulSafeguards": [
                    {
                        "title": "Report remained evidence-linked",
                        "detail": "Every terminal action was represented in the report.",
                        "evidencePaths": ["report.md"],
                    }
                ],
            },
        )

        markdown = render_retrospective_markdown(request, result)

        self.assertIn("# CI Shepherd Run Retrospective", markdown)
        self.assertIn("## Improvement findings", markdown)
        self.assertIn("Transient failures need classification", markdown)
        self.assertIn("## Watch items", markdown)
        self.assertIn("## Safeguards that worked", markdown)
        self.assertIn("`operator-log.jsonl`", markdown)

    def test_cli_prepares_and_finalizes_retrospective_artifacts(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "action-proposals.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "proposals": [
                            {"actionId": "action:current"},
                            {"actionId": "action:unrecorded"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "investigation-plan.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "requests": [
                            {"investigationId": "investigation:complete"},
                            {"investigationId": "investigation:missing"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "quarantine-session.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "proposal": {
                            "batchId": "quarantine:current",
                        },
                        "activeBatchId": None,
                        "openBatchIds": [],
                    }
                ),
                encoding="utf-8",
            )
            state_dir = work_dir / "state"
            (state_dir / "ledgers").mkdir(parents=True)
            (state_dir / "action-results.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "results": [
                            {
                                "actionId": "action:current",
                                "outcome": "executed",
                            },
                            {
                                "actionId": "action:other-run",
                                "outcome": "executed",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "ledgers" / "investigation-results.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "investigationId": "investigation:complete",
                                "outcome": "recovered",
                            }
                        ),
                        json.dumps(
                            {
                                "investigationId": "investigation:other-run",
                                "outcome": "needs-evidence",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "ledgers" / "investigation-sessions.jsonl").write_text(
                json.dumps(
                    {
                        "repository": "owner/repo",
                        "investigationId": "investigation:complete",
                        "status": "completed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "ledgers" / "quarantine-sessions.jsonl").write_text(
                json.dumps(
                    {
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "batchId": "quarantine:current",
                        "status": "started",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result_path = work_dir / "agent-retrospective.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "reviewedSessionId": "session-123",
                        "summary": "The run completed without a supported finding.",
                        "observations": [],
                        "watchItems": [],
                        "successfulSafeguards": [],
                    }
                ),
                encoding="utf-8",
            )
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )
            request_path = work_dir / "retrospective-request.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "seal",
                    "--work-dir",
                    str(work_dir),
                    "--state-dir",
                    str(state_dir),
                    "--sealed-at",
                    "2026-08-29T12:30:00Z",
                    "--output",
                    str(work_dir / "run-completion.json"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare",
                    "--work-dir",
                    str(work_dir),
                    "--reviewed-session-id",
                    "session-123",
                    "--output",
                    str(request_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result_document = json.loads(result_path.read_text(encoding="utf-8"))
            result_document["evidenceDigest"] = json.loads(
                request_path.read_text(encoding="utf-8")
            )["evidenceDigest"]
            result_path.write_text(json.dumps(result_document), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "finalize",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                    "--json-output",
                    str(work_dir / "retrospective.json"),
                    "--markdown-output",
                    str(work_dir / "retrospective.md"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue(request_path.is_file())
            self.assertTrue((work_dir / "retrospective.json").is_file())
            completion = json.loads(
                (work_dir / "run-completion.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["action:current"],
                [entry["actionId"] for entry in completion["actionResults"]],
            )
            self.assertEqual(
                ["investigation:complete"],
                [
                    entry["investigationId"]
                    for entry in completion["investigationResults"]
                ],
            )
            self.assertEqual(
                ["investigation:missing"],
                completion["missingInvestigationIds"],
            )
            self.assertEqual(
                ["investigation:complete"],
                [
                    entry["investigationId"]
                    for entry in completion["investigationSessionEvents"]
                ],
            )
            self.assertEqual(
                ["action:unrecorded"],
                completion["unrecordedActionIds"],
            )
            self.assertEqual(
                ["quarantine:current"],
                [
                    entry["batchId"]
                    for entry in completion["quarantineSessionEvents"]
                ],
            )
            self.assertEqual([], completion["unrecordedQuarantineBatchIds"])
            for path in (
                work_dir / "run-completion.json",
                request_path,
                work_dir / "retrospective.json",
                work_dir / "retrospective.md",
            ):
                with self.subTest(path=path):
                    self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertIn(
                "The run completed without a supported finding.",
                (work_dir / "retrospective.md").read_text(encoding="utf-8"),
            )

    def test_finalize_rejects_symlink_output_before_writing_any_artifact(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            request = {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                "reviewedSessionId": "session-123",
                "evidencePaths": ["report.md"],
            }
            result = {
                **request,
                "summary": "The run completed.",
                "observations": [],
                "watchItems": [],
                "successfulSafeguards": [],
            }
            request.pop("summary", None)
            request_path = work_dir / "retrospective-request.json"
            result_path = work_dir / "agent-retrospective.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path.write_text(json.dumps(result), encoding="utf-8")
            outside = work_dir / "outside.md"
            outside.write_text("unchanged\n", encoding="utf-8")
            markdown_output = work_dir / "retrospective.md"
            markdown_output.symlink_to(outside)
            json_output = work_dir / "retrospective.json"
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "finalize",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                    "--json-output",
                    str(json_output),
                    "--markdown-output",
                    str(markdown_output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(json_output.exists())
            self.assertEqual("unchanged\n", outside.read_text(encoding="utf-8"))

    def test_prepare_rejects_overwriting_allowlisted_run_evidence(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            identity = {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            }
            (work_dir / "cycle.json").write_text(
                json.dumps({**identity, "stage": "completed"}),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "run-completion.json").write_text(
                json.dumps(
                    {
                        **identity,
                        "sealedAt": "2026-08-29T12:30:00Z",
                        "actionResults": [],
                        "investigationResults": [],
                        "missingInvestigationIds": [],
                        "unrecordedActionIds": [],
                    }
                ),
                encoding="utf-8",
            )
            proposals_path = work_dir / "action-proposals.json"
            proposals_path.write_text(
                json.dumps({**identity, "proposals": []}),
                encoding="utf-8",
            )
            original = proposals_path.read_text(encoding="utf-8")
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare",
                    "--work-dir",
                    str(work_dir),
                    "--reviewed-session-id",
                    "session-123",
                    "--output",
                    str(proposals_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertEqual(original, proposals_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
