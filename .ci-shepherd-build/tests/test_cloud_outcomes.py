from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cycle
from ci_shepherd import delegation_observer
from ci_shepherd.actions import _delegation_instructions
from ci_shepherd.models import ValidationError, validate_snapshot
from tests.assessment_helpers import finish_reviewed_cycle
from tests.test_cycle import snapshot
from tests.test_delegation_observer import ScriptedClient
from tests.test_scripts import load_script


def outcome_snapshot(*, task_state="completed", files=3, body="Fix proposed; blocked on credentials."):
    value = snapshot("2026-09-02T19:00:00Z")
    value["evidence"]["issue:1"]["payload"]["updatedAt"] = "2026-09-02T18:00:00Z"
    value.update(openIssues=[], delegatedIssues=[1], delegatedPullRequests=[201])
    value["delegatedIssueDetails"] = [copy.deepcopy(value["evidence"]["issue:1"]["payload"])]
    record = {
        "actionId": "assignment:1", "repository": "owner/repo", "issueNumber": 1,
        "startedAt": "2026-09-02T18:00:00Z", "taskId": "task-1", "taskState": task_state,
        "taskObservation": "available", "lifecycle": "awaiting_pull_request",
        "requiresHuman": False, "requiresNewDecision": True,
        "issueOpen": True, "copilotAssigned": True, "humanAssigned": False,
        "pullRequests": [{
            "databaseId": 101, "globalId": "PR_101", "number": 201, "state": "open",
            "isDraft": True, "changedFiles": files, "progressSource": {"headSha": "a" * 40},
        }],
    }
    value["delegationStatus"] = {"status": "complete", "records": [record]}
    value["delegatedPullRequestDetails"] = [{
        "number": 201, "url": "https://github.com/owner/repo/pull/201", "author": "Copilot",
        "body": body, "updatedAt": "2026-09-02T18:59:00Z",
    }]
    return value


