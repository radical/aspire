from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cycle as cycle_script


def snapshot(
    collected_at: str,
    *,
    title: str = "Unclassified CI failure",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": collected_at,
        "openIssues": [1],
        "openPullRequests": [],
        "pullRequests": [],
        "rejectedCandidates": [],
        "evidence": {
            "issue:1": {
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/1",
                "collectedAt": collected_at,
                "availability": "available",
                "payload": {
                    "number": 1,
                    "state": "open",
                    "title": title,
                    "url": "https://github.com/owner/repo/issues/1",
                    "author": "github-actions[bot]",
                },
            }
        },
        "collectionErrors": [],
        "warnings": [],
        "openBotScan": {
            "status": "complete",
            "complete": True,
            "scannedPages": 1,
            "pageBudget": 40,
            "itemBudget": 250,
            "botAuthoredFound": 1,
            "botAuthoredAdopted": 1,
            "detail": None,
        },
    }


def pull_request_snapshot(collected_at: str) -> dict[str, object]:
    current_state = {
        "headSha": "abc",
        "checks": {
            "source": "check-runs",
            "state": "green",
            "total": 1,
            "failing": [],
            "pending": [],
            "truncated": False,
            "complete": True,
        },
        "review": {
            "decision": "review-required",
            "reviewers": [],
            "complete": True,
        },
        "mergeable": True,
        "mergeableState": "clean",
        "draft": False,
        "complete": True,
        "incompleteReasons": [],
    }
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": collected_at,
        "openIssues": [],
        "openPullRequests": [23],
        "pullRequests": [
            {
                "number": 23,
                "state": "open",
                "title": "Repair CI automation",
                "url": "https://github.com/owner/repo/pull/23",
                "updatedAt": collected_at,
                "labels": ["automation-broken"],
                "author": "github-actions[bot]",
                "assignees": [],
                "selectionReasons": ["label:automation-broken"],
            }
        ],
        "rejectedCandidates": [],
        "evidence": {
            "pr:23": {
                "kind": "pull-request",
                "url": "https://github.com/owner/repo/pull/23",
                "collectedAt": collected_at,
                "availability": "available",
                "payload": {
                    "number": 23,
                    "state": "open",
                    "head": {"sha": "abc", "ref": "automation/fix"},
                    "base": {"sha": "def", "ref": "main"},
                    "files": [],
                    "currentState": current_state,
                },
            }
        },
        "collectionErrors": [],
    }


