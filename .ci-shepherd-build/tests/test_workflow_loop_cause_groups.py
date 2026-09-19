from dataclasses import replace
from datetime import UTC, datetime
import itertools
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.workflow_loop.cause_groups import derive_cause
from ci_shepherd.workflow_loop.models import (
    ItemPhase, TaskState, ActionKind, FailureClassification, JudgmentDecision,
    JudgmentResult, RecommendedResponse, apply_classification_policy,
    judgment_request_to_json, parse_judgment_request,
    workflow_case_marker,
)
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from test_workflow_loop_state import _job, _run, _intent, NOW
from test_workflow_loop_models import _request
from test_workflow_loop_manager import _Reader, _Launcher, _Writer
from test_workflow_loop_reducer import _refresh, _task, _issue
from test_workflow_loop_writer import FakeActor
from ci_shepherd.workflow_loop.manager import WorkflowLoopManager, EffectMode
from ci_shepherd.workflow_loop.reducer import NextStep, reduce_item
from ci_shepherd.workflow_loop.writer import WorkflowWriter
from ci_shepherd.workflow_loop.shadow import prepare_shadow
from ci_shepherd.workflow_loop.reader import JobManifest, ManifestJob, IssueSearchResult


class _LeafReader(_Reader):
    def refresh_item(self, item, *, action=None):
        return replace(
            self.refresh, item_id=item.id, pre_write=action is not None,
            issue=replace(_issue(item.issue_number), marker=workflow_case_marker(
                item.repository, item.branch, item.workflow_id, item.workflow_path,
                item.cause_group_id,
            )) if item.issue_number else None,
            task=(
                replace(_task("completed"), task_id=item.task_id)
                if item.task_id else None
            ),
        )


class _MetadataLeafReader(_LeafReader):
    """Polling exposes inventory; only bounded detail reads expose logs."""

    def __init__(self, refresh):
        super().__init__(refresh)
        self.enriched = []

    def refresh_item(self, item, *, action=None):
        refresh = super().refresh_item(item, action=action)

        def metadata(run):
            return replace(run, jobs=tuple(replace(job, log_excerpt=None) for job in run.jobs))

        return replace(
            refresh,
            failure_run=metadata(refresh.failure_run),
            runs=tuple(metadata(run) for run in refresh.runs),
            task=(
                replace(_task("in_progress"), task_id=item.task_id)
                if item.task_id else None
            ),
        )

    def read_run_details(self, run, *, established_jobs=(), selected_log_jobs=None):
        self.enriched.append((run.run_id, run.attempt, selected_log_jobs))
        result = super().read_run_details(
            run, established_jobs=established_jobs, selected_log_jobs=selected_log_jobs,
        )
        return replace(
            result,
            run=replace(result.run, jobs=tuple(
                job if job.key in selected_log_jobs else replace(job, log_excerpt=None)
                for job in result.run.jobs
            )),
            logged_job_ids=tuple(
                job.job_id for job in result.run.jobs if job.key in selected_log_jobs
            ),
        )


class _LeafLauncher(_Launcher):
    def __init__(self, *args, classification=FailureClassification.DETERMINISTIC_TEST, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = {}
        self.classification = classification
        self.result_ready = True

    def prepare(self, reservation, request):
        self.requests[reservation.worker_id] = request
        return super().prepare(reservation, request)

    def observe(self, worker):
        self.request = self.requests[worker.worker_id]
        observation = super().observe(worker)
        result = replace(
            observation.judgment, in_scope_job_ids=(self.request.failed_jobs[0].job_id,),
            classification=self.classification, recommended_response=RecommendedResponse.REPAIR,
        )
        return replace(observation, judgment=apply_classification_policy(self.request, result))


class _LeafWriter(_Writer):
    def execute(self, request, result, **kwargs):
        # The real executor reserves after fresh ownership checks, not in core.
        if not self.store.reserve_cause_start(
            request.item_id, reserved_at=NOW, proposal=kwargs.get("propose_only", False),
        ):
            from ci_shepherd.workflow_loop.writer import WorkflowWriteResult
            return WorkflowWriteResult("capacity_wait", "Episode budget exhausted.")
        if kwargs.get("propose_only"):
            self.calls.append((request, result))
            from ci_shepherd.workflow_loop.writer import WorkflowWriteResult
            return WorkflowWriteResult("proposed", "Offline proposal", (f"proposal-{request.item_id}",))
        return replace(
            super().execute(request, result, **kwargs),
            task_id=f"task-{request.item_id}", issue_number=100 + request.item_id,
        )


class CauseSignatureTests(unittest.TestCase):
    def cause(self, text, *, step="Build", name="Build / Linux"):
        job = replace(_job(name=name), log_excerpt=text)
        return derive_cause(_run(jobs=(job,)), job, failed_steps=(step,))

    def test_exact_test_and_primary_diagnostic_ignore_transport_not_resources(self):
        a = self.cause(
            "2026-09-17T20:00:00Z Failed Aspire.Tests.Widget.Works [1 ms]\n"
            "2026-09-17T20:00:00Z Error Message:\n"
            "2026-09-17T20:00:00Z System.IO.IOException: Text file busy: '/repo/mock.sh'\n"
            "Stack Trace:\n at A.B()"
        )
        b = self.cause(
            "Failed Aspire.Tests.Widget.Works [20 ms]\nError Message:\n"
            "System.IO.IOException: Text file busy: '/repo/mock.sh'\nStack Trace:\n at C.D()",
            name="Tests / Windows",
        )
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.cause(
            "Failed Aspire.Tests.Other.Works [1 ms]\nError Message:\n"
            "System.IO.IOException: Text file busy: '/repo/mock.sh'"
        ))
        self.assertNotEqual(a, self.cause(
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\n"
            "System.IO.IOException: Text file busy: '/repo/other.sh'"
        ))

    def test_exact_compiler_and_resource_bearing_step_diagnostics(self):
        self.assertEqual(
            self.cause("##[error]src/App.cs(12,3): error CS1002: ; expected"),
            self.cause("src/App.cs(12,3): error CS1002: ; expected", name="Build / Windows"),
        )
        a = self.cause("##[error]HTTP 503 fetching https://feed.example/packages/sdk", step="Restore")
        self.assertIsNotNone(a)
        self.assertNotEqual(a, self.cause(
            "HTTP 503 fetching https://other.example/packages/sdk", step="Restore",
        ))
        self.assertNotEqual(a, self.cause(
            "HTTP 503 fetching https://feed.example/packages/sdk", step="Install",
        ))

    def test_resource_and_parameter_whitespace_is_part_of_the_exact_cause(self):
        for template in (
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\n"
            "System.IO.IOException: Text file busy: '/repo/mock{space}executable.sh'",
            'Failed Aspire.Tests.Widget.Works(path: "/repo/mock{space}executable.sh") [1 ms]\n'
            "Error Message:\nExpected: 1\nActual: 2",
            "src/mock{space}executable.cs(1): error CS1002: ; expected",
        ):
            with self.subTest(template=template):
                one = self.cause(template.format(space=" "))
                two = self.cause(template.format(space="  "))
                self.assertIsNotNone(one)
                self.assertIsNotNone(two)
                self.assertNotEqual(one, two)

    def test_missing_generic_ambiguous_and_model_prose_never_group(self):
        for text in (
            None, "", "Process completed with exit code 1.", "Timeout after 30 seconds",
            "Build failed", "error CS1002: ; expected", "HTTP 503 Service Unavailable",
            '{"cause":"same resource", "signature":"same failure"}',
            "The model says this is the same compiler problem.",
            "Failed Aspire.Tests.Widget.Works [1 ms]",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nTimeout after 30 seconds",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nCommand exited with code 1.",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nSystem.Exception: Exit code: 1",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nThe operation has timed out.",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nerror",
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\nExpected: 1\nActual: 2\n"
            "Failed Aspire.Tests.Other.Works [1 ms]\nError Message:\nExpected: 3\nActual: 4",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.cause(text))
        self.assertIsNone(self.cause("HTTP 503 fetching https://feed.example/sdk", step=""))


class CauseGroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = WorkflowLoopStore(self.path, repository="owner/repo", branch="main")
        self.store.initialize()

    def leaf(self, name, *, run_id=101, diagnostic="src/App.cs(12,3): error CS1002: ; expected"):
        job = replace(_job(900 + len(self.store.list_items()), name=name), log_excerpt=diagnostic)
        run = _run(run_id=run_id, jobs=(job,))
        item = self.store.upsert_leaf_failure(run, job.key, NOW)
        return self.store.record_cause(item.id, run, observed_at=NOW)

    def test_exact_group_leader_is_lexicographic_and_witnesses_survive_restart(self):
        z = self.leaf("Z / Linux")
        a = self.leaf("A / Windows")
        items = self.store.list_items()
        self.assertEqual({a.id}, {i.cause_leader_id for i in items})
        self.assertEqual(z.cause_group_id, a.cause_group_id)
        self.assertEqual(2, len(self.store.cause_witnesses(a.id)))
        self.leaf("A / Windows", run_id=102)
        reopened = WorkflowLoopStore(self.path, repository="owner/repo", branch="main")
        reopened.initialize()
        self.assertEqual({101, 102}, {w.run_id for w in reopened.cause_witnesses(a.id)})
        self.assertEqual(a.case_key, reopened.list_items()[1].case_key)

    def test_v7_groups_require_fresh_execution_evidence_after_migration(self):
        first = self.leaf("A")
        second = self.leaf("B")
        with closing(sqlite3.connect(self.path / "workflow-loop.sqlite3")) as connection:
            connection.execute("ALTER TABLE workflow_items DROP COLUMN cause_evidence_fingerprint")
            connection.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
            connection.commit()
        self.store.initialize()
        self.assertEqual(
            [(first.cause_group_id, first.id, None), (first.cause_group_id, first.id, None)],
            [(i.cause_group_id, i.cause_leader_id, i.cause_evidence_fingerprint)
             for i in self.store.list_items()],
        )
        refreshed = self.leaf("B")
        self.assertEqual(second.id, refreshed.id)
        self.assertEqual(refreshed.evidence_fingerprint, refreshed.cause_evidence_fingerprint)
        self.store.initialize()
        self.assertEqual(refreshed, self.store.list_items()[1])

    def test_generic_signatures_are_singletons(self):
        a = self.leaf("A", diagnostic="Process completed with exit code 1.")
        b = self.leaf("B", diagnostic="Process completed with exit code 1.")
        self.assertNotEqual(a.cause_group_id, b.cause_group_id)
        self.assertEqual((), self.store.cause_witnesses(a.id))

    def test_unknown_singleton_refines_to_first_structured_cause(self):
        unknown = self.leaf("A", diagnostic="Process completed with exit code 1.")
        structured = self.leaf(
            "A",
            run_id=102,
            diagnostic="src/App.cs(12,3): error CS1002: ; expected",
        )
        self.assertNotEqual(unknown.cause_group_id, structured.cause_group_id)
        self.assertIsNot(ItemPhase.NEEDS_ATTENTION, structured.phase)
        self.assertNotEqual("cause_conflict", structured.wait_reason)
        self.assertEqual({102}, {w.run_id for w in self.store.cause_witnesses(structured.id)})

    def test_owned_leader_never_retargets_and_conflicting_evidence_freezes_group(self):
        z = self.leaf("Z")
        self.assertTrue(self.store.reserve_cause_start(z.id, reserved_at=NOW))
        a = self.leaf("A")
        self.assertEqual(z.id, a.cause_leader_id)
        changed = self.leaf("A", diagnostic="src/Other.cs(1,2): error CS1002: ; expected")
        self.assertEqual(z.cause_group_id, changed.cause_group_id)
        self.assertEqual({"cause_conflict"}, {i.wait_reason for i in self.store.list_items()})
        self.assertFalse(self.store.reserve_cause_start(z.id, reserved_at=NOW))
        self.assertEqual(2, len(self.store.cause_witnesses(z.id)))

    def test_prepared_issue_counts_as_owned_before_any_remote_receipt(self):
        z = self.leaf("Z")
        self.assertTrue(self.store.prepare_action(
            _intent(z.id, z.episode, kind=ActionKind.CREATE_ISSUE), capacity_limit=2,
        ))
        a = self.leaf("A")
        self.assertEqual(z.id, a.cause_leader_id)
        self.assertFalse(self.store.reserve_cause_start(a.id, reserved_at=NOW))

    def test_conflict_prevents_invoking_a_prepared_effect_and_preserves_receipt(self):
        item = self.leaf("Z")
        intent = _intent(item.id, item.episode, kind=ActionKind.CREATE_ISSUE)
        self.assertTrue(self.store.prepare_action(intent, capacity_limit=2))
        self.leaf("Z", diagnostic="src/Changed.cs(1): error CS1002: ; expected")
        self.assertFalse(self.store.begin_action_invocation(
            intent.action_id, pass_id="pass", owner_id="owner", invoked_at=NOW,
        ))
        self.assertEqual("prepared", self.store.list_actions()[0].state.value)

    def test_null_failed_step_metadata_cannot_provide_a_step_signature(self):
        job = replace(_job(), log_excerpt="HTTP 503 fetching https://feed.example/sdk")
        run = _run(jobs=(job,))
        self.store.record_job_manifest(
            run, JobManifest(run, (ManifestJob(job, run.head_sha, None),), 1, True, (), 0),
            NOW, {job.job_id: "ambiguous_leaf"},
        )
        item = self.store.upsert_leaf_failure(run, job.key, NOW)
        grouped = self.store.record_cause(item.id, run, observed_at=NOW)
        self.assertEqual((), self.store.cause_witnesses(grouped.id))

    def test_concurrent_start_reservations_are_atomic_before_issue_creation(self):
        items = [self.leaf(f"Lane {i}", diagnostic=f"src/A{i}.cs(1): error CS1002: ; expected")
                 for i in range(13)]
        def reserve(item):
            reopened = WorkflowLoopStore(self.path, repository="owner/repo", branch="main")
            return reopened.reserve_cause_start(item.id, reserved_at=NOW)
        with ThreadPoolExecutor(max_workers=13) as pool:
            self.assertEqual(2, sum(pool.map(reserve, items)))
        self.assertEqual(2, len(self.store.list_cause_starts()))

    def test_two_already_owned_leaves_freeze_instead_of_merging_ownership(self):
        first = self.leaf("Z")
        self.store.update_item(
            replace(first, issue_number=17), history_event="owned", summary="Exact owner", detail={},
        )
        job = replace(_job(901, name="A"), log_excerpt="src/App.cs(12,3): error CS1002: ; expected")
        run = _run(jobs=(job,))
        second = self.store.upsert_leaf_failure(run, job.key, NOW)
        self.store.update_item(
            replace(second, issue_number=18), history_event="owned", summary="Different owner", detail={},
        )
        updated = self.store.record_cause(second.id, run, observed_at=NOW)
        self.assertNotEqual(first.cause_group_id, updated.cause_group_id)
        self.assertEqual({17, 18}, {i.issue_number for i in self.store.list_items()})
        self.assertEqual({"cause_conflict"}, {i.wait_reason for i in self.store.list_items()})

    def test_rejected_cause_evidence_does_not_expand_active_witness_scope(self):
        cause_g1 = "src/App.cs(12,3): error CS1002: ; expected"
        cause_g2 = "src/Other.cs(8,2): error CS0103: The name 'missing' does not exist"
        a = self.leaf("A", diagnostic=cause_g1)
        group_g1 = a.cause_group_id
        self.store.update_item(
            replace(a, issue_number=17),
            history_event="owned",
            summary="Cause G1 owns exact work.",
            detail={},
        )

        a_g2_job = replace(_job(901, name="A"), log_excerpt=cause_g2)
        a_g2_run = _run(run_id=102, jobs=(a_g2_job,))
        refreshed_a = self.store.upsert_leaf_failure(
            a_g2_run,
            a_g2_job.key,
            "2026-09-17T20:10:00Z",
        )
        rejected = self.store.record_cause(
            refreshed_a.id,
            a_g2_run,
            observed_at="2026-09-17T20:10:00Z",
        )
        self.assertEqual(group_g1, rejected.cause_group_id)
        self.assertEqual("cause_conflict", rejected.wait_reason)

        b_g2_job = replace(_job(902, name="B"), log_excerpt=cause_g2)
        b_g2_run = _run(run_id=103, jobs=(b_g2_job,))
        b = self.store.upsert_leaf_failure(
            b_g2_run,
            b_g2_job.key,
            "2026-09-17T20:20:00Z",
        )
        accepted = self.store.record_cause(
            b.id,
            b_g2_run,
            observed_at="2026-09-17T20:20:00Z",
        )
        witnesses = self.store.cause_witnesses(accepted.id)
        members = {
            item.case_key
            for item in self.store.list_items()
            if item.cause_group_id == accepted.cause_group_id
        }

        self.assertEqual({103}, {witness.run_id for witness in witnesses})
        self.assertEqual(members, {witness.leaf_case_key for witness in witnesses})
        self.assertEqual({accepted.case_key}, members)
        boundary = next(
            entry
            for entry in self.store.recent_history(rejected.id, limit=10)
            if entry.event == "cause-conflict"
        )
        self.assertEqual(
            accepted.cause_group_id,
            boundary.detail["observedGroupId"],
        )

    def test_new_episode_excludes_prior_execution_from_active_witnesses(self):
        diagnostic = "src/App.cs(12,3): error CS1002: ; expected"
        first = self.leaf("A", diagnostic=diagnostic)
        self.store.update_item(
            replace(
                first,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at="2026-09-17T20:10:00Z",
            ),
            history_event="recovered",
            summary="The first cause episode recovered.",
            detail={},
        )

        next_job = replace(_job(903, name="A"), log_excerpt=diagnostic)
        next_run = _run(run_id=103, jobs=(next_job,))
        next_episode = self.store.upsert_leaf_failure(
            next_run,
            next_job.key,
            "2026-09-17T20:20:00Z",
        )
        current = self.store.record_cause(
            next_episode.id,
            next_run,
            observed_at="2026-09-17T20:20:00Z",
        )

        self.assertEqual(2, current.episode)
        self.assertEqual({103}, {
            witness.run_id for witness in self.store.cause_witnesses(current.id)
        })
        with closing(sqlite3.connect(self.path / "workflow-loop.sqlite3")) as connection:
            history = connection.execute(
                "SELECT run_id FROM cause_witnesses "
                "WHERE item_id = ? ORDER BY run_id",
                (current.id,),
            ).fetchall()
        self.assertEqual([101, 103], [row[0] for row in history])

    def test_episode_departure_repairs_group_leadership_without_transferring_ownership(self):
        diagnostic = "src/App.cs(12,3): error CS1002: ; expected"
        a = self.leaf("A", diagnostic=diagnostic)
        b = self.leaf("B", diagnostic=diagnostic)
        b_run = _run(jobs=(
            replace(_job(901, name="B"), log_excerpt=diagnostic),
        ))
        follower = reduce_item(
            b,
            _refresh(item=b, failure_run=b_run, runs=(b_run,)),
            now=NOW,
        )
        self.assertIs(NextStep.WAIT_FOR_CHANGE, follower.next_step)
        self.store.update_item(
            follower.item,
            history_event=follower.history_event,
            summary=follower.summary,
            detail={},
        )
        self.store.update_item(
            replace(
                a,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at="2026-09-17T20:10:00Z",
            ),
            history_event="recovered",
            summary="The original leader recovered.",
            detail={},
        )
        next_a_job = replace(_job(902, name="A"), log_excerpt=diagnostic)
        next_a_run = _run(run_id=103, jobs=(next_a_job,))
        next_a = self.store.upsert_leaf_failure(
            next_a_run,
            next_a_job.key,
            "2026-09-17T20:20:00Z",
        )
        current_b = self.store.list_items()[1]

        self.assertEqual(2, next_a.episode)
        self.assertIsNone(next_a.cause_group_id)
        self.assertEqual(current_b.id, current_b.cause_leader_id)
        resumed = reduce_item(
            current_b,
            _refresh(item=current_b, failure_run=b_run, runs=(b_run,)),
            now="2026-09-17T20:21:00Z",
        )
        self.assertIs(NextStep.QUEUE_JUDGMENT, resumed.next_step)
        self.assertNotEqual("cause_group_follower", resumed.item.wait_reason)

        priority_a = self.leaf(
            "Priority A",
            diagnostic="src/Priority.cs(1): error CS1002: ; expected",
        )
        self.leaf(
            "Priority B",
            diagnostic="src/Priority.cs(1): error CS1002: ; expected",
        )
        priority_c = self.leaf(
            "Priority C",
            diagnostic="src/Priority.cs(1): error CS1002: ; expected",
        )
        self.store.update_item(
            replace(
                priority_c,
                task_id="task-owned-member",
                task_state=TaskState.IN_PROGRESS,
            ),
            history_event="task-running",
            summary="A non-leader group member owns live work.",
            detail={},
        )
        self.store.update_item(
            replace(
                priority_a,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=104,
                recovered_at="2026-09-17T20:30:00Z",
            ),
            history_event="recovered",
            summary="The unowned group leader recovered.",
            detail={},
        )
        next_priority_job = replace(
            _job(903, name="Priority A"),
            log_excerpt="src/Priority.cs(1): error CS1002: ; expected",
        )
        next_priority_run = _run(run_id=105, jobs=(next_priority_job,))
        self.store.upsert_leaf_failure(
            next_priority_run,
            next_priority_job.key,
            "2026-09-17T20:40:00Z",
        )
        priority_members = {
            item.case_key: item
            for item in self.store.list_items()
            if item.cause_group_id == priority_c.cause_group_id
        }
        self.assertEqual(
            {priority_c.id},
            {item.cause_leader_id for item in priority_members.values()},
        )

        only = self.leaf(
            "Only",
            diagnostic="src/Only.cs(1): error CS1002: ; expected",
        )
        only_group = only.cause_group_id
        self.store.update_item(
            replace(
                only,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=106,
                recovered_at="2026-09-17T20:50:00Z",
            ),
            history_event="recovered",
            summary="The singleton recovered.",
            detail={},
        )
        next_only_job = replace(
            _job(903, name="Only"),
            log_excerpt="src/Only.cs(1): error CS1002: ; expected",
        )
        next_only_run = _run(run_id=107, jobs=(next_only_job,))
        next_only = self.store.upsert_leaf_failure(
            next_only_run,
            next_only_job.key,
            "2026-09-17T21:00:00Z",
        )
        self.assertIsNone(next_only.cause_group_id)
        with closing(sqlite3.connect(self.path / "workflow-loop.sqlite3")) as connection:
            retained = connection.execute(
                "SELECT leader_id, frozen FROM cause_groups WHERE group_id = ?",
                (only_group,),
            ).fetchone()
        self.assertEqual((only.id, 0), retained)

        owned = self.leaf(
            "Owned A",
            diagnostic="src/Owned.cs(1): error CS1002: ; expected",
        )
        remaining = self.leaf(
            "Owned B",
            diagnostic="src/Owned.cs(1): error CS1002: ; expected",
        )
        owned = replace(
            owned,
            phase=ItemPhase.RECOVERED,
            task_id="task-live",
            task_state=TaskState.IN_PROGRESS,
            recovered_run_id=108,
            recovered_at="2026-09-17T21:10:00Z",
        )
        self.store.update_item(
            owned,
            history_event="recovered",
            summary="The owned leader recovered while its task remained live.",
            detail={},
        )
        active_before = self.store.active_item_ids()
        next_owned_job = replace(
            _job(904, name="Owned A"),
            log_excerpt="src/Owned.cs(1): error CS1002: ; expected",
        )
        next_owned_run = _run(run_id=109, jobs=(next_owned_job,))
        next_owned = self.store.upsert_leaf_failure(
            next_owned_run,
            next_owned_job.key,
            "2026-09-17T21:20:00Z",
        )
        current_remaining = next(
            item for item in self.store.list_items() if item.id == remaining.id
        )

        self.assertEqual("task-live", next_owned.task_id)
        self.assertEqual(active_before, self.store.active_item_ids())
        self.assertEqual(current_remaining.id, current_remaining.cause_leader_id)
        self.assertIs(ItemPhase.NEEDS_ATTENTION, current_remaining.phase)
        self.assertEqual("cause_conflict", current_remaining.wait_reason)
        self.assertIsNone(current_remaining.task_id)

    def test_witness_rejects_changed_sha_or_job_before_persistence(self):
        item = self.leaf("Z")
        job = replace(_job(name="Z"), log_excerpt="src/App.cs(12,3): error CS1002: ; expected")
        run = _run(jobs=(job,))
        for changed in (replace(run, head_sha="different"), replace(run, jobs=(replace(job, job_id=999),))):
            with self.subTest(run=changed), self.assertRaisesRegex(ValueError, "identity"):
                self.store.record_cause(item.id, changed, observed_at=NOW)
        self.assertEqual(1, len(self.store.cause_witnesses(item.id)))

    def test_episode_budget_counts_two_starts_not_active_tasks_and_is_durable(self):
        items = [self.leaf(f"Lane {i:02}", diagnostic=f"src/App{i}.cs(1): error CS1002: ; expected")
                 for i in range(13)]
        for index, item in enumerate(items):
            self.assertEqual(index < 2, self.store.reserve_cause_start(item.id, reserved_at=NOW))
            if index < 2:
                self.store.update_item(
                    replace(item, task_id=f"task-{index}", task_state=TaskState.COMPLETED,
                            phase=ItemPhase.NEEDS_ATTENTION),
                    history_event="task-completed", summary="No PR", detail={},
                )
        self.store.initialize()
        self.assertEqual(2, len(self.store.list_cause_starts()))
        self.assertFalse(self.store.reserve_cause_start(items[-1].id, reserved_at=NOW))
        same = self.leaf("Lane 00", run_id=102, diagnostic="src/App0.cs(1): error CS1002: ; expected")
        self.assertTrue(self.store.reserve_cause_start(same.id, reserved_at=NOW))
        newer = self.leaf("Lane 12", run_id=102, diagnostic="src/App12.cs(1): error CS1002: ; expected")
        self.assertTrue(self.store.reserve_cause_start(newer.id, reserved_at=NOW))
        self.assertEqual(3, len(self.store.list_cause_starts()))

    def test_external_policy_uses_frozen_exact_independent_run_witnesses(self):
        first = self.leaf("A")
        self.leaf("A", run_id=101)  # More jobs or attempts are not independent runs.
        run = _run(jobs=(replace(_job(name="A"), log_excerpt="src/App.cs(12,3): error CS1002: ; expected"),))
        request = replace(
            _request(), item_id=first.id, leaf_case_key=first.case_key,
            failure_run=run, failed_jobs=run.jobs,
            evidence_ids=("run:101:1", "job:101:1:900", "log:900"),
            cause_group_id=first.cause_group_id, cause_witnesses=self.store.cause_witnesses(first.id),
        )
        def result(req):
            return JudgmentResult(
                1, req.item_id, req.episode, req.evidence_fingerprint,
                JudgmentDecision.ASSIGN, "Recurrence and repo mitigation claimed by model",
                req.evidence_ids, (req.failed_jobs[0].job_id,), "Investigate exact recurrence.",
                FailureClassification.EXTERNAL_INFRA, RecommendedResponse.REPAIR,
            )
        self.assertEqual(
            RecommendedResponse.OBSERVE, apply_classification_policy(request, result(request)).recommended_response,
        )
        second = self.leaf("A", run_id=102)
        run = _run(run_id=102, jobs=(replace(_job(name="A"), job_id=901,
                    log_excerpt="src/App.cs(12,3): error CS1002: ; expected"),))
        request = replace(
            request, failure_run=run, failed_jobs=run.jobs,
            cause_witnesses=self.store.cause_witnesses(second.id),
            evidence_ids=("run:102:1", "job:102:1:901", "log:901"),
        )
        restored = parse_judgment_request(judgment_request_to_json(request))
        self.assertEqual(request, restored)
        self.assertEqual(
            RecommendedResponse.INVESTIGATE, apply_classification_policy(restored, result(restored)).recommended_response,
        )
        unrelated = replace(request, cause_witnesses=tuple(
            w for w in request.cause_witnesses if w.run_id != 102
        ))
        self.assertEqual(
            RecommendedResponse.OBSERVE, apply_classification_policy(unrelated, result(unrelated)).recommended_response,
        )


class CauseCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "state"
        self.store = WorkflowLoopStore(self.path, repository="owner/repo", branch="main")
        self.store.initialize()
        self.counter = itertools.count()

    def seed(self, count, *, same=False, run_id=101):
        jobs = tuple(replace(
            _job(900 + i, name=f"Lane {i:02}"),
            log_excerpt=f"src/App{0 if same else i}.cs(1): error CS1002: ; expected",
        ) for i in range(count))
        run = _run(run_id=run_id, jobs=jobs)
        for job in run.jobs:
            self.store.upsert_leaf_failure(run, job.key, NOW)
        return run

    def seed_exhausted_deferred_target(self, target_diagnostic):
        diagnostics = (
            "src/App0.cs(1): error CS1002: ; expected",
            "src/App1.cs(1): error CS1002: ; expected",
            target_diagnostic,
        )
        jobs = tuple(
            replace(
                _job(900 + index, name=f"Lane {index:02}"),
                log_excerpt=diagnostic,
            )
            for index, diagnostic in enumerate(diagnostics)
        )
        run = _run(jobs=jobs)
        for job in jobs:
            self.store.upsert_leaf_failure(run, job.key, NOW)
        first, second, target = self.store.list_items()
        for item in (first, second):
            item = self.store.record_cause(item.id, run, observed_at=NOW)
            self.assertTrue(
                self.store.reserve_cause_start(item.id, reserved_at=NOW)
            )
            self.store.update_item(
                replace(item, phase=ItemPhase.SUPERSEDED),
                history_event="completed",
                summary="Prior start completed.",
                detail={},
            )
        target = replace(
            target,
            phase=ItemPhase.OBSERVING_FAILURE,
            wait_reason="deferred_by_episode_budget",
        )
        self.store.update_item(
            target,
            history_event="deferred-by-episode-budget",
            summary="The episode budget is exhausted.",
            detail={},
        )
        return run, first, target

    def tick(
        self,
        launcher,
        reader,
        writer,
        mode=EffectMode.LIVE,
        *,
        now=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
    ):
        # Reopen on every pass: no in-memory budget or grouping state may be required.
        self.store = WorkflowLoopStore(self.path, repository="owner/repo", branch="main")
        self.store.initialize()
        launcher.store = self.store
        if isinstance(writer, _LeafWriter):
            writer.store = self.store
        result = WorkflowLoopManager(
            state_directory=self.path, repository="owner/repo", branch="main",
            store=self.store, reader=reader, launcher=launcher, writer=writer,
            clock=lambda: now,
            id_factory=lambda: f"pass-{next(self.counter)}",
        ).run_pass(mode=mode)
        self.assertEqual((), result.errors)
        self.assertLessEqual(len(self.store.active_item_ids()), 2)
        return result

    def real_writer_tick(self, launcher, reader, actor):
        return self.tick(launcher, reader, WorkflowWriter(
            store=self.store, reader=reader, actor=actor, repository="owner/repo", branch="main",
            clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC), active_item_limit=2,
        ))

    def test_owned_incoming_leaf_becomes_leader_without_starting_a_second_task(self):
        run = self.seed(2, same=True)
        first, second = self.store.list_items()
        self.store.record_cause(first.id, run, observed_at=NOW)
        self.store.update_item(
            replace(second, issue_number=17, task_id="existing", task_state=TaskState.IN_PROGRESS),
            history_event="owned", summary="Existing exact task", detail={},
        )
        self.store.record_cause(second.id, run, observed_at=NOW)
        reader = _MetadataLeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor(task_ids=("unexpected",))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        self.assertEqual([], actor.calls)
        first, second = self.store.list_items()
        self.assertEqual({second.id}, {i.cause_leader_id for i in (first, second)})
        self.assertEqual((17, "existing"), (second.issue_number, second.task_id))
        self.assertEqual((None, None), (first.issue_number, first.task_id))
        self.assertEqual((), self.store.list_cause_starts())

    def test_exhausted_episode_adopts_exact_owners_with_bounded_enrichment_and_no_worker(self):
        run = self.seed(6)
        first, second, copilot, human, ambiguous, unowned = self.store.list_items()
        run = replace(run, jobs=(*run.jobs[:-1],
            replace(run.jobs[-1], log_excerpt="Process completed with exit code 1.")))
        for item in (first, second):
            item = self.store.record_cause(item.id, run, observed_at=NOW)
            self.assertTrue(self.store.reserve_cause_start(item.id, reserved_at=NOW))
            self.store.update_item(replace(item, phase=ItemPhase.SUPERSEDED),
                history_event="completed", summary="Prior start completed.", detail={})
        self.assertEqual(frozenset(), self.store.active_item_ids())

        class OwnershipReader(_MetadataLeafReader):
            def __init__(self, refresh):
                super().__init__(refresh)
                self.searches = []

            def find_tracking_issue(inner, item):
                inner.searches.append(item)
                if item.id in (copilot.id, human.id):
                    issue = replace(_issue(100 + item.id),
                        copilot_assigned=item.id == copilot.id, human_assigned=item.id == human.id)
                    return IssueSearchResult("one", issue, (issue.number,), (), 1)
                if item.id == ambiguous.id:
                    return IssueSearchResult("ambiguous", None, (80, 81), (), 1)
                return IssueSearchResult("zero", None, (), (), 1)

            def refresh_item(inner, item, *, action=None):
                refresh = super().refresh_item(item, action=action)
                if item.external_owner is not None:
                    refresh = replace(refresh, issue=replace(refresh.issue,
                        human_assigned=item.external_owner == "human",
                        copilot_assigned=item.external_owner == "copilot"))
                return refresh

        reader = OwnershipReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor()
        for _ in range(5):
            before = len(reader.enriched)
            self.real_writer_tick(launcher, reader, actor)
            self.assertLessEqual(len(reader.enriched) - before, 2)
        items = {i.id: i for i in self.store.list_items()}
        self.assertEqual(ItemPhase.OBSERVING_EXTERNAL_REPAIR, items[copilot.id].phase)
        self.assertEqual(ItemPhase.WAITING_FOR_HUMAN, items[human.id].phase)
        self.assertEqual(ItemPhase.NEEDS_ATTENTION, items[ambiguous.id].phase)
        self.assertEqual("deferred_by_episode_budget", items[unowned.id].wait_reason)
        self.assertEqual({copilot.id, human.id, ambiguous.id, unowned.id}, {i.id for i in reader.searches})
        self.assertTrue(all(i.cause_group_id is not None for i in reader.searches))
        self.assertEqual(4, len(reader.enriched))
        self.assertEqual((), self.store.cause_witnesses(unowned.id))
        self.assertEqual((), self.store.list_workers())
        self.assertEqual(2, len(self.store.list_cause_starts()))
        self.assertEqual((), self.store.list_actions())
        self.assertEqual([], actor.calls)

    def test_stable_budget_deferred_leaf_does_not_repeat_progress_after_ownership_check(self):
        run = self.seed(3)
        first, second, deferred = self.store.list_items()
        for item in (first, second):
            item = self.store.record_cause(item.id, run, observed_at=NOW)
            self.assertTrue(self.store.reserve_cause_start(item.id, reserved_at=NOW))
            self.store.update_item(
                replace(item, phase=ItemPhase.SUPERSEDED),
                history_event="completed",
                summary="Prior start completed.",
                detail={},
            )
        deferred = self.store.record_cause(deferred.id, run, observed_at=NOW)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)

        first_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 1, tzinfo=UTC),
        )
        after_first = next(
            item for item in self.store.list_items()
            if item.id == deferred.id
        )
        history_after_first = len(self.store.recent_history(deferred.id))
        second_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
        )

        current = next(
            item for item in self.store.list_items()
            if item.id == deferred.id
        )
        self.assertEqual(1, first_pass.progressed_items)
        self.assertEqual(0, second_pass.progressed_items)
        self.assertEqual("deferred_by_episode_budget", current.wait_reason)
        self.assertEqual(
            after_first.last_progressed_at,
            current.last_progressed_at,
        )
        self.assertEqual("2026-09-17T20:02:00Z", current.last_checked_at)
        self.assertEqual(
            history_after_first,
            len(self.store.recent_history(deferred.id)),
            self.store.recent_history(deferred.id),
        )
        self.assertEqual((), self.store.list_workers())

    def test_budget_override_preserves_progress_for_already_deferred_leaf(self):
        run = self.seed(7)
        first, second, *unowned = self.store.list_items()
        for item in (first, second):
            item = self.store.record_cause(item.id, run, observed_at=NOW)
            self.assertTrue(self.store.reserve_cause_start(item.id, reserved_at=NOW))
            self.store.update_item(
                replace(item, phase=ItemPhase.SUPERSEDED),
                history_event="completed",
                summary="Prior start completed.",
                detail={},
            )
        target = replace(
            unowned[-1],
            phase=ItemPhase.OBSERVING_FAILURE,
            wait_reason="deferred_by_episode_budget",
        )
        self.store.update_item(
            target,
            history_event="deferred-by-episode-budget",
            summary="The episode budget is exhausted.",
            detail={},
        )
        initial_history_count = len(self.store.recent_history(target.id))
        reader = _MetadataLeafReader(
            replace(_refresh(), failure_run=run, runs=(run,))
        )
        launcher = _LeafLauncher(self.path, self.store)

        self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 1, tzinfo=UTC),
        )

        current = next(
            item for item in self.store.list_items()
            if item.id == target.id
        )
        self.assertIsNone(current.cause_group_id)
        self.assertEqual(target.last_progressed_at, current.last_progressed_at)
        self.assertEqual("2026-09-17T20:01:00Z", current.last_checked_at)
        self.assertEqual(
            initial_history_count,
            len(self.store.recent_history(target.id)),
            self.store.recent_history(target.id),
        )
        self.assertEqual((), self.store.list_workers())

    def test_budget_ownership_preparation_counts_new_follower_progress_once(self):
        run, leader, target = self.seed_exhausted_deferred_target(
            "src/App0.cs(1): error CS1002: ; expected"
        )
        reader = _MetadataLeafReader(
            replace(_refresh(), failure_run=run, runs=(run,))
        )
        launcher = _LeafLauncher(self.path, self.store)

        first_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 1, tzinfo=UTC),
        )
        after_first = next(
            item for item in self.store.list_items()
            if item.id == target.id
        )
        history_after_first = self.store.recent_history(target.id)
        second_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
        )
        current = next(
            item for item in self.store.list_items()
            if item.id == target.id
        )
        current_leader = next(
            item for item in self.store.list_items()
            if item.id == leader.id
        )

        self.assertEqual(1, first_pass.progressed_items)
        self.assertEqual(0, second_pass.progressed_items)
        self.assertEqual(current_leader.cause_group_id, current.cause_group_id)
        self.assertEqual(leader.id, current.cause_leader_id)
        self.assertEqual("cause_group_follower", current.wait_reason)
        self.assertEqual("2026-09-17T20:01:00Z", current.last_progressed_at)
        self.assertEqual("2026-09-17T20:02:00Z", current.last_checked_at)
        self.assertEqual(
            1,
            sum(entry.event == "cause-group-derived" for entry in history_after_first),
        )
        self.assertEqual(
            1,
            sum(entry.event == "cause-group-follower" for entry in history_after_first),
        )
        self.assertEqual(
            history_after_first,
            self.store.recent_history(target.id),
        )
        self.assertEqual((), self.store.list_workers())

    def test_budget_ownership_preparation_counts_new_singleton_progress_once(self):
        run, _, target = self.seed_exhausted_deferred_target(
            "Process completed with exit code 1."
        )
        reader = _MetadataLeafReader(
            replace(_refresh(), failure_run=run, runs=(run,))
        )
        launcher = _LeafLauncher(self.path, self.store)

        first_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 1, tzinfo=UTC),
        )
        after_first = next(
            item for item in self.store.list_items()
            if item.id == target.id
        )
        history_after_first = self.store.recent_history(target.id)
        second_pass = self.tick(
            launcher,
            reader,
            None,
            now=datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
        )
        current = next(
            item for item in self.store.list_items()
            if item.id == target.id
        )

        self.assertEqual(1, first_pass.progressed_items)
        self.assertEqual(0, second_pass.progressed_items)
        self.assertIsNotNone(current.cause_group_id)
        self.assertEqual(current.id, current.cause_leader_id)
        self.assertEqual("deferred_by_episode_budget", current.wait_reason)
        self.assertEqual("2026-09-17T20:01:00Z", current.last_progressed_at)
        self.assertEqual("2026-09-17T20:02:00Z", current.last_checked_at)
        self.assertEqual(
            1,
            sum(entry.event == "cause-group-derived" for entry in history_after_first),
        )
        self.assertEqual(
            1,
            sum(
                entry.event == "deferred-by-episode-budget"
                for entry in history_after_first
            ),
        )
        self.assertEqual(
            history_after_first,
            self.store.recent_history(target.id),
        )
        self.assertEqual((), self.store.list_workers())

    def test_metadata_only_follower_rechecks_new_execution_and_freezes_owned_conflict(self):
        self.assert_follower_conflict(run_id=102, attempt=1)

    def test_metadata_only_follower_rechecks_new_attempt_and_freezes_owned_conflict(self):
        self.assert_follower_conflict(run_id=101, attempt=2)

    def assert_follower_conflict(self, *, run_id, attempt):
        run = self.seed(2, same=True)
        reader = _MetadataLeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor(task_ids=("original", "unexpected"))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        first, second = self.store.list_items()
        self.assertEqual(first.id, second.cause_leader_id)
        self.assertEqual(["create_issue", "create_copilot_task"], [c[0] for c in actor.calls])
        self.assertEqual(2, reader.detail_calls)
        workers = len(self.store.list_workers())

        later = _run(run_id=run_id, attempt=attempt, jobs=(
            run.jobs[0],
            replace(run.jobs[1], log_excerpt="src/Changed.cs(1): error CS1002: ; expected"),
        ))
        reader.refresh = replace(reader.refresh, failure_run=later, runs=(later, run))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        self.assertEqual(
            [(101, 1, (run.jobs[0].key,)), (101, 1, (run.jobs[1].key,)),
             (run_id, attempt, (run.jobs[1].key,))],
            reader.enriched,
        )
        current = self.store.list_items()
        self.assertEqual({"cause_conflict"}, {i.wait_reason for i in current})
        self.assertEqual({ItemPhase.NEEDS_ATTENTION}, {i.phase for i in current})
        self.assertEqual({first.cause_group_id}, {i.cause_group_id for i in current})
        self.assertEqual("original", current[0].task_id)
        self.assertEqual(["create_issue", "create_copilot_task"], [c[0] for c in actor.calls])
        self.assertEqual(workers, len(self.store.list_workers()))

    def test_metadata_only_follower_rechecks_same_cause_once_per_execution(self):
        run = self.seed(2, same=True)
        reader = _MetadataLeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor(task_ids=("original", "unexpected"))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        workers = len(self.store.list_workers())
        later = _run(run_id=102, jobs=run.jobs)
        reader.refresh = replace(reader.refresh, failure_run=later, runs=(later, run))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        self.assertEqual(
            [(101, 1, (run.jobs[0].key,)), (101, 1, (run.jobs[1].key,)),
             (102, 1, (run.jobs[1].key,))],
            reader.enriched,
        )
        self.assertEqual(workers, len(self.store.list_workers()))
        self.assertEqual(["create_issue", "create_copilot_task"], [c[0] for c in actor.calls])
        first, second = self.store.list_items()
        self.assertEqual(first.id, second.cause_leader_id)
        self.assertEqual("cause_group_follower", second.wait_reason)
        self.assertEqual({101, 102}, {w.run_id for w in self.store.cause_witnesses(second.id)})

    def test_separately_owned_matching_leaves_freeze_without_writer_effects(self):
        run = self.seed(2, same=True)
        first, second = self.store.list_items()
        for item in (first, second):
            self.store.update_item(
                replace(item, issue_number=100 + item.id, task_id=f"existing-{item.id}",
                        task_state=TaskState.IN_PROGRESS),
                history_event="owned", summary="Separate exact work", detail={},
            )
            self.store.record_cause(item.id, run, observed_at=NOW)
        reader = _MetadataLeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor()
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        self.assertEqual([], actor.calls)
        current = self.store.list_items()
        self.assertEqual({"cause_conflict"}, {i.wait_reason for i in current})
        self.assertEqual({ItemPhase.NEEDS_ATTENTION}, {i.phase for i in current})
        self.assertEqual(2, len({i.cause_group_id for i in current}))
        self.assertEqual(
            [(100 + item.id, f"existing-{item.id}") for item in (first, second)],
            [(i.issue_number, i.task_id) for i in current],
        )

    def test_distinct_resource_whitespace_creates_separate_tasks(self):
        self.assert_whitespace_tasks(
            "Failed Aspire.Tests.Widget.Works [1 ms]\nError Message:\n"
            "System.IO.IOException: Text file busy: '/repo/mock{space}executable.sh'",
        )

    def test_distinct_parameter_whitespace_creates_separate_tasks(self):
        self.assert_whitespace_tasks(
            'Failed Aspire.Tests.Widget.Works(path: "/repo/mock{space}executable.sh") [1 ms]\n'
            "Error Message:\nExpected: 1\nActual: 2",
        )

    def assert_whitespace_tasks(self, template):
        run = self.seed(2)
        run = replace(run, jobs=tuple(
            replace(job, log_excerpt=template.format(space=" " * (index + 1)))
            for index, job in enumerate(run.jobs)
        ))
        reader = _MetadataLeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor(task_ids=("one-space", "two-spaces"))
        for _ in range(5):
            self.real_writer_tick(launcher, reader, actor)
        self.assertEqual(
            ["create_issue", "create_copilot_task"] * 2, [c[0] for c in actor.calls],
        )
        self.assertEqual(2, len({i.cause_group_id for i in self.store.list_items()}))
        self.assertEqual({"one-space", "two-spaces"}, {i.task_id for i in self.store.list_items()})
        self.assertEqual(2, reader.detail_calls)

    def test_thirteen_sequential_completions_start_only_two_then_new_run_gets_two(self):
        run = self.seed(13)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        writer = _LeafWriter()
        for _ in range(18):
            self.tick(launcher, reader, writer)
        self.assertEqual(2, len(writer.calls))
        self.assertEqual(2, len(self.store.list_cause_starts()))
        self.assertEqual(11, sum(
            item.wait_reason == "deferred_by_episode_budget" for item in self.store.list_items()
        ))
        later = self.seed(13, run_id=102)
        reader.refresh = replace(reader.refresh, failure_run=later, runs=(later, run))
        for _ in range(18):
            self.tick(launcher, reader, writer)
        self.assertEqual(4, len(writer.calls))
        self.assertEqual([101, 101, 102, 102], sorted(s["run_id"] for s in self.store.list_cause_starts()))

    def test_exact_followers_have_one_task_and_every_leaf_is_retained(self):
        run = self.seed(5, same=True)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        writer = _LeafWriter()
        for _ in range(12):
            self.tick(launcher, reader, writer)
        self.assertEqual(1, len(writer.calls))
        self.assertEqual(5, len(self.store.list_items()))
        self.assertEqual(1, len({item.cause_group_id for item in self.store.list_items()}))
        self.assertEqual(4, sum(item.wait_reason == "cause_group_follower" for item in self.store.list_items()))
        request, = launcher.requests.values()
        self.assertEqual(
            {item.case_key for item in self.store.list_items()},
            set(request.represented_leaf_keys),
        )

    def test_shadow_simulates_budget_without_canonical_reservations_or_invocations(self):
        run = self.seed(13)
        canonical = self.path
        self.path = self.root / "shadow"
        prepare_shadow(canonical, self.path, repository="owner/repo", branch="main", workflow_ids=None)
        launcher = _LeafLauncher(self.path, self.store)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        writer = _LeafWriter()
        for _ in range(18):
            self.tick(launcher, reader, writer, EffectMode.READ_ONLY)
        self.assertEqual(2, len({call[0].item_id for call in writer.calls}))
        self.assertEqual(2, len(self.store.list_cause_starts()))
        self.assertEqual((), self.store.list_actions())
        original = WorkflowLoopStore(canonical, repository="owner/repo", branch="main")
        self.assertEqual((), original.list_cause_starts())
        self.assertEqual({None}, {i.cause_group_id for i in original.list_items()})

    def test_real_writer_creates_only_two_issues_and_two_tasks(self):
        run = self.seed(13)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        actor = FakeActor(task_ids=("first", "second", "unexpected"))
        for _ in range(12):
            writer = WorkflowWriter(
                store=self.store, reader=reader, actor=actor, repository="owner/repo", branch="main",
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC), active_item_limit=2,
            )
            self.tick(launcher, reader, writer)
        self.assertEqual(
            ["create_issue", "create_copilot_task"] * 2, [call[0] for call in actor.calls],
        )
        self.assertEqual(4, len(self.store.list_actions()))

    def test_external_recurrence_flows_from_store_through_real_scenario_request(self):
        run = self.seed(1)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store, classification=FailureClassification.EXTERNAL_INFRA)
        writer = _LeafWriter()
        for _ in range(3):
            self.tick(launcher, reader, writer)
        self.assertEqual([], writer.calls)
        later = self.seed(1, run_id=102)
        reader.refresh = replace(reader.refresh, failure_run=later, runs=(later, run))
        for _ in range(4):
            self.tick(launcher, reader, writer)
        self.assertEqual(1, len(writer.calls))
        request, result, *_ = writer.calls[0]
        self.assertEqual({101, 102}, {w.run_id for w in request.cause_witnesses})
        self.assertEqual(RecommendedResponse.INVESTIGATE, result.recommended_response)

    def test_available_classifications_order_infra_before_test_with_leaf_ties(self):
        run = self.seed(2)
        reader = _LeafReader(replace(_refresh(), failure_run=run, runs=(run,)))
        launcher = _LeafLauncher(self.path, self.store)
        writer = _LeafWriter()
        self.tick(launcher, reader, writer)
        original = launcher.observe
        def classified(worker):
            observation = original(worker)
            classification = (
                FailureClassification.REPOSITORY_INFRA
                if observation.request.failed_jobs[0].key.name == "Lane 01"
                else FailureClassification.DETERMINISTIC_TEST
            )
            return replace(observation, judgment=replace(observation.judgment, classification=classification))
        launcher.observe = classified
        self.tick(launcher, reader, writer)
        self.assertEqual(["Lane 01", "Lane 00"], [call[0].failed_jobs[0].key.name for call in writer.calls])


if __name__ == "__main__":
    unittest.main()
