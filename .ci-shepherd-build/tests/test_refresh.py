from __future__ import annotations

import copy
import unittest

from ci_shepherd.refresh import (
    RefreshError,
    RefreshPlan,
    complete_refresh_plan,
    plan_refresh,
    reconstruct_inventory,
)


REPOSITORY = "owner/repo"
COLLECTED_AT = "2026-08-18T00:00:00Z"
UPDATED_AT = "2026-08-17T00:00:00Z"


def issue_summary(number: int, *, updated_at: str = UPDATED_AT) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "title": f"Issue {number}",
        "body": "Failure details",
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": updated_at,
        "closed_at": None,
        "labels": [{"name": "ci-failure-cause"}],
        "user": {"login": "octocat"},
    }


def evidence(
    kind: str,
    payload: dict[str, object],
    *,
    availability: str = "available",
) -> dict[str, object]:
    return {
        "kind": kind,
        "url": "https://example.invalid/evidence",
        "collectedAt": COLLECTED_AT,
        "availability": availability,
        "payload": payload,
    }


def prior_snapshot() -> dict[str, object]:
    normalized_issue = {
        "number": 1,
        "state": "open",
        "title": "Issue 1",
        "body": "Failure details",
        "url": f"https://github.com/{REPOSITORY}/issues/1",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": UPDATED_AT,
        "closedAt": None,
        "labels": ["ci-failure-cause"],
        "comments": [
            {
                "id": 11,
                "url": f"https://github.com/{REPOSITORY}/issues/1#issuecomment-11",
                "createdAt": "2026-08-01T01:00:00Z",
                "updatedAt": "2026-08-01T01:00:00Z",
                "author": "octocat",
                "body": "More evidence",
            }
        ],
        "episodes": [{"openedAt": "2026-08-01T00:00:00Z", "closedAt": None}],
        "supportingIssueNumbers": [],
        "markers": [],
        "facts": [],
        "occurrences": [],
        "supportingSearch": {
            "complete": True,
            "candidateIssueNumbers": [],
            "truncated": False,
        },
    }
    issue_payload = {
        **copy.deepcopy(normalized_issue),
        "markers": [],
        "facts": [],
        "occurrences": [],
        "references": [
            {
                "sourceIssueNumber": 1,
                "sourceEvidenceId": "issue:1",
                "sourceUrl": normalized_issue["url"],
                "targetType": "workflow-run",
                "targetRepository": REPOSITORY,
                "targetUrl": f"https://github.com/{REPOSITORY}/actions/runs/99",
                "runId": 99,
                "extractionMethod": "url",
            }
        ],
    }
    referenced_by = [
        {
            "sourceIssueNumber": 1,
            "sourceEvidenceId": "issue:1",
            "sourceUrl": normalized_issue["url"],
            "extractionMethod": "url",
        }
    ]
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": COLLECTED_AT,
        "openIssues": [1],
        "issues": [normalized_issue],
        "supportingIssues": [],
        "evidence": {
            "issue:1": evidence("issue-event", issue_payload),
            "issue:1:comment:11": evidence(
                "issue-comment",
                {
                    **normalized_issue["comments"][0],
                    "sourceIssueNumber": 1,
                    "markers": [],
                    "facts": [],
                    "references": [],
                },
            ),
            "run:99": evidence(
                "workflow-run",
                {
                    "runId": 99,
                    "targetRepository": REPOSITORY,
                    "status": "completed",
                    "conclusion": "failure",
                    "workflowId": 7,
                    "branch": "main",
                    "createdAt": "2026-08-10T00:00:00Z",
                    "updatedAt": "2026-08-10T01:00:00Z",
                    "recentHistory": [],
                    "recentHistoryCollected": True,
                    "recentHistoryTruncated": False,
                    "recentHistoryTotalCount": 1,
                    "historyCoversSourceRun": True,
                    "recentHistoryGap": "",
                    "referencedBy": referenced_by,
                },
            ),
            "commit:abc": evidence(
                "commit",
                {
                    "sha": "abc",
                    "targetRepository": REPOSITORY,
                    "referencedBy": referenced_by,
                },
            ),
            "derived:1": evidence(
                "issue-event",
                {
                    "source": "derived",
                    "dependencyFingerprint": "issue:1@2026-08-17T00:00:00Z",
                    "sourceIssueNumber": 1,
                },
            ),
        },
        "collectionErrors": [],
        "warnings": [],
        "references": {"1": issue_payload["references"]},
    }