class CloudOutcomeTests(unittest.TestCase):
    def attach(self, value, previous=None, client=None):
        delegation_observer.attach_cloud_outcomes(value, previous, client or ScriptedClient({}))
        validate_snapshot(value)
        return value["delegationStatus"]["records"][0]["outcomeEvidence"]

    def test_reuses_full_collected_body_and_binds_author_head_and_tail(self):
        value = outcome_snapshot(body="x" * 4000 + "blocked")
        original = copy.deepcopy(value)
        client = ScriptedClient({})
        evidence = self.attach(value, client=client)
        self.assertEqual([], client.calls)
        pull = evidence["pullRequests"][0]
        self.assertEqual(("Copilot", "a" * 40, "https://github.com/owner/repo/pull/201"),
                         (pull["author"], pull["headSha"], pull["url"]))
        self.assertEqual("x" * 4000, pull["body"]["preview"])
        self.assertTrue(pull["body"]["truncated"])
        changed = copy.deepcopy(original)
        changed["delegatedPullRequestDetails"][0]["body"] = "x" * 4000 + "fixed"
        after = self.attach(changed, value)
        self.assertNotEqual(evidence["fingerprint"], after["fingerprint"])
        self.assertEqual(pull["body"]["preview"], after["pullRequests"][0]["body"]["preview"])

    def test_unavailable_no_pr_empty_pr_and_failed_task_do_not_imply_fix(self):
        for state, files, no_pr, unavailable in (
            ("completed", 0, True, False), ("completed", 0, False, False),
            ("failed", 0, True, False), (None, 3, True, True),
            ("in_progress", 0, True, True),
        ):
            with self.subTest(state=state, files=files, no_pr=no_pr):
                value = outcome_snapshot(task_state=state, files=files, body="")
                record = value["delegationStatus"]["records"][0]
                if no_pr:
                    record["pullRequests"] = []
                    value["delegatedPullRequestDetails"] = []
                    value["delegatedPullRequests"] = []
                if unavailable:
                    record["taskObservation"] = "unavailable"
                evidence = self.attach(value)
                self.assertEqual("unavailable", evidence["availability"])
                self.assertTrue(evidence["assessmentRequired"])
                self.assertEqual("outcome evidence unavailable", evidence["detail"])
                self.assertEqual([], evidence["pullRequests"] if no_pr else [])

    def test_completed_blocked_draft_is_assessed_once_and_retained_without_reminder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state"
            baseline = snapshot("2026-09-02T18:59:00Z")
            baseline["evidence"]["issue:1"]["payload"]["updatedAt"] = "2026-09-02T18:00:00Z"
            baseline_path = root / "baseline.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            baseline_work = root / "baseline"
            cycle.start_cycle(repository="owner/repo", state_dir=state, work_dir=baseline_work,
                              checkout=None, shepherd_author="shepherd", input_path=baseline_path)
            finish_reviewed_cycle(work_dir=baseline_work, agent_judgments_path=baseline_work / "agent-judgments.json")
            value = outcome_snapshot(body="Fix proposed; blocked on credentials." + "x" * 4000)
            self.attach(value)
            previous = None
            for index in range(5):
                value["collectedAt"] = f"2026-09-02T19:0{index}:00Z"
                if index == 2:
                    value["delegatedPullRequestDetails"][0]["body"] += " Need a maintainer decision."
                if index == 3:
                    value["delegationStatus"]["records"][0]["pullRequests"][0]["progressSource"]["headSha"] = "b" * 40
                self.attach(value, previous)
                input_path = root / f"input-{index}.json"
                input_path.write_text(json.dumps(value), encoding="utf-8")
                work = root / f"work-{index}"
                result = cycle.start_cycle(
                    repository="owner/repo", state_dir=state, work_dir=work, checkout=None,
                    shepherd_author="shepherd", input_path=input_path,
                )
                self.assertEqual(0 if index in {1, 4} else 1, result["issueReviewCount"])
                prepared = json.loads((work / "assessment-input.json").read_text())
                self.assertEqual("task-1", prepared["issues"][0]["delegationContext"]["records"][0]["outcomeEvidence"]["taskId"])
                selection = json.loads((work / "review-selection.json").read_text())
                if index not in {1, 4}:
                    self.assertNotIn("delegate-copilot", selection["selected"][0]["allowedDispositions"])
                    self.assertNotIn("ping-human", selection["selected"][0]["allowedDispositions"])
                    self.assertIn("reported", selection["selected"][0]["question"]["ask"])
                    judgments = {
                        "schemaVersion": 1, "snapshotId": result["snapshotId"], "issues": [{
                            "issueNumber": 1, "category": "unknown", "recommendations": [{
                                "disposition": "watch", "target": {"kind": "issue", "value": 1},
                                "confidence": "medium", "summary": "Fix proposed; credentials blocker reported at https://github.com/owner/repo/pull/201.",
                                "evidenceIds": ["issue:1"], "missingEvidence": ["A maintainer decision about credentials."],
                                "reassessWhen": "After outcome evidence changes or the typed handoff wakeup.",
                            }],
                        }],
                    }
                    (work / "agent-judgments.json").write_text(json.dumps(judgments), encoding="utf-8")
                else:
                    self.assertEqual("A maintainer decision about credentials.",
                                     selection["omitted"][0]["retainedJudgment"]["recommendations"][0]["missingEvidence"][0])
                if result["stage"] == "awaiting-review":
                    finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                else:
                    self.assertEqual("completed", result["stage"])
                proposals = json.loads((work / "action-proposals.json").read_text())
                self.assertEqual([], proposals["proposals"])
                report = (work / "report.md").read_text()
                for expected in (
                    "PR body reported at https://github.com/owner/repo/pull/201 by Copilot",
                    "blocked on credentials.", "Assessed repair outcome:",
                    "credentials blocker reported at https://github.com/owner/repo/pull/201.",
                    "A maintainer decision about credentials.",
                    "Task completion and draft contents are not verified repair.",
                ):
                    with self.subTest(cycle=index, expected=expected):
                        self.assertIn(expected, report)
                previous = copy.deepcopy(value)

    def test_tampered_bound_outcome_is_rejected(self):
        value = outcome_snapshot()
        self.attach(value)
        for field, replacement in (("taskId", "other-task"), ("fingerprint", "fnv1a64:0000000000000000")):
            with self.subTest(field=field):
                tampered = copy.deepcopy(value)
                tampered["delegationStatus"]["records"][0]["outcomeEvidence"][field] = replacement
                with self.assertRaises(ValidationError):
                    validate_snapshot(tampered)

    def test_investigate_outcome_does_not_retire_existing_status_before_handoff(self):
        for delegated in (True, False):
            with self.subTest(delegated=delegated), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                value = outcome_snapshot()
                value["evidence"]["issue:1"]["payload"]["labels"] = ["ci-failure-cause"]
                value["evidence"]["issue:1:comment:900"] = {
                    "kind": "issue-comment", "availability": "available", "collectedAt": value["collectedAt"],
                    "url": "https://github.com/owner/repo/issues/1#issuecomment-900",
                    "payload": {
                        "id": 900, "sourceIssueNumber": 1, "author": "shepherd",
                        "createdAt": "2026-09-02T18:00:00Z", "updatedAt": "2026-09-02T18:00:00Z",
                        "body": "[automated] Watching this issue while the draft awaits review.",
                        "markers": [], "facts": [], "references": [],
                        "shepherdStatus": {"role": "status", "idempotencyKey": "issue:1:status", "owned": True},
                    },
                }
                if delegated:
                    value["delegatedIssueDetails"][0]["labels"] = ["ci-failure-cause"]
                    self.attach(value)
                else:
                    value["openIssues"] = [1]
                    for field in ("delegatedIssues", "delegatedIssueDetails", "delegatedPullRequests",
                                  "delegatedPullRequestDetails", "delegationStatus"):
                        value.pop(field)
                path = root / "input.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                work = root / "work"
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd", input_path=path,
                )
                prepared = json.loads((work / "assessment-input.json").read_text())
                if delegated:
                    self.assertFalse(prepared["issues"][0]["delegationContext"]["decisionRequired"])
                judgments = {
                    "schemaVersion": 1, "snapshotId": started["snapshotId"], "issues": [{
                        "issueNumber": 1, "category": "unknown", "recommendations": [{
                            "disposition": "investigate", "target": {"kind": "issue", "value": 1},
                            "confidence": "medium", "summary": "Name the evidence needed to assess the credential blocker.",
                            "evidenceIds": ["issue:1"], "missingEvidence": ["A maintainer credential decision."],
                            "reassessWhen": "After an independent decision or the typed handoff wakeup.",
                        }],
                    }],
                }
                (work / "agent-judgments.json").write_text(json.dumps(judgments), encoding="utf-8")
                finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                proposals = json.loads((work / "action-proposals.json").read_text())["proposals"]
                if delegated:
                    self.assertEqual([], proposals)
                else:
                    self.assertEqual(1, len(proposals))
                    self.assertTrue(proposals[0]["actionId"].endswith(":retire-status-comment"))
                    self.assertEqual("edit-comment", proposals[0]["operation"])

    def test_conclusion_request_preserves_scope_and_does_not_require_a_useless_diff(self):
        instructions = _delegation_instructions(1, None, workflow_failure=True)
        for expected in (
            "smallest complete", "draft pull request", "Do not remove quarantine or skip attributes",
            "Keep the incident open", "Do not manufacture a code change merely to produce a diff",
            "concise outcome", "evidence actually inspected", "changes made",
            "missing evidence or human decision", "suggested next step", "same-PR comment",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, instructions)

    def test_unknown_and_unsuccessful_attempts_enter_real_cycle_without_public_action(self):
        for state, no_pr, unavailable in (
            ("completed", True, False), ("completed", False, False),
            ("failed", True, False), ("waiting_for_user", False, False),
            (None, True, True),
        ):
            with self.subTest(state=state, no_pr=no_pr), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                value = outcome_snapshot(task_state=state, files=0, body="")
                record = value["delegationStatus"]["records"][0]
                record.update(lifecycle="handoff_required", requiresHuman=True)
                if no_pr:
                    record["pullRequests"] = []
                    value.update(delegatedPullRequests=[], delegatedPullRequestDetails=[])
                if unavailable:
                    record["taskObservation"] = "unavailable"
                self.attach(value)
                input_path = root / "input.json"
                input_path.write_text(json.dumps(value), encoding="utf-8")
                work = root / "work"
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd", input_path=input_path,
                )
                self.assertEqual(1, started["issueReviewCount"])
                prepared = json.loads((work / "assessment-input.json").read_text())
                context = prepared["issues"][0]["delegationContext"]
                self.assertEqual("outcome evidence unavailable", context["records"][0]["outcomeEvidence"]["detail"])
                self.assertFalse(context["decisionRequired"])
                finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                self.assertEqual([], json.loads((work / "action-proposals.json").read_text())["proposals"])

    def test_recent_comments_are_bounded_and_reobserved_without_pr_timestamp_changes(self):
        value = outcome_snapshot()
        record = value["delegationStatus"]["records"][0]
        source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {
            "html_url": "https://github.com/owner/repo/pull/201", "body": "Earlier body",
            "user": {"login": "Copilot"}, "head": {"sha": "a" * 40}, "comments": 12,
            "updated_at": "2026-09-02T18:59:00Z",
        })
        delegation_observer.initialize_cloud_outcome(record, {101: source})
        comments = [{
            "id": identity, "html_url": f"https://github.com/owner/repo/pull/201#issuecomment-{identity}",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/201",
            "user": {"login": "Copilot"}, "body": "x" * 2000 + "missing logs",
            "created_at": "2026-09-02T18:00:00Z", "updated_at": "2026-09-02T18:59:00Z",
        } for identity in (11, 12)]
        endpoint = "/repos/owner/repo/issues/201/comments?per_page=5&page=3"
        client = ScriptedClient({}, {endpoint: comments})
        before = self.attach(value, client=client)
        pull = before["pullRequests"][0]
        self.assertEqual([(endpoint, None)], client.calls)
        self.assertEqual("Fix proposed; blocked on credentials.", pull["body"]["preview"])
        self.assertEqual([11, 12], [comment["id"] for comment in pull["comments"]])
        self.assertTrue(pull["comments"][0]["body"]["truncated"])
        self.assertEqual(2000, len(pull["comments"][0]["body"]["preview"]))
        self.assertTrue(pull["commentWindowTruncated"])
        unchanged = copy.deepcopy(value)
        carried = self.attach(unchanged, value, client)
        self.assertEqual(before, carried)
        self.assertEqual([(endpoint, None)] * 2, client.calls)
        changed = copy.deepcopy(value)
        comments[0]["body"] = "x" * 2000 + "credentials needed"
        comments[0]["updated_at"] = "2026-09-02T19:00:00Z"
        after = self.attach(changed, value, client)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotEqual(pull["comments"][0]["body"]["fingerprint"], after["pullRequests"][0]["comments"][0]["body"]["fingerprint"])
        self.assertEqual([(endpoint, None)] * 3, client.calls)

    def test_comment_tail_edit_with_unchanged_pr_metadata_wakes_real_cycle_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = outcome_snapshot(body="")
            record = value["delegationStatus"]["records"][0]
            source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {})
            source["commentCount"] = 1
            delegation_observer.initialize_cloud_outcome(record, {101: source})
            comment = {
                "id": 11, "html_url": "https://github.com/owner/repo/pull/201#issuecomment-11",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/201",
                "user": {"login": "Copilot"}, "body": "x" * 2000 + "missing logs",
                "created_at": "2026-09-02T18:00:00Z", "updated_at": "2026-09-02T18:59:00Z",
            }
            endpoint = "/repos/owner/repo/issues/201/comments?per_page=5&page=1"
            client = ScriptedClient({}, {endpoint: [comment]})
            previous = None
            for index in range(4):
                value["collectedAt"] = f"2026-09-02T19:0{index}:00Z"
                if index == 2:
                    # Neither PR nor comment metadata advances in this fixture:
                    # the complete content fingerprint must detect the edit.
                    comment["body"] = "x" * 2000 + "needs a human decision"
                evidence = self.attach(value, previous, client)
                path = root / f"input-{index}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                work = root / f"work-{index}"
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd", input_path=path,
                )
                self.assertEqual(1 if index in {0, 2} else 0, started["issueReviewCount"])
                if started["stage"] == "awaiting-review":
                    finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                self.assertEqual([], json.loads((work / "action-proposals.json").read_text())["proposals"])
                if index == 2:
                    self.assertNotEqual(previous["delegationStatus"]["records"][0]["outcomeEvidence"]["fingerprint"],
                                        evidence["fingerprint"])
                previous = copy.deepcopy(value)
            self.assertEqual([(endpoint, None)] * 4, client.calls)

    def test_shared_pull_binding_reads_one_comment_window_per_collection(self):
        value = outcome_snapshot()
        record = value["delegationStatus"]["records"][0]
        source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {})
        source["commentCount"] = 1
        delegation_observer.initialize_cloud_outcome(record, {101: source})
        other = copy.deepcopy(record)
        other.update(actionId="assignment:1:another", taskId="task-2")
        value["delegationStatus"]["records"].append(other)
        endpoint = "/repos/owner/repo/issues/201/comments?per_page=5&page=1"
        client = ScriptedClient({}, {endpoint: []})
        self.attach(value, client=client)
        self.assertEqual([(endpoint, None)], client.calls)

    def test_running_task_does_not_query_comments_but_completion_does(self):
        value = outcome_snapshot(task_state="in_progress")
        record = value["delegationStatus"]["records"][0]
        source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {})
        source["commentCount"] = 1
        delegation_observer.initialize_cloud_outcome(record, {101: source})
        client = ScriptedClient({})
        before = self.attach(value, client=client)
        self.assertFalse(before["assessmentRequired"])
        self.assertEqual([], client.calls)
        ended = copy.deepcopy(value)
        ended["delegationStatus"]["records"][0]["taskState"] = "failed"
        after = self.attach(ended, value, client)
        self.assertTrue(after["assessmentRequired"])
        self.assertEqual("unavailable", after["pullRequests"][0]["commentsAvailability"])
        self.assertEqual([("/repos/owner/repo/issues/201/comments?per_page=5&page=1", None)], client.calls)

    def test_wrong_pr_comment_or_oversized_page_cannot_supply_outcome(self):
        for response in ([{}], [{}] * 6):
            with self.subTest(response=response):
                value = outcome_snapshot(body="")
                record = value["delegationStatus"]["records"][0]
                source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {})
                source["commentCount"] = 1
                delegation_observer.initialize_cloud_outcome(record, {101: source})
                evidence = self.attach(value, client=ScriptedClient({}, {
                    "/repos/owner/repo/issues/201/comments?per_page=5&page=1": response,
                }))
                self.assertEqual("unavailable", evidence["availability"])
                self.assertEqual([], evidence["pullRequests"][0]["comments"])

    def test_real_task_observer_preserves_linked_outcome_source_for_collection(self):
        collect = load_script("collect")
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/agents/repos/owner/repo/tasks/task-1": {
                "id": "task-1", "state": "completed", "created_at": "2026-09-02T18:00:00Z",
                "artifacts": [{"type": "pull", "provider": "github", "data": {"id": 101, "global_id": "PR_101"}}],
            },
            "/repos/owner/repo/pulls/201": {
                "id": 101, "number": 201, "node_id": "PR_101", "state": "open", "draft": True,
                "changed_files": 3, "head": {"sha": "a" * 40}, "comments": 0,
                "html_url": "https://github.com/owner/repo/pull/201", "body": "Missing reproduction.",
                "user": {"login": "Copilot"}, "updated_at": "2026-09-02T18:59:00Z",
            },
            "/repos/owner/repo/issues/1": {"number": 1, "state": "open", "assignees": []},
        })
        events = [
            {"eventType": "delegation-baseline", "actionId": "assignment:1", "recordedAt": "2026-09-02T18:00:00Z",
             "operation": "assign-copilot", "repository": "owner/repo", "target": {"kind": "issue", "number": 1}, "taskIdsBefore": []},
            {"eventType": "terminal", "actionId": "assignment:1", "outcome": "executed", "result": {"taskId": "task-1"}},
            {"eventType": "delegation-observed", "actionId": "assignment:1", "record": outcome_snapshot()["delegationStatus"]["records"][0]},
        ]
        status, _ = collect.observe_delegation_status(client, "owner/repo", events=events, now=datetime(2026, 9, 2, 19, tzinfo=UTC))
        record = status["records"][0]
        self.assertEqual("awaiting_pull_request", record["lifecycle"])
        self.assertFalse(record["requiresHuman"])
        self.assertEqual("Missing reproduction.", record["outcomeEvidence"]["pullRequests"][0]["body"]["preview"])
        value = outcome_snapshot()
        value["delegationStatus"] = status
        evidence = self.attach(value, client=client)
        self.assertEqual("Fix proposed; blocked on credentials.", evidence["pullRequests"][0]["body"]["preview"])
        self.assertTrue(evidence["assessmentRequired"])
        self.assertEqual("available", evidence["pullRequests"][0]["commentsAvailability"])
