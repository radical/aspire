from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Mapping

from ci_shepherd.lifecycle import candidate_for, prepare_assessment


COLLECTED_AT = "2026-08-19T16:00:00Z"
REPOSITORY = "microsoft/aspire"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


class MappingSubclass(Mapping):
    """A Mapping that is not a ``dict``, as produced by wrapper evidence readers."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def evidence(
    evidence_id: str,
    kind: str,
    payload: dict[str, object],
    *,
    availability: str = "available",
) -> tuple[str, dict[str, object]]:
    return (
        evidence_id,
        {
            "kind": kind,
            "url": f"https://github.com/{REPOSITORY}/issues/1",
            "collectedAt": COLLECTED_AT,
            "availability": availability,
            "payload": payload,
        },
    )


def issue_payload(
    number: int,
    *,
    producer: str,
    autoclose: bool | None,
    ledger: dict[str, object],
    updated_at: str = "2026-08-10T00:00:00Z",
    facts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "title": f"Issue {number}",
        "url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": updated_at,
        "closedAt": None,
        "labels": ["ci-failure-cause"],
        "producer": producer,
        "autoclose": autoclose,
        "ledger": ledger,
        "episodes": [{"openedAt": "2026-08-01T00:00:00Z", "closedAt": None}],
        "episodesComplete": False,
        "facts": facts or [],
    }


def complete_ledger(*run_ids: int) -> dict[str, object]:
    return {
        "source": "body-table",
        "schema": "occurrences-v1",
        "schemaRecognized": True,
        "sourceRecordCount": len(run_ids),
        "parsedRowCount": len(run_ids),
        "complete": bool(run_ids),
        "rows": [
            {
                "date": f"2026-08-{index + 1:02d}",
                "sourceRun": run_id,
                "runUrl": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                "job": "Tests",
                "pullRequest": 1,
            }
            for index, run_id in enumerate(run_ids)
        ],
    }


def snapshot(
    payload: dict[str, object],
    *extra_evidence: tuple[str, dict[str, object]],
) -> dict[str, object]:
    number = int(payload["number"])
    issue_record = evidence(f"issue:{number}", "issue-event", payload)
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": COLLECTED_AT,
        "openIssues": [number],
        "issues": [payload],
        "supportingIssues": [],
        "evidence": dict((issue_record, *extra_evidence)),
        "collectionErrors": [],
        "warnings": [],
        "references": {},
    }


class LifecycleAssessmentTests(unittest.TestCase):
    def test_prepared_issue_evidence_preserves_bot_author(self) -> None:
        payload = issue_payload(
            1,
            producer="unknown",
            autoclose=None,
            ledger=complete_ledger(),
        )
        payload["author"] = "github-actions[bot]"

        prepared = prepare_assessment(snapshot(payload), max_bundle_records=25)

        issue_evidence = prepared["issues"][0]["evidenceBundle"][0]
        self.assertEqual("github-actions[bot]", issue_evidence["payload"]["author"])

    def test_raw_collector_branch_field_is_projected_as_head_branch(self) -> None:
        # Regression: the raw collector always emits "branch" (never
        # "headBranch") for workflow-run payloads and their recentHistory
        # entries. Prepared payloads -- and downstream recovery logic in
        # poc.py -- read "headBranch" everywhere, so prepare_assessment must
        # canonicalize branch -> headBranch at this boundary without
        # redesigning the raw collector and without dropping the raw field's
        # value.
        issue_number = 18
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(500),
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        run = evidence(
            "run:500",
            "workflow-run",
            {
                "runId": 500,
                "conclusion": "success",
                "branch": "main",
                "createdAt": "2026-08-19T10:00:00Z",
                "referencedBy": referenced_by,
                "recentHistory": [
                    {
                        "runId": 499,
                        "conclusion": "failure",
                        "createdAt": "2026-08-18T10:00:00Z",
                        "branch": "main",
                    }
                ],
            },
        )

        prepared = prepare_assessment(snapshot(payload, run))

        candidate = candidate_for(prepared, issue_number)
        run_evidence = next(
            entry for entry in candidate["evidenceBundle"] if entry["id"] == "run:500"
        )
        self.assertEqual("main", run_evidence["payload"]["headBranch"])
        self.assertNotIn("branch", run_evidence["payload"])
        history_entry = run_evidence["payload"]["recentHistory"][0]
        self.assertEqual("main", history_entry["headBranch"])
        self.assertNotIn("branch", history_entry)

    def test_existing_head_branch_is_preserved_over_raw_branch(self) -> None:
        # An already-prepared-shaped payload that carries "headBranch" (for
        # example, from a test fixture built directly against the prepared
        # shape) must not be overwritten by a stray "branch" field.
        issue_number = 19
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(600),
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        run = evidence(
            "run:600",
            "workflow-run",
            {
                "runId": 600,
                "conclusion": "success",
                "branch": "some-other-branch",
                "headBranch": "main",
                "createdAt": "2026-08-19T10:00:00Z",
                "referencedBy": referenced_by,
            },
        )

        prepared = prepare_assessment(snapshot(payload, run))

        candidate = candidate_for(prepared, issue_number)
        run_evidence = next(
            entry for entry in candidate["evidenceBundle"] if entry["id"] == "run:600"
        )
        self.assertEqual("main", run_evidence["payload"]["headBranch"])

    def test_unknown_producer_is_a_data_quality_blocker(self) -> None:
        prepared = prepare_assessment(
            snapshot(
                issue_payload(
                    10,
                    producer="unknown",
                    autoclose=None,
                    ledger={
                        "source": "none",
                        "schema": None,
                        "schemaRecognized": False,
                        "sourceRecordCount": 0,
                        "parsedRowCount": 0,
                        "complete": False,
                        "rows": [],
                    },
                )
            )
        )

        candidate = candidate_for(prepared, 10)
        self.assertEqual("insufficient-evidence", candidate["candidateState"])
        self.assertEqual("investigate", candidate["candidateAction"])
        self.assertIn("unknown-producer", candidate["blockers"])
        self.assertFalse(candidate["automationEligible"])

    def test_autoclose_tracker_waits_for_existing_watchdog(self) -> None:
        payload = issue_payload(
            11,
            producer="tracking-issue",
            autoclose=True,
            ledger={
                "source": "run-comments",
                "schema": "tracking-comments-v1",
                "schemaRecognized": True,
                "sourceRecordCount": 2,
                "parsedRowCount": 2,
                "complete": True,
                "rows": [
                    {"commentId": 1, "createdAt": "2026-08-01T00:00:00Z", "runId": 100},
                    {"commentId": 2, "createdAt": "2026-08-02T00:00:00Z", "runId": 101},
                ],
            },
        )
        payload["labels"] = ["automation-broken"]

        candidate = candidate_for(prepare_assessment(snapshot(payload)), 11)

        self.assertEqual("observing", candidate["candidateState"])
        self.assertEqual("wait", candidate["candidateAction"])
        self.assertEqual(["wait"], candidate["allowedActions"])
        self.assertIn("existing-watchdog-owns-closure", candidate["blockers"])

    def test_recurrent_cause_is_actionable(self) -> None:
        payload = issue_payload(
            12,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100, 101),
            facts=[{"field": "causeId", "normalized": "timeout"}],
        )

        candidate = candidate_for(prepare_assessment(snapshot(payload)), 12)

        self.assertEqual("actionable", candidate["candidateState"])
        self.assertEqual("investigate", candidate["candidateAction"])
        self.assertEqual("timeout", candidate["identity"]["tier1CauseId"])

    def test_commit_anchored_recovery_is_advisory_close_candidate(self) -> None:
        issue_number = 13
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-09T00:00:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        pr = evidence(
            "pr:20",
            "pull-request",
            {
                "number": 20,
                "state": "closed",
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "a" * 40,
                "referencedBy": referenced_by,
            },
        )
        run = evidence(
            "run:200",
            "workflow-run",
            {
                "runId": 200,
                "conclusion": "success",
                "headSha": "a" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        candidate = candidate_for(prepare_assessment(snapshot(payload, pr, run)), issue_number)

        self.assertEqual("resolved", candidate["candidateState"])
        self.assertEqual("recommend-close", candidate["candidateAction"])
        self.assertTrue(candidate["approvalRequired"])
        self.assertFalse(candidate["automationEligible"])
        self.assertIn("recommend-close", candidate["allowedActions"])
        self.assertIn("autoclose-policy-does-not-permit-shepherd", candidate["blockers"])

    def test_commit_anchored_recovery_uses_run_time_for_same_day_occurrence(self) -> None:
        issue_number = 16
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-10T09:30:00Z",
        )
        payload["ledger"]["rows"][0]["date"] = "2026-08-10"
        referenced_by = [{"sourceIssueNumber": issue_number}]
        occurrence_run = evidence(
            "run:100",
            "workflow-run",
            {
                "runId": 100,
                "conclusion": "failure",
                "headSha": "c" * 40,
                "runStartedAt": "2026-08-10T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        pr = evidence(
            "pr:22",
            "pull-request",
            {
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "d" * 40,
                "referencedBy": referenced_by,
            },
        )
        recovery_run = evidence(
            "run:202",
            "workflow-run",
            {
                "runId": 202,
                "conclusion": "success",
                "headSha": "d" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        incomplete_candidate = candidate_for(
            prepare_assessment(snapshot(payload, pr, recovery_run)),
            issue_number,
        )

        self.assertEqual("wait", incomplete_candidate["candidateAction"])
        self.assertIn(
            "occurrence-run-timestamp-for-fix-day",
            incomplete_candidate["missingPrerequisites"],
        )

        candidate = candidate_for(
            prepare_assessment(snapshot(payload, occurrence_run, pr, recovery_run)),
            issue_number,
        )

        self.assertEqual("resolved", candidate["candidateState"])
        self.assertEqual("recommend-close", candidate["candidateAction"])
        self.assertEqual(
            "2026-08-10T09:00:00Z",
            candidate["resolutionEvidence"]["latestOccurrence"],
        )

    def test_commit_anchored_recovery_compares_successful_run_offsets_by_instant(self) -> None:
        issue_number = 17
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-10T09:30:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        occurrence_run = evidence(
            "run:100",
            "workflow-run",
            {
                "runId": 100,
                "conclusion": "failure",
                "headSha": "c" * 40,
                "runStartedAt": "2026-08-10T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        pr = evidence(
            "pr:23",
            "pull-request",
            {
                "mergedAt": "2026-08-10T12:00:00+02:00",
                "mergeCommitSha": "e" * 40,
                "referencedBy": referenced_by,
            },
        )
        recovery_run = evidence(
            "run:203",
            "workflow-run",
            {
                "runId": 203,
                "conclusion": "success",
                "headSha": "e" * 40,
                "runStartedAt": "2026-08-10T06:01:00-04:00",
                "referencedBy": referenced_by,
            },
        )

        candidate = candidate_for(
            prepare_assessment(snapshot(payload, occurrence_run, pr, recovery_run)),
            issue_number,
        )

        self.assertEqual("resolved", candidate["candidateState"])
        self.assertEqual("recommend-close", candidate["candidateAction"])

    def test_commit_anchored_recovery_rejects_later_occurrence_with_earlier_offset_text(self) -> None:
        issue_number = 18
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-10T09:30:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        occurrence_run = evidence(
            "run:100",
            "workflow-run",
            {
                "runId": 100,
                "conclusion": "failure",
                "headSha": "c" * 40,
                "runStartedAt": "2026-08-10T07:30:00-03:00",
                "referencedBy": referenced_by,
            },
        )
        pr = evidence(
            "pr:24",
            "pull-request",
            {
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "f" * 40,
                "referencedBy": referenced_by,
            },
        )
        recovery_run = evidence(
            "run:204",
            "workflow-run",
            {
                "runId": 204,
                "conclusion": "success",
                "headSha": "f" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        candidate = candidate_for(
            prepare_assessment(snapshot(payload, occurrence_run, pr, recovery_run)),
            issue_number,
        )

        self.assertEqual("observing", candidate["candidateState"])
        self.assertEqual("wait", candidate["candidateAction"])

    def test_commit_anchored_recovery_reports_latest_occurrence_as_utc_z(self) -> None:
        issue_number = 19
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-10T09:30:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        occurrence_run = evidence(
            "run:100",
            "workflow-run",
            {
                "runId": 100,
                "conclusion": "failure",
                "headSha": "c" * 40,
                "runStartedAt": "2026-08-10T06:00:00-03:00",
                "referencedBy": referenced_by,
            },
        )
        pr = evidence(
            "pr:25",
            "pull-request",
            {
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "a" * 40,
                "referencedBy": referenced_by,
            },
        )
        recovery_run = evidence(
            "run:205",
            "workflow-run",
            {
                "runId": 205,
                "conclusion": "success",
                "headSha": "a" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        candidate = candidate_for(
            prepare_assessment(snapshot(payload, occurrence_run, pr, recovery_run)),
            issue_number,
        )

        self.assertEqual("2026-08-10T09:00:00Z", candidate["resolutionEvidence"]["latestOccurrence"])

    def test_commit_anchored_recovery_rejects_naive_timestamps(self) -> None:
        issue_number = 20
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-10T09:30:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        occurrence_run = evidence(
            "run:100",
            "workflow-run",
            {
                "runId": 100,
                "conclusion": "failure",
                "headSha": "c" * 40,
                "runStartedAt": "2026-08-10T09:00:00",
                "referencedBy": referenced_by,
            },
        )
        pr = evidence(
            "pr:26",
            "pull-request",
            {
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "a" * 40,
                "referencedBy": referenced_by,
            },
        )
        recovery_run = evidence(
            "run:206",
            "workflow-run",
            {
                "runId": 206,
                "conclusion": "success",
                "headSha": "a" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        with self.assertRaisesRegex(ValueError, "latest occurrence"):
            prepare_assessment(snapshot(payload, occurrence_run, pr, recovery_run))

    def test_issue_update_after_fix_adds_parser_disagreement_blocker(self) -> None:
        issue_number = 14
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            updated_at="2026-08-11T00:00:00Z",
        )
        referenced_by = [{"sourceIssueNumber": issue_number}]
        pr = evidence(
            "pr:21",
            "pull-request",
            {
                "mergedAt": "2026-08-10T10:00:00Z",
                "mergeCommitSha": "b" * 40,
                "referencedBy": referenced_by,
            },
        )
        run = evidence(
            "run:201",
            "workflow-run",
            {
                "conclusion": "success",
                "headSha": "b" * 40,
                "runStartedAt": "2026-08-10T10:01:00Z",
                "referencedBy": referenced_by,
            },
        )

        candidate = candidate_for(prepare_assessment(snapshot(payload, pr, run)), issue_number)

        self.assertEqual("needs-human", candidate["candidateState"])
        self.assertEqual("recommend-close", candidate["candidateAction"])
        self.assertIn("issue-updated-after-fix-without-ledger-row", candidate["blockers"])

    def test_bundle_is_bounded_after_all_scoped_evidence_is_scanned(self) -> None:
        issue_number = 15
        payload = issue_payload(
            issue_number,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100, 101),
        )
        extras = tuple(
            evidence(
                f"source:tests%2FFile{index}.cs",
                "source-path",
                {
                    "path": f"tests/File{index}.cs",
                    "sourceIssueNumber": issue_number,
                },
            )
            for index in range(178)
        )

        candidate = candidate_for(
            prepare_assessment(
                snapshot(payload, *extras),
                max_bundle_records=25,
            ),
            issue_number,
        )

        self.assertLessEqual(len(candidate["evidenceBundle"]), 25)
        self.assertEqual(179, candidate["completenessProof"]["scopedRecordCount"])
        self.assertEqual(154, candidate["completenessProof"]["excludedRecordCount"])
        self.assertTrue(candidate["completenessProof"]["allScopedEvidenceScanned"])

    def test_bundle_projects_records_instead_of_copying_raw_payloads(self) -> None:
        payload = issue_payload(
            16,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
            facts=[{"field": "causeId", "normalized": "timeout"}],
        )
        payload["body"] = "large body " * 2_000
        payload["comments"] = [{"body": "large comment " * 2_000}]
        comment = evidence(
            "issue:16:comment:1",
            "issue-comment",
            {
                "id": 1,
                "sourceIssueNumber": 16,
                "createdAt": "2026-08-01T00:00:00Z",
                "body": "diagnostic " * 1_000,
                "facts": [{"field": "exceptionType", "normalized": "TimeoutException"}],
            },
        )

        candidate = candidate_for(prepare_assessment(snapshot(payload, comment)), 16)
        issue_bundle_payload = candidate["evidenceBundle"][0]["payload"]
        comment_bundle_payload = candidate["evidenceBundle"][1]["payload"]

        self.assertNotIn("body", issue_bundle_payload)
        self.assertNotIn("comments", issue_bundle_payload)
        self.assertEqual("timeout", issue_bundle_payload["facts"][0]["normalized"])
        self.assertLessEqual(len(comment_bundle_payload["body"]), 2_000)

    def test_scoped_evidence_with_a_mapping_payload_is_bundled(self) -> None:
        payload = issue_payload(
            17,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(100),
        )
        comment = evidence(
            "issue:17:comment:1",
            "issue-comment",
            MappingSubclass(
                {
                    "id": 1,
                    "createdAt": "2026-08-01T00:00:00Z",
                    "body": "diagnostic",
                    "referencedBy": [MappingSubclass({"sourceIssueNumber": 17})],
                }
            ),
        )

        candidate = candidate_for(prepare_assessment(snapshot(payload, comment)), 17)

        self.assertEqual(
            ["issue:17", "issue:17:comment:1"],
            [record["id"] for record in candidate["evidenceBundle"]],
        )


class AnnotationBundleTests(unittest.TestCase):
    ANNOTATION_ID = "run:7001:check:8001:annotation:9001"
    JOB_ID = "run:7001:attempt:1:job:2001"

    def _fixture_snapshot(self, **extra: object) -> dict[str, object]:
        # Captured verbatim from Collector.enrich_github_evidence: the collector
        # files check-run annotations under the "workflow-job" kind.
        fixture = json.loads(
            (FIXTURE_ROOT / "collector-annotation-evidence.json").read_text(encoding="utf-8")
        )
        payload = issue_payload(
            11,
            producer="ci-failure-cause",
            autoclose=None,
            ledger=complete_ledger(7001),
        )
        return snapshot(payload, *fixture["evidence"].items(), *extra.items())

    def test_check_run_annotations_are_bundled_under_their_own_kind(self) -> None:
        candidate = candidate_for(prepare_assessment(self._fixture_snapshot()), 11)
        kinds_by_id = {record["id"]: record["kind"] for record in candidate["evidenceBundle"]}

        self.assertEqual("workflow-annotation", kinds_by_id[self.ANNOTATION_ID])
        self.assertEqual(
            [self.JOB_ID],
            sorted(record_id for record_id, kind in kinds_by_id.items() if kind == "workflow-job"),
        )

    def test_bundled_annotation_retains_its_failure_text_and_location(self) -> None:
        candidate = candidate_for(prepare_assessment(self._fixture_snapshot()), 11)
        annotation = next(
            record for record in candidate["evidenceBundle"] if record["id"] == self.ANNOTATION_ID
        )

        self.assertEqual(
            {
                "annotationId": 9001,
                "checkRunId": 8001,
                "runId": 7001,
                "attempt": 1,
                "jobId": 2001,
                "path": "tests/Alpha.Tests/SampleTests.cs",
                "startLine": 42,
                "endLine": 42,
                "level": "failure",
                "title": "Alpha.Tests.SampleTests.FailingTest",
                "message": "Assert.Equal() Failure: Values differ",
                "targetRepository": "owner/repo",
                "referencedBy": annotation["payload"].get("referencedBy"),
            },
            annotation["payload"],
        )

    def test_annotation_level_and_raw_details_aliases_are_projected(self) -> None:
        # GitHub's check-run annotation API names these `annotation_level` and
        # `raw_details`; the collector normalizes to `level`/`message` but an
        # untransformed record must not silently lose them.
        snapshot_with_aliases = self._fixture_snapshot()
        aliased_id = "run:7001:check:8001:annotation:9002"
        snapshot_with_aliases["evidence"][aliased_id] = {
            "kind": "workflow-job",
            "url": "https://github.com/owner/repo/actions/runs/7001/job/2001",
            "collectedAt": COLLECTED_AT,
            "availability": "available",
            "payload": {
                "annotationId": 9002,
                "checkRunId": 8001,
                "runId": 7001,
                "annotationLevel": "warning",
                "rawDetails": "  at Alpha.Tests.SampleTests.FailingTest()",
                "sourceIssueNumber": 11,
            },
        }

        candidate = candidate_for(prepare_assessment(snapshot_with_aliases), 11)
        aliased = next(
            record for record in candidate["evidenceBundle"] if record["id"] == aliased_id
        )

        self.assertEqual("workflow-annotation", aliased["kind"])
        self.assertEqual("warning", aliased["payload"]["annotationLevel"])
        self.assertEqual(
            "  at Alpha.Tests.SampleTests.FailingTest()",
            aliased["payload"]["rawDetails"],
        )

    def test_annotations_are_evicted_before_jobs_logs_and_source_paths(self) -> None:
        extras = {
            f"run:7001:check:8001:annotation:{9100 + index}": {
                "kind": "workflow-job",
                "url": "https://github.com/owner/repo/actions/runs/7001/job/2001",
                "collectedAt": COLLECTED_AT,
                "availability": "available",
                "payload": {
                    "annotationId": 9100 + index,
                    "checkRunId": 8001,
                    "runId": 7001,
                    "level": "warning",
                    "message": f"Node.js deprecation notice {index}",
                    "sourceIssueNumber": 11,
                },
            }
            for index in range(20)
        }
        extras["run:7001:attempt:1:job:2001:log"] = {
            "kind": "workflow-log",
            "url": "https://github.com/owner/repo/actions/runs/7001/job/2001",
            "collectedAt": COLLECTED_AT,
            "availability": "available",
            "payload": {
                "evidenceId": "run:7001:attempt:1:job:2001:log",
                "runId": 7001,
                "jobId": 2001,
                "errorCategory": "test-failure",
                "sourceIssueNumber": 11,
            },
        }
        extras["source:tests%2FSampleTests.cs"] = {
            "kind": "source-path",
            "url": "https://github.com/owner/repo/blob/main/tests/Alpha.Tests/SampleTests.cs",
            "collectedAt": COLLECTED_AT,
            "availability": "available",
            "payload": {
                "path": "tests/Alpha.Tests/SampleTests.cs",
                "exists": True,
                "sourceIssueNumber": 11,
            },
        }

        candidate = candidate_for(
            prepare_assessment(self._fixture_snapshot(**extras), max_bundle_records=6),
            11,
        )
        bundled = [record["id"] for record in candidate["evidenceBundle"]]

        self.assertEqual(
            [
                "issue:11",
                "run:7001",
                "run:7001:attempt:1:job:2001",
                "run:7001:attempt:1:job:2001:log",
                "source:tests%2FSampleTests.cs",
            ],
            sorted(record_id for record_id in bundled if ":annotation:" not in record_id),
        )
        # 21 annotations are scoped and the cap leaves room for exactly one, so the
        # other 20 are the only records evicted: every job, log, and source path
        # outranks them.
        self.assertEqual(
            {"workflow-annotation": 20},
            candidate["completenessProof"]["excludedCountsByKind"],
        )


if __name__ == "__main__":
    unittest.main()
