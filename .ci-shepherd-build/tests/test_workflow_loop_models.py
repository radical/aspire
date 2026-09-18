from __future__ import annotations

from dataclasses import replace
import json
import unittest

from ci_shepherd.workflow_loop.models import (
    ActionCompletion,
    ActionKind,
    ActionState,
    ItemPhase,
    JobKey,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    RunObservation,
    TaskState,
    WorkflowKey,
    canonical_fingerprint,
    judgment_request_to_json,
    parse_judgment_request,
    parse_judgment_result,
)


def _job(
    job_id: int = 900,
    *,
    name: str = "Build / Linux",
    conclusion: str | None = "failure",
) -> JobObservation:
    return JobObservation(
        run_id=101,
        attempt=1,
        job_id=job_id,
        key=JobKey(name=name, runner_labels=("ubuntu-latest",)),
        status="completed",
        conclusion=conclusion,
        started_at="2026-09-17T20:00:00Z",
        completed_at="2026-09-17T20:01:00Z",
        url=f"https://github.com/owner/repo/actions/runs/101/job/{job_id}",
        log_excerpt="error CS1002: ; expected",
        log_truncated=False,
    )


def _run(*jobs: JobObservation) -> RunObservation:
    return RunObservation(
        key=WorkflowKey(
            repository="owner/repo",
            workflow_id=42,
            branch="main",
        ),
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        run_id=101,
        run_number=88,
        attempt=1,
        head_sha="0123456789abcdef",
        event="push",
        status="completed",
        conclusion="failure",
        created_at="2026-09-17T20:00:00Z",
        updated_at="2026-09-17T20:01:00Z",
        url="https://github.com/owner/repo/actions/runs/101",
        jobs_complete=True,
        jobs=jobs or (_job(),),
    )


def _request(
    *,
    decision_context: str = "initial",
    failed_jobs: tuple[JobObservation, ...] | None = None,
) -> JudgmentRequest:
    jobs = failed_jobs or (_job(),)
    follow_up = decision_context == "follow-up"
    return JudgmentRequest(
        worker_id="worker-1",
        session_id="session-1",
        item_id=7,
        episode=2,
        evidence_fingerprint="fnv1a64:0123456789abcdef",
        round=1 if follow_up else 0,
        repository="owner/repo",
        branch="main",
        workflow_id=42,
        workflow_path=".github/workflows/ci.yml",
        failure_run=_run(*jobs),
        failed_jobs=jobs,
        evidence_ids=("run:101", "job:101:900", "log:900"),
        issue_number=17,
        task_id="task-owned-123" if follow_up else None,
        pull_request_number=23 if follow_up else None,
        pull_request_head_sha="fedcba9876543210" if follow_up else None,
        pull_request_head_ref="copilot/fix-build" if follow_up else None,
        pull_request_base_ref="main" if follow_up else None,
        pull_request_observed_at=(
            "2026-09-17T20:02:00Z" if follow_up else None
        ),
        followup_count=1 if follow_up else 0,
        prompt="Classify the workflow failure.",
    )


def _result_document(**overrides: object) -> str:
    document: dict[str, object] = {
        "schemaVersion": 1,
        "itemId": 7,
        "episode": 2,
        "evidenceFingerprint": "fnv1a64:0123456789abcdef",
        "decision": "assign",
        "summary": (
            "The failed job contains a compiler diagnostic before tests start."
        ),
        "evidenceIds": ["run:101", "job:101:900", "log:900"],
        "inScopeJobIds": [900],
        "copilotRequest": (
            "Fix the compiler failure and add focused regression coverage."
        ),
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":"))


