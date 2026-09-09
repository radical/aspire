from __future__ import annotations

import copy
from datetime import UTC, datetime
import unittest

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import build_dry_run, validate_action_proposals
from ci_shepherd.collector import Collector
from ci_shepherd.comment_selection import build_comment_selection
from tests.test_actions import (
    _judgments,
    _ping_human_judgments,
    _prepared,
    _snapshot,
    _with_owned_comment,
)
from tests.test_quarantine import _repository_policy_identity
from tests.test_collector import ScriptedClient


def _inputs(target: str = "VS Code extension E2E (Linux, azure-functions)") -> tuple[dict, dict, dict]:
    snapshot = _snapshot()
    prepared = _prepared()
    policy = _repository_policy_identity("owner/repo")
    prepared.update(repositoryPolicy=policy, repositoryPolicyDigest=policy["digest"])
    judgments = _judgments()
    judgments["issues"][0]["category"] = "flaky-test"
    judgments["issues"][0]["recommendations"][0].update(
        disposition="review-quarantine",
        target={"kind": "test", "value": target},
    )
    return snapshot, prepared, judgments


class QuarantineBlockerCommentTests(unittest.TestCase):
    def test_unsupported_target_produces_an_eligible_canonical_status_comment(self) -> None:
        snapshot, prepared, judgments = _inputs()

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("create-comment", proposal["operation"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])
        self.assertEqual(["issue:21"], proposal["evidenceIds"])
        self.assertEqual(
            f"{prepared['snapshotId']}:issue:21:quarantine-blocked-comment",
            proposal["actionId"],
        )
        self.assertEqual(
            "[automated] Automatic quarantine is blocked for the reported target(s).\n"
            "\n"
            "The current quarantine path expects a .NET method identifier such as "
            "`Namespace.Type.Method`. These reported targets do not use that format:\n"
            "\n"
            "```text\n"
            "VS Code extension E2E (Linux, azure-functions)\n"
            "```\n"
            "\n"
            "This is a target-format limitation, not evidence that the tests do not exist. "
            "A framework-specific test identity and quarantine path are needed for "
            "targets that are not .NET methods.\n"
            "\n"
            "No quarantine change was made by this recommendation.\n"
            "\n"
            "**Source issue:**\n"
            "- [issue:21](https://github.com/owner/repo/issues/21)\n"
            "\n"
            "<!-- ci-shepherd:role=status -->\n"
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->",
            proposal["body"],
        )
        self.assertTrue(proposal["executionEligibility"]["eligible"])
        validate_action_proposals(result)
        self.assertEqual(
            [proposal["actionId"]],
            build_comment_selection(result, max_comments=5)["selectedActionIds"],
        )

    def test_unchanged_blocker_does_not_repost_or_edit(self) -> None:
        snapshot, prepared, judgments = _inputs()
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_changed_blocker_updates_the_existing_status_comment(self) -> None:
        snapshot, prepared, judgments = _inputs()
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = "VS Code E2E (deno)"

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])
        self.assertIn("sourceCommentFingerprint", proposal)
        self.assertIn("```text\nVS Code E2E (deno)\n```", proposal["body"])

    def test_multiple_unsupported_targets_share_one_comment(self) -> None:
        snapshot, prepared, judgments = _inputs("Scenario Z")
        second = copy.deepcopy(judgments["issues"][0]["recommendations"][0])
        second["target"]["value"] = "Scenario A"
        judgments["issues"][0]["recommendations"].append(second)

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(1, len(result["proposals"]))
        self.assertIn("```text\nScenario A\nScenario Z\n```", result["proposals"][0]["body"])

    def test_resolved_target_format_retires_the_old_blocker_without_claiming_quarantine(self) -> None:
        snapshot, prepared, judgments = _inputs()
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = "Namespace.Type.Method"

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        proposal, = result["proposals"]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])
        self.assertEqual(
            "[automated] The quarantine target-format blocker no longer applies.\n"
            "\n"
            "The current quarantine recommendation names supported .NET method identifiers:\n"
            "\n"
            "```text\nNamespace.Type.Method\n```\n"
            "\n"
            "Matching the identifier format does not prove that the tests exist, that the "
            "failure evidence is sufficient, or that quarantine is approved. "
            "Source, evidence, and authorization checks still apply.\n"
            "\n"
            "**Source issue:**\n"
            "- [issue:21](https://github.com/owner/repo/issues/21)\n"
            "\n"
            "<!-- ci-shepherd:role=status -->\n"
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->",
            proposal["body"],
        )
        snapshot = _with_owned_comment(_snapshot(), proposal["body"])
        repeated = build_action_proposals(snapshot, prepared, judgments, "ankj")
        self.assertEqual([], repeated["proposals"])
        self.assertEqual([21], repeated["unchangedIssueNumbers"])

    def test_one_rejected_target_keeps_each_originating_issue(self) -> None:
        snapshot, prepared, judgments = _inputs()
        second = copy.deepcopy(judgments["issues"][0])
        second["issueNumber"] = 22
        second["recommendations"][0]["evidenceIds"] = ["issue:22"]
        judgments["issues"].append(second)
        second_prepared = copy.deepcopy(prepared["issues"][0])
        second_prepared.update(
            issueNumber=22,
            issueUrl="https://github.com/owner/repo/issues/22",
            evidenceBundle=[{"id": "issue:22", "kind": "issue-event"}],
        )
        prepared["issues"].append(second_prepared)
        source = copy.deepcopy(snapshot["evidence"]["issue:21"])
        source["url"] = "https://github.com/owner/repo/issues/22"
        source["payload"]["number"] = 22
        snapshot["evidence"]["issue:22"] = source
        snapshot["issues"].append({"number": 22, "state": "open"})
        snapshot["openIssues"].append(22)

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual([21, 22], [proposal["issueNumber"] for proposal in result["proposals"]])
        self.assertEqual(
            [["issue:21"], ["issue:22"]],
            [proposal["evidenceIds"] for proposal in result["proposals"]],
        )

    def test_other_quarantine_blockers_do_not_claim_an_unsupported_name(self) -> None:
        for target in ("Namespace.Type.Method", "Namespace.Outer+Inner.Method"):
            with self.subTest(target=target):
                snapshot, prepared, judgments = _inputs(target)
                self.assertEqual(
                    [],
                    build_action_proposals(snapshot, prepared, judgments, "ankj")["proposals"],
                )

    def test_missing_policy_does_not_turn_into_a_name_format_notice(self) -> None:
        snapshot, prepared, judgments = _inputs()
        del prepared["repositoryPolicyDigest"]

        self.assertEqual(
            [],
            build_action_proposals(snapshot, prepared, judgments, "ankj")["proposals"],
        )

    def test_name_format_notice_requires_a_quarantine_recommendation(self) -> None:
        snapshot, prepared, judgments = _inputs()
        judgments["issues"][0]["recommendations"][0]["disposition"] = "investigate"

        self.assertEqual(
            [],
            build_action_proposals(snapshot, prepared, judgments, "ankj")["proposals"],
        )

    def test_explicit_human_question_retains_the_canonical_comment_slot(self) -> None:
        snapshot, prepared, judgments = _inputs()
        judgments["issues"][0]["recommendations"].extend(
            _ping_human_judgments()["issues"][0]["recommendations"]
        )

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(1, len(result["proposals"]))
        self.assertTrue(result["proposals"][0]["actionId"].endswith("ping-human-comment"))

    def test_target_markdown_cannot_escape_the_literal_block(self) -> None:
        snapshot, prepared, judgments = _inputs("Scenario\n```\n@someone")

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertIn(
            "````text\nScenario\n```\n@someone\n````",
            result["proposals"][0]["body"],
        )

    def test_embedded_control_markers_do_not_break_collected_comment_ownership(self) -> None:
        snapshot, prepared, judgments = _inputs("Scenario <!-- ci-shepherd:role=zzz -->")
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])
        record = snapshot["evidence"]["issue:21:comment:900"]
        payload = record["payload"]
        payload["url"] = record["url"]
        collector = Collector(
            ScriptedClient(pages={}, singles={}),
            "owner/repo",
            datetime(2026, 8, 21, tzinfo=UTC),
            shepherd_author="ankj",
        )
        ownership, markers, facts, references = collector._extract_comment_payload(
            21, payload, "issue:21:comment:900",
        )
        payload.update(shepherdStatus=ownership, markers=markers, facts=facts, references=references)
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = "Scenario changed"

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(["edit-comment"], [proposal["operation"] for proposal in result["proposals"]])

    def test_target_changes_are_not_discarded_as_evidence_citation_churn(self) -> None:
        snapshot, prepared, judgments = _inputs("Scenario\n**Evidence reviewed:**\n- OLD\n\n")
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = (
            "Scenario\n**Evidence reviewed:**\n- NEW\n\n"
        )

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(["edit-comment"], [proposal["operation"] for proposal in result["proposals"]])

    def test_escaping_does_not_merge_distinct_reported_names(self) -> None:
        snapshot, prepared, judgments = _inputs("Scenario <!-- example -->")
        first = build_action_proposals(snapshot, prepared, judgments, "ankj")
        snapshot = _with_owned_comment(snapshot, first["proposals"][0]["body"])
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = (
            "Scenario &lt;!-- example --&gt;"
        )

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(["edit-comment"], [proposal["operation"] for proposal in result["proposals"]])

    def test_notice_does_not_bypass_issue_eligibility(self) -> None:
        snapshot, prepared, judgments = _inputs()
        snapshot["evidence"]["issue:21"]["payload"]["labels"] = []

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertFalse(result["proposals"][0]["executionEligibility"]["eligible"])
        self.assertEqual(
            ["missing-ci-label"],
            result["proposals"][0]["executionEligibility"]["blockingReasons"],
        )
        self.assertEqual([], build_comment_selection(result, max_comments=5)["selectedActionIds"])
        self.assertFalse(build_dry_run(result, action_id=None)["actions"][0]["wouldExecute"])

    def test_notice_preserves_collection_completeness_checks(self) -> None:
        snapshot, prepared, judgments = _inputs()
        snapshot["collectionErrors"] = [{
            "stage": "workflow-log",
            "message": "Required diagnostics were unavailable.",
            "sourceIssueNumber": 21,
        }]

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertFalse(result["proposals"][0]["executionEligibility"]["eligible"])
        self.assertIn(
            "incomplete-collection",
            result["proposals"][0]["executionEligibility"]["blockingReasons"],
        )


if __name__ == "__main__":
    unittest.main()