def current_history(snapshot: dict[str, object] | None = None) -> dict[str, object]:
    source = snapshot or prior_snapshot()
    history_evidence: dict[str, object] = {}
    for evidence_id, record in source["evidence"].items():
        payload = record["payload"]
        freshness = "volatile"
        if evidence_id == "issue:1" or record["kind"] == "issue-comment":
            freshness = "source-versioned"
        elif record["kind"] == "commit" or (
            record["kind"] == "workflow-job"
            and payload.get("status") == "completed"
        ) or (
            record["kind"] == "workflow-run"
            and payload.get("status") == "completed"
            and payload.get("recentHistoryCollected") is not True
            and payload.get("recentHistoryGap") in {None, "", "not-requested"}
        ):
            freshness = "immutable"
        elif payload.get("source") == "derived":
            freshness = "derived"
        history_record = copy.deepcopy(record)
        history_record["observedAt"] = record["collectedAt"]
        history_record["freshnessClass"] = freshness
        source_updated_at = payload.get("updatedAt")
        if evidence_id == "issue:1":
            source_updated_at = UPDATED_AT
        if source_updated_at:
            history_record["sourceUpdatedAt"] = source_updated_at
        history_evidence[evidence_id] = history_record
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "sourceSchemaVersions": {"snapshot": 1, "report": 1},
        "evidence": history_evidence,
    }


