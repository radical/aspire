from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cycle
from ci_shepherd.history import load_current
from ci_shepherd.lifecycle import control_comment_only_issue_numbers
from ci_shepherd.poc_state import record_review_wakeup
from tests.assessment_helpers import finish_reviewed_cycle
from tests import test_lifecycle as lifecycle_fixtures
from tests.test_cycle import snapshot


def review_snapshot():
    value = snapshot("2026-09-02T19:00:00Z")
    root = value["evidence"]["issue:1"]["payload"]
    root.update(body="Issue diagnostic " + "x" * 4000 + "old tail", updatedAt="2026-09-02T18:00:00Z")
    comments = []
    for identity, owned in ((21, False), (22, True)):
        payload = {
            "id": identity, "url": f"https://github.com/owner/repo/issues/1#issuecomment-{identity}",
            "author": "shepherd" if owned else "maintainer", "authorType": "User",
            "createdAt": "2026-09-02T18:00:00Z", "updatedAt": "2026-09-02T18:00:00Z",
            "sourceIssueNumber": 1,
            "body": "[automated] Watching." if owned else "Comment diagnostic " + "x" * 2000 + "old tail",
            "shepherdStatus": {"role": "status", "idempotencyKey": "issue:1:status", "owned": owned},
            "markers": [], "facts": [], "references": [],
        }
        value["evidence"][f"issue:1:comment:{identity}"] = {
            "kind": "issue-comment", "url": payload["url"], "collectedAt": value["collectedAt"],
            "availability": "available", "payload": payload,
        }
        comments.append(payload)
    root["comments"] = comments
    return value


def resolved_snapshot():
    payload = lifecycle_fixtures.issue_payload(
        14, producer="ci-failure-cause", autoclose=None,
        ledger=lifecycle_fixtures.complete_ledger(100),
        updated_at="2026-08-09T00:00:00Z",
    )
    payload["body"] = "Independent diagnosis " + "x" * 4000 + " old tail"
    reference = [{"sourceIssueNumber": 14}]
    value = lifecycle_fixtures.with_exact_coverage(lifecycle_fixtures.snapshot(
        payload,
        lifecycle_fixtures.evidence("pr:21", "pull-request", {
            "mergedAt": "2026-08-10T10:00:00Z", "mergeCommitSha": "b" * 40,
            "referencedBy": reference,
        }),
        lifecycle_fixtures.evidence("run:201", "workflow-run", {
            "conclusion": "success", "headSha": "b" * 40,
            "runStartedAt": "2026-08-10T10:01:00Z", "referencedBy": reference,
        }),
    ))
    value["evidence"]["issue:14:comment:900"] = {
        "kind": "issue-comment", "availability": "available",
        "url": "https://github.com/microsoft/aspire/issues/14#issuecomment-900",
        "payload": {
            "id": 900, "sourceIssueNumber": 14, "author": "shepherd",
            "createdAt": "2026-08-09T00:00:00Z", "updatedAt": "2026-08-09T00:00:00Z",
            "body": "[automated] Recovery is under review.",
            "markers": [], "facts": [], "references": [],
            "shepherdStatus": {"role": "status", "idempotencyKey": "issue:14:status", "owned": True},
        },
    }
    return value