class WorkflowLoopModelTests(unittest.TestCase):
    def test_canonical_fingerprint_matches_existing_fnv1a_encoding(self) -> None:
        self.assertEqual(
            "fnv1a64:8f00364b3055ed35",
            canonical_fingerprint({"b": 2, "a": [True, None, "x"]}),
        )

    def test_positive_ids_reject_bool(self) -> None:
        with self.assertRaisesRegex(ValueError, "workflow_id"):
            WorkflowKey(repository="owner/repo", workflow_id=True, branch="main")

    def test_timestamps_require_aware_utc_rfc3339_z(self) -> None:
        for timestamp in (
            "2026-09-17T20:00:00",
            "2026-09-17T20:00:00+00:00",
            "2026-09-17T16:00:00-04:00",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(ValueError, "created_at"):
                    replace(_run(), created_at=timestamp)

    def test_request_round_trips_all_typed_identity(self) -> None:
        request = _request(decision_context="follow-up")

        reparsed = parse_judgment_request(judgment_request_to_json(request))

        self.assertEqual(request, reparsed)
        self.assertEqual(23, reparsed.pull_request_number)
        self.assertEqual("task-owned-123", reparsed.task_id)
        self.assertEqual("fedcba9876543210", reparsed.pull_request_head_sha)
        self.assertEqual("copilot/fix-build", reparsed.pull_request_head_ref)
        self.assertEqual("main", reparsed.pull_request_base_ref)
        self.assertEqual(
            "2026-09-17T20:02:00Z",
            reparsed.pull_request_observed_at,
        )
        self.assertIs(ItemPhase.COPILOT_ACTIVE, ItemPhase("copilot_active"))
        self.assertIs(ActionKind.FOLLOW_UP, ActionKind("follow_up"))
        self.assertIs(JudgmentDecision.ASSIGN, JudgmentDecision("assign"))

    def test_request_round_rejects_bool_and_negative_values(self) -> None:
        for value in (True, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "round"):
                    replace(_request(), round=value)

    def test_parse_judgment_result_accepts_exact_valid_document(self) -> None:
        request = _request()

        reparsed = parse_judgment_request(judgment_request_to_json(request))
        result = parse_judgment_result(_result_document(), reparsed)

        self.assertEqual(0, reparsed.round)
        self.assertEqual(JudgmentDecision.ASSIGN, result.decision)
        self.assertEqual((900,), result.in_scope_job_ids)

    def test_parse_judgment_result_rejects_invalid_documents_table(self) -> None:
        successful = _job(901, conclusion="success")
        request_with_success = _request(failed_jobs=(_job(), successful))
        cases = {
            "fenced JSON": ("```json\n" + _result_document() + "\n```", _request()),
            "trailing prose": (_result_document() + "\nDone.", _request()),
            "unknown field": (_result_document(extra=True), _request()),
            "missing field": (
                json.dumps(
                    {
                        key: value
                        for key, value in json.loads(
                            _result_document()
                        ).items()
                        if key != "summary"
                    }
                ),
                _request(),
            ),
            "invalid enum": (
                _result_document(decision="repair_everything"),
                _request(),
            ),
            "float schema version": (
                _result_document(schemaVersion=1.0),
                _request(),
            ),
            "bool item id": (_result_document(itemId=True), _request()),
            "duplicate key": (
                _result_document()[:-1] + ',"summary":"duplicate"}',
                _request(),
            ),
            "mismatched item": (_result_document(itemId=8), _request()),
            "mismatched episode": (_result_document(episode=3), _request()),
            "mismatched fingerprint": (
                _result_document(
                    evidenceFingerprint="fnv1a64:ffffffffffffffff"
                ),
                _request(),
            ),
            "foreign evidence": (
                _result_document(evidenceIds=["run:999"]),
                _request(),
            ),
            "duplicate evidence": (
                _result_document(evidenceIds=["run:101", "run:101"]),
                _request(),
            ),
            "foreign job": (
                _result_document(inScopeJobIds=[999]),
                _request(),
            ),
            "successful job": (
                _result_document(inScopeJobIds=[901]),
                request_with_success,
            ),
            "duplicate job": (
                _result_document(inScopeJobIds=[900, 900]),
                _request(),
            ),
            "empty summary": (_result_document(summary=" "), _request()),
            "assign without jobs": (
                _result_document(inScopeJobIds=[]),
                _request(),
            ),
            "assign without request": (
                _result_document(copilotRequest=None),
                _request(),
            ),
            "defer with jobs": (
                _result_document(
                    decision="defer_ordinary_test",
                    inScopeJobIds=[900],
                    copilotRequest=None,
                ),
                _request(),
            ),
        }
        for name, (text, request) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    parse_judgment_result(text, request)

    def test_follow_up_requires_fresh_tracked_pull_request_below_limit(self) -> None:
        valid_request = _request(decision_context="follow-up")
        text = _result_document(decision="follow_up")

        result = parse_judgment_result(text, valid_request)

        self.assertEqual(JudgmentDecision.FOLLOW_UP, result.decision)
        invalid_requests = (
            replace(
                valid_request,
                task_id=None,
                pull_request_number=None,
                pull_request_head_sha=None,
                pull_request_head_ref=None,
                pull_request_base_ref=None,
                pull_request_observed_at=None,
            ),
            replace(valid_request, followup_count=2),
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    parse_judgment_result(text, request)

        with self.assertRaisesRegex(ValueError, "Initial assign"):
            parse_judgment_result(
                _result_document(),
                valid_request,
            )

    def test_non_action_decisions_reject_copilot_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "copilotRequest"):
            parse_judgment_result(
                _result_document(
                    decision="needs_attention",
                    inScopeJobIds=[],
                ),
                _request(),
            )

    def test_action_completion_retains_opaque_remote_task_id(self) -> None:
        completion = ActionCompletion(
            action_id="action-1",
            state=ActionState.CONFIRMED,
            completed_at="2026-09-17T20:03:00Z",
            remote_number=None,
            remote_task_id="01J8TASKopaque-value",
            error=None,
        )

        self.assertEqual("01J8TASKopaque-value", completion.remote_task_id)
        self.assertIs(TaskState.IN_PROGRESS, TaskState("in_progress"))


if __name__ == "__main__":
    unittest.main()