class RefreshPlanTests(unittest.TestCase):
    def test_refresh_plan_normalizes_all_members_to_sorted_tuples(self) -> None:
        plan = RefreshPlan(
            reuse=("z", "a", "a"),
            refresh=("run:2", "run:1"),
            retry=("issue:3",),
            retire=("issue:9", "issue:8"),
            new_issues=(5, 2, 5),
            changed_issues=(7, 4),
        )

        self.assertEqual(("a", "z"), plan.reuse)
        self.assertEqual(("run:1", "run:2"), plan.refresh)
        self.assertEqual(("issue:3",), plan.retry)
        self.assertEqual(("issue:8", "issue:9"), plan.retire)
        self.assertEqual((2, 5), plan.new_issues)
        self.assertEqual((4, 7), plan.changed_issues)
        with self.assertRaises((AttributeError, TypeError)):
            plan.reuse += ("other",)

    def test_unchanged_issue_reuses_source_records_and_refreshes_volatile_history(self) -> None:
        snapshot = prior_snapshot()

        plan = plan_refresh(
            REPOSITORY,
            [issue_summary(1)],
            snapshot,
            current_history(snapshot),
        )

        self.assertIn("issue:1", plan.reuse)
        self.assertIn("issue:1:comment:11", plan.reuse)
        self.assertIn("commit:abc", plan.reuse)
        self.assertIn("derived:1", plan.reuse)
        self.assertIn("run:99", plan.refresh)
        self.assertEqual((), plan.new_issues)
        self.assertEqual((), plan.changed_issues)

    def test_changed_new_and_retired_issues_are_planned_from_live_inventory(self) -> None:
        snapshot = prior_snapshot()

        plan = plan_refresh(
            REPOSITORY,
            [
                issue_summary(1, updated_at="2026-08-19T00:00:00Z"),
                issue_summary(2),
            ],
            snapshot,
            current_history(snapshot),
        )

        self.assertEqual((2,), plan.new_issues)
        self.assertEqual((1,), plan.changed_issues)
        self.assertIn("issue:1", plan.refresh)

        retired = plan_refresh(REPOSITORY, [], snapshot, current_history(snapshot))
        self.assertIn("issue:1", retired.retire)
        self.assertIn("issue:1:comment:11", retired.retire)

    def test_partial_records_are_retried_but_bounded_nested_gaps_are_reused(self) -> None:
        snapshot = prior_snapshot()
        snapshot["evidence"]["run:99"]["availability"] = "partial"
        snapshot["evidence"]["issue:1"]["payload"]["supportingSearch"]["truncated"] = True
        history = current_history(snapshot)

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("run:99", plan.retry)
        self.assertIn("issue:1", plan.reuse)
        self.assertNotIn("issue:1", plan.retry)

    def test_completed_run_with_bounded_history_gap_is_refreshed_not_retried(self) -> None:
        snapshot = prior_snapshot()
        run = snapshot["evidence"]["run:99"]["payload"]
        run["recentHistoryTruncated"] = True
        run["historyCoversSourceRun"] = False
        run["recentHistoryGap"] = "source-run-outside-bounded-window"
        history = current_history(snapshot)

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("run:99", plan.refresh)
        self.assertNotIn("run:99", plan.retry)

    def test_full_refresh_bypasses_all_reuse_but_preserves_issue_change_classification(self) -> None:
        snapshot = prior_snapshot()

        plan = plan_refresh(
            REPOSITORY,
            [issue_summary(1)],
            snapshot,
            current_history(snapshot),
            full_refresh=True,
        )

        self.assertEqual((), plan.reuse)
        self.assertEqual((), plan.new_issues)
        self.assertEqual((), plan.changed_issues)
        self.assertIn("issue:1", plan.refresh)
        self.assertIn("commit:abc", plan.refresh)

    def test_snapshot_repository_mismatch_raises_refresh_error(self) -> None:
        snapshot = prior_snapshot()
        snapshot["repository"] = "other/repo"

        with self.assertRaisesRegex(RefreshError, "snapshot repository"):
            plan_refresh(
                REPOSITORY,
                [issue_summary(1)],
                snapshot,
                current_history(snapshot),
            )

    def test_history_repository_mismatch_raises_refresh_error(self) -> None:
        snapshot = prior_snapshot()
        mismatched = current_history(snapshot)
        mismatched["repository"] = "other/repo"

        with self.assertRaisesRegex(RefreshError, "history repository"):
            plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, mismatched)

    def test_source_schema_mismatch_rejects_reuse(self) -> None:
        snapshot = prior_snapshot()
        wrong_schema = current_history(snapshot)
        wrong_schema["sourceSchemaVersions"]["snapshot"] = 2

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, wrong_schema)

        self.assertEqual((), plan.reuse)
        self.assertIn("issue:1", plan.refresh)

    def test_derived_record_recomputes_when_dependency_fingerprint_changes(self) -> None:
        snapshot = prior_snapshot()
        history = current_history(snapshot)
        history["evidence"]["derived:1"]["payload"]["dependencyFingerprint"] = "stale"

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("derived:1", plan.refresh)
        self.assertNotIn("derived:1", plan.reuse)

    def test_supporting_issue_chain_retires_when_its_live_root_closes(self) -> None:
        snapshot = prior_snapshot()
        snapshot["issues"][0]["supportingIssueNumbers"] = [401, 402]
        snapshot["supportingIssues"] = [
            {"number": 401, "state": "closed"},
            {"number": 402, "state": "closed"},
        ]
        snapshot["references"]["401"] = [
            {
                "sourceIssueNumber": 401,
                "sourceEvidenceId": "issue:401",
                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
                "targetType": "issue",
                "targetRepository": REPOSITORY,
                "targetUrl": f"https://github.com/{REPOSITORY}/issues/402",
                "targetNumber": 402,
                "extractionMethod": "url",
            }
        ]
        snapshot["evidence"]["issue:401"] = evidence(
            "issue-event",
            {
                "number": 401,
                "targetRepository": REPOSITORY,
                "state": "closed",
                "updatedAt": UPDATED_AT,
                "referencedBy": [
                    {
                        "sourceIssueNumber": 1,
                        "sourceEvidenceId": "issue:1",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/1",
                        "extractionMethod": "url",
                    }
                ],
            },
        )
        snapshot["evidence"]["issue:402"] = evidence(
            "issue-event",
            {
                "number": 402,
                "targetRepository": REPOSITORY,
                "state": "closed",
                "updatedAt": UPDATED_AT,
                "referencedBy": [
                    {
                        "sourceIssueNumber": 401,
                        "sourceEvidenceId": "issue:401",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
                        "extractionMethod": "url",
                    }
                ],
            },
        )
        snapshot["evidence"]["run:777"] = evidence(
            "workflow-run",
            {
                "runId": 777,
                "targetRepository": REPOSITORY,
                "status": "completed",
                "conclusion": "failure",
                "referencedBy": [
                    {
                        "sourceIssueNumber": 402,
                        "sourceEvidenceId": "issue:402",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/402",
                        "extractionMethod": "url",
                    }
                ],
            },
        )
        history = current_history(snapshot)

        plan = plan_refresh(REPOSITORY, [], snapshot, history)

        self.assertIn("issue:401", plan.retire)
        self.assertIn("issue:402", plan.retire)
        self.assertIn("run:777", plan.retire)

    def test_foreign_immutable_records_reuse_when_scope_and_history_agree(self) -> None:
        snapshot = prior_snapshot()
        referenced_by = snapshot["evidence"]["run:99"]["payload"]["referencedBy"]
        snapshot["evidence"]["commit:other/repo:abc"] = evidence(
            "commit",
            {
                "sha": "abc",
                "targetRepository": "other/repo",
                "referencedBy": referenced_by,
            },
        )
        snapshot["evidence"]["pr:other/repo:7"] = evidence(
            "pull-request",
            {
                "number": 7,
                "targetRepository": "other/repo",
                "mergedAt": "2026-08-10T00:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        history = current_history(snapshot)

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("commit:other/repo:abc", plan.reuse)
        self.assertIn("pr:other/repo:7", plan.reuse)

    def test_foreign_immutable_record_retries_when_history_scope_mismatches(self) -> None:
        snapshot = prior_snapshot()
        snapshot["evidence"]["commit:other/repo:abc"] = evidence(
            "commit",
            {
                "sha": "abc",
                "targetRepository": "other/repo",
                "referencedBy": snapshot["evidence"]["run:99"]["payload"]["referencedBy"],
            },
        )
        history = current_history(snapshot)
        history["evidence"]["commit:other/repo:abc"]["payload"]["targetRepository"] = "third/repo"

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("commit:other/repo:abc", plan.retry)

    def test_record_kind_and_url_mismatches_are_retried(self) -> None:
        for field, mismatched_value in (
            ("kind", "pull-request"),
            ("url", "https://example.invalid/different"),
        ):
            with self.subTest(field=field):
                snapshot = prior_snapshot()
                history = current_history(snapshot)
                history["evidence"]["commit:abc"][field] = mismatched_value

                plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

                self.assertIn("commit:abc", plan.retry)
                self.assertNotIn("commit:abc", plan.reuse)

    def test_payload_identity_field_mismatches_are_retried(self) -> None:
        cases = (
            ("issue:1", "number", 2),
            ("run:99", "runId", 100),
            ("commit:abc", "sha", "def"),
            ("issue:1:comment:11", "id", 12),
            ("job:55", "jobId", 56),
        )
        for evidence_id, field, mismatched_value in cases:
            with self.subTest(evidence_id=evidence_id, field=field):
                snapshot = prior_snapshot()
                if evidence_id == "job:55":
                    snapshot["evidence"][evidence_id] = evidence(
                        "workflow-job",
                        {
                            "jobId": 55,
                            "targetRepository": REPOSITORY,
                            "referencedBy": snapshot["evidence"]["run:99"]["payload"]["referencedBy"],
                        },
                    )
                history = current_history(snapshot)
                history["evidence"][evidence_id]["payload"][field] = mismatched_value

                plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

                self.assertIn(evidence_id, plan.retry)
                self.assertNotIn(evidence_id, plan.reuse)

    def test_payload_identity_must_match_evidence_id_before_reuse(self) -> None:
        referenced_by = prior_snapshot()["evidence"]["run:99"]["payload"]["referencedBy"]
        cases = (
            ("issue:1", "issue-event", {"number": 2}),
            (
                "issue:1:comment:11",
                "issue-comment",
                {"id": 12, "sourceIssueNumber": 1},
            ),
            (
                "issue:1:event:21",
                "issue-event",
                {"id": 22, "sourceIssueNumber": 1},
            ),
            (
                "run:99",
                "workflow-run",
                {"runId": 100, "targetRepository": REPOSITORY},
            ),
            (
                "run:99:attempt:2:job:55",
                "workflow-job",
                {
                    "runId": 99,
                    "attempt": 2,
                    "jobId": 56,
                    "targetRepository": REPOSITORY,
                },
            ),
            (
                "run:99:check:66:annotation:3",
                "workflow-job",
                {
                    "runId": 99,
                    "checkRunId": 66,
                    "annotationId": 4,
                    "targetRepository": REPOSITORY,
                },
            ),
            (
                "pr:other/repo:7",
                "pull-request",
                {
                    "number": 8,
                    "targetRepository": "other/repo",
                    "mergedAt": "2026-08-10T00:00:00Z",
                },
            ),
            (
                "commit:other/repo:abcdef0",
                "commit",
                {"sha": "abcdef1", "targetRepository": "other/repo"},
            ),
            (
                "commit:abcdef0",
                "commit",
                {"sha": "abcdef1", "targetRepository": REPOSITORY},
            ),
            (
                "source:src%2Fapp.py",
                "source-path",
                {"path": "src/other.py", "targetRepository": REPOSITORY},
            ),
            (
                "codeowners:src%2Fapp.py:7",
                "codeowners",
                {"path": "src/app.py", "line": 8, "targetRepository": REPOSITORY},
            ),
        )
        for evidence_id, kind, payload in cases:
            with self.subTest(evidence_id=evidence_id):
                snapshot = prior_snapshot()
                payload["referencedBy"] = copy.deepcopy(referenced_by)
                snapshot["evidence"][evidence_id] = evidence(kind, payload)
                history = current_history(snapshot)

                plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

                self.assertIn(evidence_id, plan.retry)
                self.assertNotIn(evidence_id, plan.reuse)

    def test_evidence_id_repository_scope_mismatch_is_retried(self) -> None:
        snapshot = prior_snapshot()
        snapshot["evidence"]["commit:other/repo:abc"] = evidence(
            "commit",
            {
                "sha": "abc",
                "targetRepository": "third/repo",
                "referencedBy": snapshot["evidence"]["run:99"]["payload"]["referencedBy"],
            },
        )
        history = current_history(snapshot)

        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        self.assertIn("commit:other/repo:abc", plan.retry)
        self.assertNotIn("commit:other/repo:abc", plan.reuse)

    def test_failed_refresh_is_reported_as_retry_not_refreshed(self) -> None:
        plan = RefreshPlan(refresh=("issue:401",))
        stale_carried_record = evidence(
            "issue-event",
            {"number": 401, "errorCategory": "api", "errorMessage": "unavailable"},
            availability="partial",
        )

        completed = complete_refresh_plan(plan, {"issue:401": stale_carried_record})

        self.assertNotIn("issue:401", completed.refresh)
        self.assertIn("issue:401", completed.retry)

    def test_completion_classifies_every_planned_id_once_when_reuse_is_nonempty(self) -> None:
        plan = RefreshPlan(
            reuse=("commit:kept", "commit:missing"),
            refresh=("run:done", "run:missing"),
            retry=("issue:retry",),
            retire=("issue:retire",),
        )
        final_evidence = {
            "commit:kept": evidence(
                "commit",
                {"sha": "kept", "targetRepository": REPOSITORY},
            ),
            "run:done": evidence(
                "workflow-run",
                {"runId": 1, "targetRepository": REPOSITORY},
            ),
        }

        completed = complete_refresh_plan(plan, final_evidence)

        self.assertEqual(("commit:kept",), completed.reuse)
        self.assertEqual(("run:done",), completed.refresh)
        self.assertEqual(
            ("commit:missing", "issue:retry", "run:missing"),
            completed.retry,
        )
        self.assertEqual(("issue:retire",), completed.retire)
        buckets = completed.reuse, completed.refresh, completed.retry, completed.retire
        planned_ids = set(plan.reuse + plan.refresh + plan.retry + plan.retire)
        self.assertEqual(planned_ids, set().union(*map(set, buckets)))
        self.assertEqual(sum(map(len, buckets)), len(planned_ids))


class ReconstructionTests(unittest.TestCase):
    def test_reconstructs_complete_unchanged_issue_without_report_content(self) -> None:
        snapshot = prior_snapshot()
        snapshot["previousDecisions"] = [{"reasoning": "must not leak"}]
        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, current_history(snapshot))

        inventory = reconstruct_inventory(
            REPOSITORY,
            [issue_summary(1)],
            snapshot,
            plan,
        )

        self.assertEqual(snapshot["issues"], inventory.open_issues)
        self.assertEqual(
            snapshot["evidence"]["issue:1"],
            inventory.evidence["issue:1"],
        )
        self.assertEqual(
            snapshot["evidence"]["issue:1:comment:11"],
            inventory.evidence["issue:1:comment:11"],
        )
        self.assertNotIn("previousDecisions", inventory.evidence)
        self.assertNotIn("reasoning", repr(inventory.evidence))

    def test_reconstruction_removes_retired_issue_from_current_view_only(self) -> None:
        snapshot = prior_snapshot()
        original = copy.deepcopy(snapshot)
        plan = plan_refresh(REPOSITORY, [], snapshot, current_history(snapshot))

        inventory = reconstruct_inventory(REPOSITORY, [], snapshot, plan)

        self.assertEqual([], inventory.open_issues)
        self.assertNotIn("issue:1", inventory.evidence)
        self.assertEqual(original, snapshot)

    def test_reconstruction_matches_fresh_nonempty_reference_shape_for_live_support(self) -> None:
        snapshot = prior_snapshot()
        snapshot["issues"][0]["supportingIssueNumbers"] = [401]
        snapshot["supportingIssues"] = [{"number": 401, "state": "closed"}]
        supporting_reference = {
            "sourceIssueNumber": 401,
            "sourceEvidenceId": "issue:401",
            "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
            "targetType": "workflow-run",
            "targetRepository": REPOSITORY,
            "targetUrl": f"https://github.com/{REPOSITORY}/actions/runs/777",
            "runId": 777,
            "extractionMethod": "url",
        }
        snapshot["references"]["401"] = [supporting_reference]
        snapshot["references"]["999"] = [{"sourceIssueNumber": 999}]
        snapshot["references"]["2"] = []
        snapshot["evidence"]["issue:401"] = evidence(
            "issue-event",
            {
                "number": 401,
                "targetRepository": REPOSITORY,
                "state": "closed",
                "updatedAt": UPDATED_AT,
                "referencedBy": [
                    {
                        "sourceIssueNumber": 1,
                        "sourceEvidenceId": "issue:1",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/1",
                        "extractionMethod": "url",
                    }
                ],
            },
        )
        history = current_history(snapshot)
        plan = plan_refresh(REPOSITORY, [issue_summary(1)], snapshot, history)

        inventory = reconstruct_inventory(REPOSITORY, [issue_summary(1)], snapshot, plan)

        self.assertEqual({1, 401}, set(inventory.references))
        self.assertEqual(snapshot["references"]["1"], inventory.references[1])
        self.assertEqual([supporting_reference], inventory.references[401])
        self.assertTrue(all(inventory.references.values()))


if __name__ == "__main__":
    unittest.main()