class CycleTests(unittest.TestCase):
    def test_bootstraps_review_events_for_state_created_before_scheduling(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-20T12:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            cycle_script.finish_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            (state / "ledgers" / "review-events.jsonl").unlink()

            next_input = root / "input-2.json"
            next_input.write_text(
                json.dumps(snapshot("2026-08-21T12:00:00Z")),
                encoding="utf-8",
            )
            next_work = root / "work-2"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=next_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=next_input,
            )
            selection = json.loads(
                (next_work / "review-selection.json").read_text(encoding="utf-8")
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual("first-seen", selection["selected"][0]["changeClass"])
            self.assertEqual(
                ["first-seen"],
                selection["selected"][0]["changeReasons"],
            )
            self.assertIn(
                "initial-assessment",
                selection["selected"][0]["reviewReasons"],
            )

    def test_reselects_an_unchanged_case_when_its_review_becomes_due(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"

            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-20T12:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            cycle_script.finish_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            self.assertEqual("awaiting-review", started["stage"])

            due_input = root / "input-2.json"
            due_input.write_text(
                json.dumps(snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )
            due_work = root / "work-2"
            due = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=due_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=due_input,
            )

            self.assertEqual("awaiting-review", due["stage"])
            selection = json.loads(
                (due_work / "review-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual("due", selection["selected"][0]["changeClass"])
            self.assertEqual(
                "2026-08-20T12:00:00Z",
                selection["selected"][0]["lastReviewedAt"],
            )
            self.assertEqual(
                "2026-08-27T12:00:00Z",
                selection["selected"][0]["reassessAt"],
            )
            cycle_script.finish_cycle(
                work_dir=due_work,
                agent_judgments_path=due_work / "agent-judgments.json",
            )

            after_review_input = root / "input-3.json"
            after_review_input.write_text(
                json.dumps(snapshot("2026-08-28T12:00:00Z")),
                encoding="utf-8",
            )
            after_review = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=root / "work-3",
                checkout=None,
                shepherd_author="ankj",
                input_path=after_review_input,
            )
            self.assertEqual("completed", after_review["stage"])
            self.assertEqual(0, after_review["issueReviewCount"])

    def test_resumes_agent_review_then_auto_finalizes_unchanged_cycle(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_work = root / "work-1"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )

            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual(1, started["issueReviewCount"])
            self.assertTrue((first_work / "assessment-defaults.json").is_file())
            selected = json.loads(
                (first_work / "review-selection.json").read_text(encoding="utf-8")
            )
            agent_input = json.loads(
                (first_work / "agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [entry["issueNumber"] for entry in selected["selected"]],
                [issue["issueNumber"] for issue in agent_input["issues"]],
            )
            self.assertTrue((first_work / "agent-pull-request-judgments.json").is_file())
            agent_judgments = first_work / "agent-judgments.json"
            self.assertTrue(agent_judgments.is_file())

            completed = cycle_script.finish_cycle(
                work_dir=first_work,
                agent_judgments_path=agent_judgments,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertTrue((first_work / "report.md").is_file())
            self.assertTrue((first_work / "action-proposals.json").is_file())
            self.assertEqual(1, len(list((state / "runs").iterdir())))

            second_work = root / "work-2"
            second_input = root / "input-2.json"
            second_input.write_text(
                json.dumps(snapshot("2026-08-28T12:00:00Z")),
                encoding="utf-8",
            )
            unchanged = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", unchanged["stage"])
            self.assertEqual(0, unchanged["issueReviewCount"])
            self.assertEqual(2, len(list((state / "runs").iterdir())))

            changed_work = root / "work-3"
            changed_input = root / "input-3.json"
            changed_input.write_text(
                json.dumps(
                    snapshot(
                        "2026-08-29T12:00:00Z",
                        title="Unclassified CI failure with a new signature",
                    )
                ),
                encoding="utf-8",
            )
            changed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=changed_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=changed_input,
            )

            self.assertEqual("awaiting-review", changed["stage"])
            self.assertEqual(1, changed["issueReviewCount"])
            changed_selection = json.loads(
                (changed_work / "review-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["derived-assessment-changed"],
                changed_selection["selected"][0]["changeReasons"],
            )
            self.assertEqual(
                "investigate",
                changed_selection["selected"][0]["previousDisposition"],
            )

    def test_finishes_pull_request_review_into_report_and_shared_proposals(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(pull_request_snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual(0, started["issueReviewCount"])
            self.assertEqual(1, started["pullRequestReviewCount"])
            pull_request_judgments = work / "pull-request-judgments.json"
            pull_request_judgments.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "pullRequests": [],
                    }
                ),
                encoding="utf-8",
            )

            completed = cycle_script.finish_cycle(
                work_dir=work,
                agent_judgments_path=work / "agent-judgments.json",
                pull_request_judgments_path=pull_request_judgments,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertIn("## Pull requests", (work / "report.md").read_text())
            proposals = json.loads(
                (work / "action-proposals.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], proposals["proposals"])
            self.assertEqual([], proposals["unchangedPullRequestNumbers"])

    def test_report_surfaces_incomplete_collection(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            document = snapshot("2026-08-27T12:00:00Z")
            document["warnings"] = ["open bot-authored inventory is incomplete"]
            document["collectionErrors"] = [
                {
                    "stage": "open-bot-scan",
                    "endpoint": "/repos/owner/repo/issues?page=2",
                    "message": "rate limited",
                }
            ]
            document["openBotScan"] = {
                "status": "failed",
                "complete": False,
                "scannedPages": 1,
                "pageBudget": 40,
                "itemBudget": 250,
                "botAuthoredFound": 100,
                "botAuthoredAdopted": 100,
                "detail": "rate limited",
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(document), encoding="utf-8")
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_judgments = work / "agent-judgments.json"
            agent_judgments.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                    }
                ),
                encoding="utf-8",
            )

            cycle_script.finish_cycle(
                work_dir=work,
                agent_judgments_path=agent_judgments,
            )

            report = (work / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Collection completeness", report)
            self.assertIn("**Open bot scan:** `failed`", report)
            self.assertIn("**Collection errors:** 1", report)
            self.assertIn(
                "`open-bot-scan`: rate limited "
                "(`/repos/owner/repo/issues?page=2`)",
                report,
            )
            self.assertIn("open bot-authored inventory is incomplete", report)


if __name__ == "__main__":
    unittest.main()