class SemanticReviewChangeTests(unittest.TestCase):
    def run_cycle(self, root, value, index):
        value["collectedAt"] = f"{value['collectedAt'][:14]}{index:02}:00Z"
        for record in value["evidence"].values():
            record["collectedAt"] = value["collectedAt"]
        path = root / f"input-{index}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        work = root / f"work-{index}"
        started = cycle.start_cycle(
            repository=value["repository"], state_dir=root / "state", work_dir=work,
            checkout=None, shepherd_author="shepherd", input_path=path,
        )
        if started["stage"] == "awaiting-review":
            finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
        frozen = json.loads((work / "input.json").read_text())
        history = load_current(root / "state", value["repository"])
        sealed = json.loads((history.run_directory / "snapshot.json").read_text())
        for number in value["openIssues"]:
            identity = f"issue:{number}"
            self.assertEqual(value["evidence"][identity]["payload"]["updatedAt"],
                             frozen["evidence"][identity]["payload"]["updatedAt"])
            self.assertEqual(value["evidence"][identity]["payload"]["updatedAt"],
                             sealed["evidence"][identity]["payload"]["updatedAt"])
        return started, json.loads((work / "review-selection.json").read_text())

    def test_control_update_after_verified_fix_preserves_recovery_and_case_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = resolved_snapshot()
            for index, change in enumerate(("initial", "owned", "unchanged", "owned", "due")):
                previous = json.loads(json.dumps(value))
                if change == "owned":
                    updated = f"2026-08-19T16:{index:02}:00Z"
                    value["evidence"]["issue:14"]["payload"]["updatedAt"] = updated
                    value["evidence"]["issue:14:comment:900"]["payload"].update(
                        body=f"[automated] Still awaiting review, observation {index}.", updatedAt=updated,
                    )
                    self.assertEqual({14}, control_comment_only_issue_numbers(value, previous))
                elif change == "due":
                    record_review_wakeup(
                        root / "state", value["repository"], target_kind="issue", target_number=14,
                        evaluate_at="2026-08-19T16:04:00Z", reason="positive-coverage-review",
                    )
                value["refreshSummary"] = {"changedIssueNumbers": [14] if change == "owned" else []}
                started, selection = self.run_cycle(root, value, index)
                work = root / f"work-{index}"
                prepared = json.loads((work / "assessment-input.json").read_text())
                self.assertEqual("resolved", prepared["issues"][0]["candidateState"], change)
                self.assertEqual("verified", prepared["issues"][0]["recovery"]["status"], change)
                self.assertEqual(1 if change in {"initial", "due"} else 0, started["issueReviewCount"], change)
                if change in {"owned", "unchanged"}:
                    self.assertEqual("unchanged-stable", selection["omitted"][0]["reason"])
                for name in ("assessment-input.json", "assessment-defaults.json", "report.md"):
                    self.assertNotIn("issue-updated-after-fix-without-ledger-row", (work / name).read_text(), change)
                events = (root / "state" / "ledgers" / "case-events.jsonl").read_text().splitlines()
                if index == 0:
                    initial_events = events
                self.assertEqual(initial_events, events, change)
                proposals = json.loads((work / "action-proposals.json").read_text())["proposals"]
                self.assertTrue(proposals)
                for proposal in proposals:
                    self.assertEqual(value["evidence"]["issue:14"]["payload"]["updatedAt"],
                                     proposal["sourceEvidenceFingerprint"]["issueUpdatedAt"])

    def test_normalized_timestamp_does_not_hide_independent_changes(self):
        for change in ("body-tail", "fix-commit", "task-state", "unexplained-timestamp"):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                value = resolved_snapshot()
                if change == "task-state":
                    value["delegationStatus"] = {"status": "complete", "records": [{
                        "actionId": "assignment:14", "repository": value["repository"], "issueNumber": 14,
                        "startedAt": "2026-08-09T00:00:00Z", "taskId": "task-14", "taskState": "queued",
                        "taskObservation": "available", "lifecycle": "running",
                        "requiresHuman": False, "requiresNewDecision": False,
                        "issueOpen": True, "copilotAssigned": True, "humanAssigned": False, "pullRequests": [],
                    }]}
                self.run_cycle(root, value, 0)
                issue = value["evidence"]["issue:14"]["payload"]
                control = value["evidence"]["issue:14:comment:900"]["payload"]
                issue["updatedAt"] = "2026-08-19T16:01:00Z"
                control["body"] += " Refreshed."
                value["refreshSummary"] = {"changedIssueNumbers": [14]}
                self.assertEqual(0, self.run_cycle(root, value, 1)[0]["issueReviewCount"])

                value["refreshSummary"] = {"changedIssueNumbers": []}
                if change == "body-tail":
                    issue["body"] += " Changed independent tail."
                elif change == "unexplained-timestamp":
                    issue["updatedAt"] = "2026-08-19T16:02:00Z"
                else:
                    issue["updatedAt"] = "2026-08-19T16:02:00Z"
                    control["body"] += " Another refresh."
                    if change == "fix-commit":
                        value["evidence"]["pr:21"]["payload"]["mergeCommitSha"] = "c" * 40
                    else:
                        value["delegationStatus"]["records"][0]["taskState"] = "in_progress"
                self.assertEqual(1, self.run_cycle(root, value, 2)[0]["issueReviewCount"])
                prepared = json.loads((root / "work-2" / "assessment-input.json").read_text())["issues"][0]
                if change == "fix-commit":
                    self.assertEqual("observing", prepared["candidateState"])
                    self.assertEqual({}, prepared["resolutionEvidence"])
                elif change == "task-state":
                    self.assertEqual("in_progress", prepared["delegationContext"]["records"][0]["taskState"])
                    self.assertEqual("resolved", prepared["candidateState"])
                else:
                    self.assertEqual("needs-human", prepared["candidateState"])
                    self.assertIn("issue-updated-after-fix-without-ledger-row", prepared["blockers"])

    def test_control_comment_and_root_timestamp_churn_do_not_reassess_but_tail_edits_do(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = review_snapshot()
            control_record = value["evidence"]["issue:1:comment:22"]
            self.assertEqual(1, self.run_cycle(root, value, 0)[0]["issueReviewCount"])
            for index, target, expected in (
                (1, "owned", 0), (2, "comment", 1), (3, "issue", 1),
                (4, "owned", 0), (5, "owned-delete", 0),
                (6, "owned-add", 0), (7, "unchanged", 0),
            ):
                if target.startswith("owned"):
                    control_record["payload"].update(
                        body=f"[automated] Updated control status {index}.",
                        updatedAt=f"2026-09-02T19:{index:02}:00Z",
                    )
                    value["evidence"]["issue:1"]["payload"]["updatedAt"] = f"2026-09-02T19:{index:02}:00Z"
                    if target == "owned-delete":
                        del value["evidence"]["issue:1:comment:22"]
                        value["evidence"]["issue:1"]["payload"]["comments"] = [
                            comment for comment in value["evidence"]["issue:1"]["payload"]["comments"]
                            if comment["id"] != 22
                        ]
                    elif target == "owned-add":
                        value["evidence"]["issue:1:comment:22"] = control_record
                        value["evidence"]["issue:1"]["payload"]["comments"].append(control_record["payload"])
                    value["refreshSummary"] = {"changedIssueNumbers": [1]}
                elif target == "comment":
                    value["evidence"]["issue:1:comment:21"]["payload"]["body"] += " new independent tail"
                    value["refreshSummary"] = {"changedIssueNumbers": []}
                elif target == "issue":
                    value["evidence"]["issue:1"]["payload"]["body"] += " new issue tail"
                    value["refreshSummary"] = {"changedIssueNumbers": []}
                else:
                    value["refreshSummary"] = {"changedIssueNumbers": []}
                result, selection = self.run_cycle(root, value, index)
                self.assertEqual(expected, result["issueReviewCount"], target)
                if expected == 0:
                    self.assertEqual("unchanged-stable", selection["omitted"][0]["reason"])

    def test_control_churn_does_not_hide_other_source_changes(self):
        for change in ("body", "labels", "unowned-comment", "availability"):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                value = review_snapshot()
                self.run_cycle(root, value, 0)
                value["evidence"]["issue:1:comment:22"]["payload"]["body"] += " refreshed"
                value["evidence"]["issue:1"]["payload"]["updatedAt"] = "2026-09-02T19:01:00Z"
                value["refreshSummary"] = {"changedIssueNumbers": [1]}
                if change == "body":
                    value["evidence"]["issue:1"]["payload"]["body"] += " changed tail"
                elif change == "labels":
                    value["evidence"]["issue:1"]["payload"]["labels"] = ["blocking-ci"]
                elif change == "unowned-comment":
                    value["evidence"]["issue:1:comment:21"]["payload"]["body"] += " changed independent tail"
                else:
                    value["evidence"]["issue:1:comment:21"]["availability"] = "expired-or-unavailable"
                self.assertEqual(1, self.run_cycle(root, value, 1)[0]["issueReviewCount"])

    def test_unchanged_independent_sources_do_not_make_unexplained_timestamp_churn_owned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = review_snapshot()
            self.run_cycle(root, value, 0)
            value["evidence"]["issue:1"]["payload"]["updatedAt"] = "2026-09-02T19:01:00Z"
            value["refreshSummary"] = {"changedIssueNumbers": [1]}
            self.assertEqual(1, self.run_cycle(root, value, 1)[0]["issueReviewCount"])
