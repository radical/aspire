from __future__ import annotations

import copy
import json
import unittest
from datetime import timedelta
from pathlib import Path

import test_authorization as authorization_fixture
from test_delegation_execution import FakeExecution, ScriptedClient, task
from test_policy_selection import _event

from ci_shepherd.delegation_execution import reserve_delegation_start
from ci_shepherd.delegations import (
    CapacityLimits, DelegatedPullRequest, PullRequestState,
    derive_delegation_tracking, normalize_agent_task,
)
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.eligibility import repair_priority
from ci_shepherd.policy_budget import CoordinatorPolicyBudgetValidator
from ci_shepherd.actor import execute_action
from test_actor import ScriptedActorClient
from test_repair_routing import (
    collect_repair_logs, collect_triggering_pull_request, override_category,
    producer_snapshot, producer_workflow_snapshot, repair_snapshot,
)
from test_production_decisions import assess
from ci_shepherd.github import GitHubApiError


class CapacityClient(ScriptedClient):
    def __init__(self, task_pages, pulls):
        super().__init__(task_pages)
        self.pulls = pulls

    def get(self, endpoint: str) -> object:
        if "/issues/" in endpoint:
            return {
                "number": int(endpoint.rsplit("/", 1)[-1]),
                "state": "open",
                "assignees": [{"login": "Copilot"}],
            }
        if "/pulls/" in endpoint:
            return next(pull for pull in self.pulls if pull["number"] == int(endpoint.rsplit("/", 1)[-1]))
        try:
            return super().get(endpoint)
        except KeyError:
            raise GitHubApiError(
                category="not-found", endpoint=endpoint, status=404,
                headers={}, retryable=False, attempts=1, sanitized_stderr="",
            ) from None

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        if "/pulls?" in endpoint:
            return self.pulls
        return super().get_pages(endpoint, key)


class CheckedInRepairCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = authorization_fixture.AutonomousPolicyGrantTests(
            "test_requires_exactly_one_action_id"
        )
        fixture._testMethodName = self._testMethodName
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture = fixture
        policies = Path(__file__).resolve().parents[1] / "policies"
        self.capacity_path = policies / "production-delegation-v1.json"
        policy = authorization_fixture._policy_document(
            repository=fixture.repository, revision=1,
            created_at_utc=fixture.now - timedelta(days=1),
            expires_at_utc=fixture.now + timedelta(days=1),
            enabled_classes=frozenset({"delegate-copilot"}),
        )
        policy["operationClasses"] = json.loads(
            (policies / "autonomous-live-pilot-caps-v1.json").read_text(encoding="utf-8")
        )
        fixture.store.append_policy_revision(
            repository=fixture.repository, expected_revision=0, document=policy,
        )
        template = fixture.proposals["proposals"][0]
        proposals = []
        for number in range(2, 6):
            proposal = copy.deepcopy(template)
            for field in ("commentId", "sourceCommentFingerprint", "body"):
                proposal.pop(field)
            proposal.update(
                actionId=f"{fixture.proposals['snapshotId']}:issue:{number}:delegate",
                issueNumber=number,
                issueUrl=f"https://github.com/{fixture.repository}/issues/{number}",
                operation="assign-copilot", targetRepository=fixture.repository,
                baseBranch="main", customInstructions=f"Investigate and fix issue #{number}.",
                model="", idempotencyKey=f"issue:{number}:delegate",
                evidenceIds=[f"issue:{number}"],
            )
            proposals.append(proposal)
        fixture.proposals["proposals"] = proposals
        fixture._write_proposals()

    def _history(self, states: list[str], *, age: timedelta = timedelta(hours=1), same_cycle: bool = False):
        fixture = self.fixture
        events = []
        tasks = []
        for index, state in enumerate(states):
            timestamp = fixture.now - age
            action_id = f"previous:{index}"
            common = dict(
                action_id=action_id, operation="assign-copilot", target_number=100 + index,
                idempotency_key=action_id, recorded_at=timestamp,
                run_id=f"cycle:{fixture.proposals['snapshotId']}" if same_cycle else "previous-cycle",
            )
            events.append({
                **_event(event_type="delegation-baseline", **common), "taskIdsBefore": [],
            })
            events.append({
                **_event(event_type="terminal", **common), "result": {"taskId": f"task-{index}"},
            })
            record = task(f"task-{index}")
            record.update(
                state=state, created_at=timestamp.isoformat(), updated_at=timestamp.isoformat(),
            )
            tasks.append(record)
        (fixture.state_dir / "action-events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        return tasks

    def _selection(self):
        fixture = self.fixture
        events = ActionEventStore(fixture.state_dir).events(repository=fixture.repository)
        return fixture._build_and_write_selection(action_events=events)

    def _admission(self, tasks, pulls=()):
        fixture = self.fixture
        selected = self._selection()["selectedActionIds"]
        if not selected:
            return None
        grant = fixture._mint(
            selected[0], production_delegation_policy_path=self.capacity_path,
        )
        authorized = fixture._load(
            selected[0], production_delegation_policy_path=self.capacity_path,
        )
        self.assertEqual(grant["allowedActionIds"], [authorized.proposal["actionId"]])
        budget = grant["budget"]
        events = ActionEventStore(fixture.state_dir).events(repository=fixture.repository)
        return reserve_delegation_start(
            execution=FakeExecution(events), client=CapacityClient([tasks, []], pulls),
            repository=fixture.repository, now=fixture.now,
            limits=CapacityLimits(
                budget["maxRunningCopilotTasks"], budget["maxCopilotStartsPerRolling24h"],
                budget["maxOpenDelegatedPullRequests"], budget["maxRepositoryRunningCopilotTasks"],
            ),
        )

    def _set_assessed_proposals(self, proposals, *, evidence_round):
        proposals["shepherdAuthor"] = "radical"
        proposals["productionPilotCapability"] = {
            **self.fixture.proposals["productionPilotCapability"], "evidenceRound": evidence_round,
        }
        self.fixture.proposals = proposals
        self.fixture._write_proposals()

    def _execute_assessed_repair(self, value, category):
        fixture = self.fixture
        value["collectedAt"] = fixture.now.isoformat().replace("+00:00", "Z")
        source = value["evidence"]["issue:21"]["payload"]
        proposals = assess(value)[3] if category is None else override_category(value, category)[3]
        self._set_assessed_proposals(proposals, evidence_round=len(value.get("expansions", [])))
        admission = self._admission([])
        self.assertIsNotNone(admission)
        self.assertTrue(admission.permitted)
        action_id, = self._selection()["selectedActionIds"]
        authorized = fixture._load(action_id, production_delegation_policy_path=self.capacity_path)
        live = {
            "number": 21, "state": "open", "html_url": source["url"],
            "updated_at": source["updatedAt"], "labels": source["labels"], "body": source["body"],
            "assignees": [], "user": {"login": source["author"], "type": source.get("authorType", "Bot")},
        }
        client = ScriptedActorClient(authenticated_login="radical", issues=[
            live, {**live, "assignees": [{"login": "Copilot"}]},
        ])
        result = execute_action(
            proposals, action_id=authorized.proposal["actionId"],
            prior_results={"schemaVersion": 1, "repository": fixture.repository, "results": []},
            client=client, now=lambda: fixture.now,
        )
        self.assertEqual("executed", result["outcome"])
        self.assertEqual(1, len([call for call in client.calls if call[0] == "assign_copilot"]))
        return authorized.proposal

    def test_checked_in_grant_allows_third_active_but_blocks_fourth(self) -> None:
        tasks = self._history(["queued", "in_progress"])
        self.assertTrue(self._admission(tasks).permitted)
        tasks = self._history(["queued", "in_progress", "queued"])
        blocked = self._admission(tasks)
        self.assertFalse(blocked.permitted)
        self.assertIn("max_running_tasks", blocked.blocked_by)

    def test_repository_wide_safety_ceiling_remains_one_hundred(self) -> None:
        tasks = [task(f"foreign-{index}") for index in range(100)]
        self.assertTrue(self._admission(tasks[:99]).permitted)
        blocked = self._admission(tasks)
        self.assertFalse(blocked.permitted)
        self.assertIn("max_repository_running_tasks", blocked.blocked_by)

    def test_verified_producer_assignment_passes_exact_grant_and_live_preflight(self) -> None:
        value = producer_snapshot()
        value["expansions"] = [{"round": 1}]
        source = value["evidence"]["issue:21"]["payload"]
        source["body"] = source["body"].replace("workflow_id: ci,", "workflow_id: analyze-ci-failure,")
        value["evidence"]["run:100"]["payload"]["workflowPath"] = ".github/workflows/analyze-ci-failure.lock.yml"
        value["evidence"] = {
            key: record for key, record in value["evidence"].items()
            if record["kind"] not in {"workflow-log", "workflow-job"}
        }
        proposal = self._execute_assessed_repair(value, "product-or-tooling")
        self.assertEqual("current-workflow-break", proposal["repairPriority"]["kind"])

    def test_current_discovered_producer_passes_policy_grant_capacity_and_assignment(self) -> None:
        proposal = self._execute_assessed_repair(producer_workflow_snapshot(), "blocking-build")
        self.assertEqual("workflow-producer", proposal["evidenceBasis"])
        self.assertEqual("current-workflow-break", proposal["repairPriority"]["kind"])

    def test_reporter_markdown_source_pr_passes_default_policy_grant_capacity_and_assignment(self) -> None:
        value = producer_snapshot()
        source = value["evidence"]["issue:21"]["payload"]
        source["body"] += "\n**Pull Request:** [#50](https://github.com/microsoft/aspire/pull/50)"
        collect_repair_logs(value, ["src/Program.cs(1): error CS1002: ; expected"])
        collect_triggering_pull_request(value, source_text=source["body"])
        proposal = self._execute_assessed_repair(value, None)
        self.assertEqual("workflow-producer", proposal["evidenceBasis"])
        self.assertEqual("current-workflow-break", proposal["repairPriority"]["kind"])

    def test_collected_job_diagnostics_pass_policy_grant_capacity_and_assignment(self) -> None:
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "##[error]Unable to locate the browser executable",
            "##[error]Unable to locate the browser executable",
        ])
        proposal = self._execute_assessed_repair(value, "product-or-tooling")
        self.assertEqual("recurrent-ci-failure", proposal["repairPriority"]["kind"])

    def test_collected_triggering_pr_passes_policy_grant_capacity_and_assignment(self) -> None:
        value = collect_repair_logs(repair_snapshot(), [
            "Failed Demo.Tests.Fails [42 ms]", "Failed Demo.Tests.Fails [99 ms]",
        ])
        collect_triggering_pull_request(value)
        proposal = self._execute_assessed_repair(value, "flaky-test")
        self.assertEqual("unquarantined-test-instability", proposal["repairPriority"]["kind"])

    def test_unrelated_http_failures_cannot_reach_policy_grant_or_capacity(self) -> None:
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "##[error]runtime-archive download failed: HTTP 503",
            "##[error]browser-package download failed: HTTP 503",
        ])
        value["collectedAt"] = self.fixture.now.isoformat().replace("+00:00", "Z")
        self._set_assessed_proposals(assess(value)[3], evidence_round=0)
        self.assertEqual([], self._selection()["selectedActionIds"])
        self.assertIsNone(self._admission([]))
        with self.assertRaises(ValueError):
            override_category(value, "transient-infrastructure")

    def test_checked_in_policy_allows_tenth_start_but_not_eleventh_after_restart(self) -> None:
        tasks = self._history(["completed"] * 9)
        self.assertEqual(1, len(self._selection()["selectedActionIds"]))
        self.assertTrue(self._admission(tasks).permitted)
        self._history(["completed"] * 10)
        self.assertEqual([], self._selection()["selectedActionIds"])
        self.assertEqual([], self._selection()["selectedActionIds"])

    def test_terminal_releases_active_slot_without_refunding_daily_start(self) -> None:
        tasks = self._history(["completed", "queued", "in_progress"])
        self.assertTrue(self._admission(tasks).permitted)
        tasks = self._history(["completed"] * 8 + ["queued", "in_progress"])
        self.assertIsNone(self._admission(tasks))

    def test_old_unassociated_start_ages_out_daily_but_keeps_capacity_blocked(self) -> None:
        self._history(["completed"], age=timedelta(hours=25))
        fixture = self.fixture
        events = ActionEventStore(fixture.state_dir).events(repository=fixture.repository)
        events[-1].update(outcome="indeterminate", result={})
        (fixture.state_dir / "action-events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        self.assertEqual(3, len(self._selection()["selectedActionIds"]))
        blocked = self._admission([])
        self.assertFalse(blocked.permitted)
        self.assertIn("capacity_evidence_incomplete", blocked.blocked_by)

    def test_unavailable_owned_task_with_readable_bound_pr_does_not_release_capacity(self) -> None:
        tasks = self._history(["queued"], age=timedelta(hours=25))
        fixture = self.fixture
        events = ActionEventStore(fixture.state_dir).events(repository=fixture.repository)
        pull = DelegatedPullRequest(
            database_id=1000, global_id="PR_0", number=2000,
            state=PullRequestState.OPEN, is_draft=True, changed_files=0,
        )
        record, = derive_delegation_tracking(
            events=events, tasks=[normalize_agent_task(tasks[0])],
            pull_requests=[pull], task_pull_request_ids={"task-0": [1000]},
        )
        events.append({
            "eventType": "delegation-observed", "repository": fixture.repository,
            "actionId": record["actionId"], "record": record,
        })
        (fixture.state_dir / "action-events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        blocked = self._admission([], [{
            "id": 1000, "node_id": "PR_0", "number": 2000, "state": "open",
            "draft": True, "changed_files": 0, "merged": False, "merged_at": None,
            "head": {"sha": "a" * 40},
        }])
        self.assertFalse(blocked.permitted)
        self.assertIn("capacity_evidence_incomplete", blocked.blocked_by)

    def test_exact_24_hour_cutoff_is_excluded_but_newer_starts_are_not(self) -> None:
        tasks = self._history(["completed"] * 10, age=timedelta(hours=24))
        self.assertEqual(3, len(self._selection()["selectedActionIds"]))
        self.assertTrue(self._admission(tasks).permitted)
        self._history(["completed"] * 10, age=timedelta(hours=24) - timedelta(microseconds=1))
        self.assertEqual([], self._selection()["selectedActionIds"])

    def test_cycle_cap_survives_reselection_and_process_restart(self) -> None:
        self.assertEqual(3, len(self._selection()["selectedActionIds"]))
        self._history(["completed"] * 2, same_cycle=True)
        self.assertEqual(1, len(self._selection()["selectedActionIds"]))
        self._history(["completed"] * 3, same_cycle=True)
        self.assertEqual([], self._selection()["selectedActionIds"])
        self.assertEqual([], self._selection()["selectedActionIds"])

    def test_open_empty_drafts_consume_checked_in_pull_request_cap(self) -> None:
        tasks = self._history(["completed"] * 9)
        tasks[0]["artifacts"] = [{
            "type": "branch", "provider": "github",
            "data": {"head_ref": "copilot/repair", "base_ref": "main"},
        }]
        pulls = [{
            "id": 1000 + index, "node_id": f"PR_{index}", "number": 2000 + index,
            "state": "open", "draft": True, "changed_files": 0,
            "merged_at": None, "merged": False, "head": {"sha": "a" * 40},
        } for index in range(10)]
        self.assertTrue(self._admission(tasks, pulls[:9]).permitted)
        blocked = self._admission(tasks, pulls)
        self.assertFalse(blocked.permitted)
        self.assertIn("max_open_delegated_prs", blocked.blocked_by)

    def test_five_repair_priorities_precede_budgets_and_hard_blocks_precede_priority(self) -> None:
        proposals = self.fixture.proposals["proposals"]
        quarantine = copy.deepcopy(proposals[0])
        quarantine.update(
            actionId=f"{self.fixture.proposals['snapshotId']}:issue:6:delegate",
            issueNumber=6, issueUrl="https://github.com/microsoft/aspire/issues/6",
            idempotencyKey="issue:6:delegate", evidenceIds=["issue:6"],
        )
        proposals.append(quarantine)
        facts = [
            {"producer": "gh-aw-failure-issue"},
            {"repairEvidence": {"current": True, "category": "flaky-test", "recurrent": True}},
            {"repairEvidence": {"current": True, "category": "product-or-tooling", "recurrent": True}},
            {"repairEvidence": {"current": True, "category": "blocking-build"}},
            {"testMaintenance": {"state": "quarantined"}},
        ]
        for proposal, priority_facts in zip(proposals, facts):
            proposal.update(repairPriorityFacts=priority_facts, repairPriority=repair_priority(priority_facts))
        self.fixture._write_proposals()
        self.assertEqual([proposals[index]["actionId"] for index in (3, 2, 1)], self._selection()["selectedActionIds"])
        proposals[3]["executionEligibility"].update(eligible=False, ciLabels=[], blockingReasons=["missing-ci-label"])
        self.fixture.proposals["executionEligibility"] = {
            "status": "partially-eligible",
            "violations": [{"actionId": proposals[3]["actionId"], "blockingReasons": ["missing-ci-label"]}],
        }
        self.fixture._write_proposals()
        self.assertEqual([proposals[index]["actionId"] for index in (2, 1, 0)], self._selection()["selectedActionIds"])

    def test_forged_model_priority_is_rejected_before_selection(self) -> None:
        proposal = self.fixture.proposals["proposals"][0]
        proposal.update(repairPriorityFacts={"testMaintenance": {"state": "quarantined"}})
        proposal["repairPriority"] = repair_priority(proposal["repairPriorityFacts"])
        proposal["repairPriority"]["rank"] = 0
        self.fixture._write_proposals()
        with self.assertRaisesRegex(ValueError, "must match frozen routing facts"):
            self._selection()

    def test_copied_grant_replay_and_another_selection_cannot_refund_tenth_start(self) -> None:
        fixture = self.fixture
        tasks = self._history(["completed"] * 9)
        action_id, = self._selection()["selectedActionIds"]
        fixture._mint(action_id, production_delegation_policy_path=self.capacity_path)
        authorized = fixture._load(action_id, production_delegation_policy_path=self.capacity_path)
        proposal = authorized.proposal
        arguments = dict(
            action_id=action_id, chain_root=authorized.chain_root,
            operation="assign-copilot", target_kind="issue", target_number=proposal["issueNumber"],
            idempotency_key=proposal["idempotencyKey"], body_digest=None,
            expected_actor_login="radical", at=fixture.now,
        )
        event_store = ActionEventStore(
            fixture.state_dir, policy_budget_validator=CoordinatorPolicyBudgetValidator(fixture.store),
        )
        with event_store.transaction(authorized.grant, **arguments) as execution:
            self.assertEqual("execute", execution.reservation.mode)
            admitted = reserve_delegation_start(
                execution=execution, client=CapacityClient([tasks, []], []),
                repository=fixture.repository, limits=CapacityLimits(3, 10, 10, 100), now=fixture.now,
            )
            self.assertTrue(admitted.permitted)
            execution.append_terminal(result={
                "actionId": action_id, "outcome": "executed", "result": {"taskId": "tenth-task"},
            }, at=fixture.now)
        copied_grant = fixture.scratch / "copied-grant.json"
        copied_grant.write_bytes(fixture.output_path.read_bytes())
        fixture.output_path = copied_grant
        copied = fixture._load(action_id, production_delegation_policy_path=self.capacity_path)
        restarted_store = ActionEventStore(
            fixture.state_dir, policy_budget_validator=CoordinatorPolicyBudgetValidator(fixture.store),
        )
        with restarted_store.transaction(copied.grant, **arguments) as execution:
            self.assertEqual("terminal", execution.reservation.mode)
        self.assertEqual(10, len([
            event for event in restarted_store.events(repository=fixture.repository)
            if event["eventType"] == "delegation-baseline"
        ]))
        fixture.proposals["snapshotId"] += ":next"
        fixture._write_proposals()
        self.assertEqual([], self._selection()["selectedActionIds"])


if __name__ == "__main__":
    unittest.main()
