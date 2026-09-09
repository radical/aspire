from __future__ import annotations

import unittest
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ci_shepherd.investigations import (
    build_investigation_plan,
    record_investigation_result,
    record_investigation_session_event,
    read_investigation_session_events,
)
from test_investigations import _judgments, _prepared
from ci_shepherd import investigations
from ci_shepherd.investigation_worktrees import (
    cleanup_investigation_worktree,
    list_investigation_worktrees,
    provision_investigation_worktree,
)
from ci_shepherd.lifecycle import prepare_assessment
from test_production_decisions import recovery_snapshot
from ci_shepherd.investigation_scope import validate_scoped_result, validate_work_log


def _source_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    checkout.mkdir()
    for arguments in (
        ["init", "--quiet"],
        ["config", "user.name", "CI Shepherd"],
        ["config", "user.email", "ci-shepherd@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["remote", "add", "origin", "https://github.com/owner/repo.git"],
        ["commit", "--quiet", "--allow-empty", "-m", "Initial source"],
    ):
        subprocess.run(
            ["git", "--no-pager", "-C", str(checkout), *arguments],
            check=True, capture_output=True, text=True,
        )
    return checkout


def _source_request(checkout: Path) -> dict:
    prepared = _prepared()
    revision = subprocess.run(
        ["git", "--no-pager", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    prepared["sourceRevision"] = revision
    prepared["issues"][0]["sourceRevision"] = revision
    return build_investigation_plan(prepared, _judgments(), [])["requests"][0]


def _owned_worker(root: Path, source: Path) -> tuple[Path, dict, Path]:
    request = _source_request(source)
    state = root / "state"
    record = provision_investigation_worktree(
        state, request, source_checkout=source, attempt=1,
        recorded_at="2026-09-08T12:00:00Z", managed_root=root / "workers",
    )
    return state, request, Path(record["checkoutPath"])


def _evidence_result() -> dict:
    return {
        "outcome": "needs-evidence", "summary": "The supplied log is unavailable.",
        "evidenceIds": ["issue:21"], "missingEvidence": ["failure log"],
        "reassessWhen": "When the failure log is available.", "fixHandoff": None,
        "workLog": [{"kind": "evidence", "evidenceId": "issue:21", "finding": "The failure lacks a diagnostic log."}],
    }


class InvestigationScopeTests(unittest.TestCase):
    def test_work_log_accepts_attempt_scoped_jobs_without_widening_other_routes(self) -> None:
        with TemporaryDirectory() as directory:
            checkout = _source_checkout(Path(directory))
            request = _source_request(checkout)
            entry = {
                "kind": "github-get",
                "url": "https://api.github.com/repos/owner/repo/actions/runs/210/attempts/1/jobs?per_page=100",
                "finding": "Read the failing jobs from the recorded run attempt.",
            }
            self.assertEqual([entry], validate_work_log(request, [entry], checkout, []))
            for suffix in ("attempts/0/jobs", "attempts/1/rerun", "attempts/1/../../secrets"):
                with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "endpoint scope"):
                    validate_work_log(request, [{
                        **entry, "url": f"https://api.github.com/repos/owner/repo/actions/runs/210/{suffix}",
                    }], checkout, [])
            with self.assertRaisesRegex(ValueError, "fields do not match"):
                validate_work_log(request, [{
                    "kind": "evidence", "evidenceId": "issue:21",
                    "path": "not-an-evidence-field", "finding": "Read the supplied evidence.",
                }], checkout, [])

    def test_scoped_result_rejects_silent_unknown_or_malformed_fields(self) -> None:
        result = {
            "outcome": "inconclusive", "summary": "Some context is missing.",
            "evidenceIds": ["issue:21"], "missingEvidence": [], "reassessWhen": "New evidence.",
            "fixHandoff": None, "workLog": [],
        }
        for field, value in (
            ("unexpected", True), ("missingEvidence", "missing"), ("summary", " " * 4001),
            ("fixHandoff", {"problem": "Missing source."}),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_scoped_result({**result, field: value})

    def test_source_pin_cannot_diverge_from_fingerprinted_issue(self) -> None:
        for issue_revision in (None, "b" * 40):
            prepared = _prepared()
            prepared["sourceRevision"] = "a" * 40
            if issue_revision:
                prepared["issues"][0]["sourceRevision"] = issue_revision
            with self.subTest(issue_revision=issue_revision), self.assertRaisesRegex(ValueError, "source revision"):
                build_investigation_plan(prepared, _judgments(), [])

    def test_discovered_source_is_recorded_without_becoming_frozen_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = _source_checkout(root)
            source = checkout / "example.py"
            source.write_text("def example():\n    return 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "--no-pager", "-C", str(checkout), "add", "--", "example.py"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "--no-pager", "-C", str(checkout), "-c", "user.name=Test",
                 "-c", "user.email=test@example.invalid", "commit", "-qm", "Add source"],
                check=True, capture_output=True,
            )
            state, request, checkout = _owned_worker(root, checkout)
            record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="worker-one", checkout=checkout,
            )
            result = {
                "outcome": "inconclusive", "summary": "The implementation returns a constant.",
                "evidenceIds": ["issue:21"], "missingEvidence": ["expected behavior"],
                "reassessWhen": "When expected behavior is clarified.", "fixHandoff": None,
                "workLog": [{"kind": "source", "path": "example.py", "startLine": 1,
                             "endLine": 2, "finding": "The function returns 1."}],
            }
            recorded = record_investigation_result(
                state, request, result, recorded_at="2026-09-08T12:01:00Z",
                session_id="worker-one", checkout=checkout,
            )
            self.assertEqual(["issue:21"], recorded["evidenceIds"])
            self.assertEqual(result["workLog"], recorded["workLog"])
            self.assertEqual(request["sourceRevision"], recorded["sourceRevision"])

    def test_cloud_investigation_does_not_require_a_local_worker(self) -> None:
        prepared = _prepared()
        prepared["sourceRevision"] = "a" * 40
        prepared["issues"][0]["sourceRevision"] = "a" * 40
        judgments = _judgments()
        judgments["issues"][0]["category"] = "product-or-tooling"
        judgments["issues"][0]["recommendations"][0]["disposition"] = "delegate-copilot"
        judgments["issues"][0]["recommendations"][0]["confidence"] = "medium"
        self.assertEqual([], build_investigation_plan(prepared, judgments, [])["requests"])

    def test_work_log_rejects_out_of_scope_discovery_or_unapproved_commands(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = _source_checkout(root)
            request = _source_request(checkout)
            invalid_entries = [
                {"kind": "source", "path": "../secret", "startLine": 1, "endLine": 1, "finding": "found"},
                {"kind": "source", "path": ".git/config", "startLine": 1, "endLine": 1, "finding": "found"},
                {"kind": "source", "path": "missing.cs", "startLine": 1, "endLine": 1, "finding": "found"},
                {"kind": "github-get", "url": "https://api.github.com/repos/other/repo/issues/21", "finding": "found"},
                {"kind": "github-get", "url": "https://api.github.com/search/issues?q=token", "finding": "found"},
                {"kind": "github-get", "url": "https://api.github.com/repos/owner/repo/issues/21/../../secrets", "finding": "found"},
                {"kind": "command", "argv": ["unapproved"], "exitCode": 0, "output": "ok", "finding": "found"},
            ]
            for entry in invalid_entries:
                with self.subTest(entry=entry), self.assertRaises(ValueError):
                    validate_work_log(request, [entry], checkout, [])
            with self.assertRaisesRegex(ValueError, "budget"):
                validate_work_log(request, [
                    {"kind": "github-get", "url": "https://api.github.com/repos/owner/repo/issues/21", "finding": "found"}
                    for _ in range(13)
                ], checkout, [])

    def test_explicit_reproduction_records_actual_output_and_replays_without_another_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = _source_checkout(root)
            state, request, checkout = _owned_worker(root, checkout)
            command = [sys.executable, "-c", "print('reproduction output')"]
            record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="worker-one", checkout=checkout, reproduction_commands=[command],
            )
            completed = subprocess.run(command, check=True, capture_output=True, text=True, cwd=checkout)
            result = {
                "outcome": "inconclusive", "summary": "The explicitly permitted command completed.",
                "evidenceIds": ["issue:21"], "missingEvidence": ["original failure environment"],
                "reassessWhen": "When the original failure environment is available.", "fixHandoff": None,
                "workLog": [{"kind": "command", "argv": command, "exitCode": completed.returncode,
                             "output": completed.stdout, "finding": "Command output was captured."}],
            }
            first = record_investigation_result(
                state, request, result, recorded_at="2026-09-08T12:01:00Z",
                session_id="worker-one", checkout=checkout,
            )
            replay = record_investigation_result(
                state, request, result, recorded_at="2026-09-08T12:02:00Z",
                session_id="worker-one", checkout=checkout,
            )
            self.assertEqual(first, replay)
            self.assertEqual("reproduction output\n", first["workLog"][0]["output"])
            self.assertEqual([command], read_investigation_session_events(state)[-1]["reproductionCommands"])
            self.assertEqual([command], first["reproductionCommands"])

    def test_scoped_session_rejects_a_different_source_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = _source_checkout(root)
            state, _, checkout = _owned_worker(root, checkout)
            prepared = _prepared()
            prepared["sourceRevision"] = "a" * 40
            prepared["issues"][0]["sourceRevision"] = "a" * 40
            request, = build_investigation_plan(prepared, _judgments(), [])["requests"]

            with self.assertRaisesRegex(ValueError, "source revision"):
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="worker-one", checkout=checkout,
                )

    def test_preparation_preserves_source_revision_for_scoped_investigations(self) -> None:
        snapshot = recovery_snapshot()
        snapshot["sourceRevision"] = "a" * 40

        prepared = prepare_assessment(snapshot)

        self.assertEqual("a" * 40, prepared["sourceRevision"])
        self.assertEqual("a" * 40, prepared["issues"][0]["sourceRevision"])

    def test_scoped_result_requires_a_record_of_actual_work(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = _source_checkout(root)
            state, request, checkout = _owned_worker(root, checkout)
            record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="worker-one", checkout=checkout,
            )
            result = {
                "outcome": "needs-evidence", "summary": "The log is unavailable.",
                "evidenceIds": ["issue:21"], "missingEvidence": ["failure log"],
                "reassessWhen": "When the failure log is available.", "fixHandoff": None,
            }
            with self.assertRaisesRegex(ValueError, "workLog"):
                record_investigation_result(
                    state, request, result, recorded_at="2026-09-08T12:01:00Z",
                    session_id="worker-one", checkout=checkout,
                )

    def test_source_question_can_inspect_pinned_repository_without_source_in_packet(self) -> None:
        prepared = _prepared()
        prepared["sourceRevision"] = "a" * 40
        prepared["issues"][0]["sourceRevision"] = prepared["sourceRevision"]
        judgments = _judgments()
        judgments["issues"][0]["recommendations"][0]["summary"] = (
            "Verify the quarantine label against current test source."
        )

        request, = build_investigation_plan(prepared, judgments, [])["requests"]

        self.assertEqual("a" * 40, request["sourceRevision"])
        self.assertEqual({
            "sourceRevision": "a" * 40,
            "maxSourceFiles": 40,
            "maxReadOnlyRequests": 12,
            "reproductionCommands": [],
        }, request["investigationScope"])
        self.assertIn("Search and read tracked source", request["workerPrompt"])
        self.assertIn("workLog", request["workerPrompt"])
        self.assertIn("not permission to mutate GitHub", request["workerPrompt"])

    def test_dirty_coordinator_starts_owned_worker_but_unregistered_source_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = _source_checkout(root)
            state, request, checkout = _owned_worker(root, source)
            with self.assertRaisesRegex(ValueError, "owned|registry"):
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="unregistered-worker", checkout=source,
                )
            (source / "caller-scratch").write_text("preserve")
            started = record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="owned-worker", checkout=checkout,
            )
            allocation, = list_investigation_worktrees(state)
            self.assertEqual("bound", allocation["state"])
            self.assertEqual(allocation["ownershipId"], started["worktreeOwnershipId"])
            self.assertEqual(str(checkout), started["checkoutPath"])
            self.assertEqual("preserve", (source / "caller-scratch").read_text())

    def test_start_replay_recovers_bound_before_started_without_widening_command_grants(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, request, checkout = _owned_worker(root, _source_checkout(root))
            append = investigations.append_jsonl_rows
            command = [sys.executable, "-c", "print('allowed')"]

            def interrupt_start(path, rows):
                if path.name == "investigation-sessions.jsonl":
                    self.assertEqual("bound", list_investigation_worktrees(state)[0]["state"])
                    raise KeyboardInterrupt("after bind, before started")
                return append(path, rows)

            with patch.object(investigations, "append_jsonl_rows", side_effect=interrupt_start):
                with self.assertRaises(KeyboardInterrupt):
                    record_investigation_session_event(
                        state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                        session_id="worker", checkout=checkout, reproduction_commands=[command],
                    )
            started = record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:01:00Z",
                session_id="worker", checkout=checkout, reproduction_commands=[command],
            )
            replay = record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:02:00Z",
                session_id="worker", checkout=checkout, reproduction_commands=[command],
            )
            self.assertEqual(started, replay)
            self.assertEqual(1, len(read_investigation_session_events(state)))
            with self.assertRaisesRegex(ValueError, "already|replay|authorization"):
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:03:00Z",
                    session_id="worker", checkout=checkout, reproduction_commands=[],
                )

    def test_result_replay_repairs_both_terminal_crash_windows_and_survives_cleanup(self) -> None:
        for boundary in ("session", "registry"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory)
                state, request, checkout = _owned_worker(root, _source_checkout(root))
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="worker", checkout=checkout,
                )
                append = investigations.append_jsonl_rows

                def interrupt_terminal(path, rows):
                    if path.name == "investigation-sessions.jsonl" and rows[0]["status"] == "completed":
                        raise KeyboardInterrupt("after result, before completed session")
                    return append(path, rows)

                interruption = (
                    patch.object(investigations, "append_jsonl_rows", side_effect=interrupt_terminal)
                    if boundary == "session" else
                    patch.object(investigations, "finish_investigation_worktree", side_effect=KeyboardInterrupt)
                )
                with interruption, self.assertRaises(KeyboardInterrupt):
                    record_investigation_result(
                        state, request, _evidence_result(), recorded_at="2026-09-08T12:01:00Z",
                        session_id="worker", checkout=checkout,
                    )
                first = record_investigation_result(
                    state, request, _evidence_result(), recorded_at="2026-09-08T12:02:00Z",
                    session_id="worker", checkout=checkout,
                )
                self.assertEqual("completed", read_investigation_session_events(state)[-1]["status"])
                allocation, = list_investigation_worktrees(state)
                self.assertEqual("terminal", allocation["state"])
                self.assertFalse(allocation["workerStopped"])
                with self.assertRaisesRegex(ValueError, "stopped"):
                    cleanup_investigation_worktree(
                        state, request, checkout=checkout, session_id="worker", recorded_at="2026-09-08T12:03:00Z",
                    )
                cleanup_investigation_worktree(
                    state, request, checkout=checkout, session_id="worker", recorded_at="2026-09-08T12:03:00Z",
                    confirm_worker_stopped=True,
                )
                replay = record_investigation_result(
                    state, request, _evidence_result(), recorded_at="2026-09-08T12:04:00Z",
                    session_id="worker", checkout=checkout,
                )
                self.assertEqual(first, replay)
                self.assertEqual(2, len(read_investigation_session_events(state)))

    def test_dirty_worker_fault_and_stopped_abandonment_preserve_ownership(self) -> None:
        for status in ("failed", "abandoned"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                state, request, checkout = _owned_worker(root, _source_checkout(root))
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="worker", checkout=checkout,
                )
                (checkout / "worker-output").write_text("retain")
                terminal = record_investigation_session_event(
                    state, request, status=status, recorded_at="2026-09-08T13:01:00Z",
                    session_id="worker", checkout=checkout, failure_reason="Worker did not return valid JSON.",
                    confirm_worker_stopped=status == "abandoned",
                )
                replay = record_investigation_session_event(
                    state, request, status=status, recorded_at="2026-09-08T13:02:00Z",
                    session_id="worker", checkout=checkout, failure_reason="Worker did not return valid JSON.",
                    confirm_worker_stopped=status == "abandoned",
                )
                self.assertEqual(terminal, replay)
                allocation, = list_investigation_worktrees(state)
                self.assertEqual("terminal", allocation["state"])
                self.assertEqual(status, allocation["terminalStatus"])
                self.assertEqual(allocation["ownershipId"], terminal["worktreeOwnershipId"])
                self.assertEqual(str(checkout), terminal["checkoutPath"])
                with self.assertRaisesRegex(ValueError, "clean"):
                    cleanup_investigation_worktree(
                        state, request, checkout=checkout, session_id="worker",
                        recorded_at="2026-09-08T13:03:00Z", confirm_worker_stopped=True,
                    )
                self.assertEqual("retain", (checkout / "worker-output").read_text())

    def test_fault_replay_repairs_registry_after_missing_or_replaced_worker_source(self) -> None:
        for replacement in ("missing", "symlink"):
            with self.subTest(replacement=replacement), TemporaryDirectory() as directory:
                root = Path(directory)
                state, request, checkout = _owned_worker(root, _source_checkout(root))
                record_investigation_session_event(
                    state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="worker", checkout=checkout,
                )
                checkout.rename(root / "retained-source")
                if replacement == "symlink":
                    checkout.symlink_to(root / "retained-source", target_is_directory=True)
                with patch.object(investigations, "finish_investigation_worktree", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        record_investigation_session_event(
                            state, request, status="failed", recorded_at="2026-09-08T12:01:00Z",
                            session_id="worker", failure_reason="Worker source was removed or replaced.",
                        )
                replay = record_investigation_session_event(
                    state, request, status="failed", recorded_at="2026-09-08T12:02:00Z",
                    session_id="worker", failure_reason="Worker source was removed or replaced.",
                )
                self.assertEqual("failed", replay["status"])
                self.assertEqual(str(checkout), replay["checkoutPath"])
                self.assertEqual("terminal", list_investigation_worktrees(state)[0]["state"])
                self.assertEqual(2, len(read_investigation_session_events(state)))
                with self.assertRaises(ValueError):
                    cleanup_investigation_worktree(
                        state, request, checkout=checkout, session_id="worker",
                        recorded_at="2026-09-08T12:03:00Z", confirm_worker_stopped=True,
                    )
                self.assertTrue((root / "retained-source").exists())

    def test_scoped_abandonment_requires_stopped_assertion_and_one_hour(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, request, checkout = _owned_worker(root, _source_checkout(root))
            record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="worker", checkout=checkout,
            )
            for time, stopped, message in (
                ("2026-09-08T13:01:00Z", False, "stopped"),
                ("2026-09-08T12:01:00Z", True, "one-hour"),
            ):
                with self.subTest(time=time), self.assertRaisesRegex(ValueError, message):
                    record_investigation_session_event(
                        state, request, status="abandoned", recorded_at=time,
                        session_id="worker", checkout=checkout, failure_reason="Worker unavailable.",
                        confirm_worker_stopped=stopped,
                    )
            self.assertEqual("started", read_investigation_session_events(state)[-1]["status"])
            self.assertEqual("bound", list_investigation_worktrees(state)[0]["state"])

    def test_new_result_rechecks_ownership_and_rejects_ungranted_reproduction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, request, checkout = _owned_worker(root, _source_checkout(root))
            record_investigation_session_event(
                state, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                session_id="worker", checkout=checkout,
            )
            result = _evidence_result()
            result["workLog"] = [{
                "kind": "command", "argv": ["unapproved"], "exitCode": 0,
                "output": "claimed output", "finding": "Unapproved command was claimed.",
            }]
            with self.assertRaisesRegex(ValueError, "authorized"):
                record_investigation_result(
                    state, request, result, recorded_at="2026-09-08T12:01:00Z",
                    session_id="worker", checkout=checkout,
                )
            subprocess.run(
                ["git", "--no-pager", "-C", str(checkout), "worktree", "unlock", str(checkout)],
                check=True, capture_output=True,
            )
            with self.assertRaisesRegex(ValueError, "ownership"):
                record_investigation_result(
                    state, request, _evidence_result(), recorded_at="2026-09-08T12:02:00Z",
                    session_id="worker", checkout=checkout,
                )
            self.assertFalse((state / "ledgers/investigation-results.jsonl").exists())
            self.assertEqual("started", read_investigation_session_events(state)[-1]["status"])

    def test_canonical_cli_rejects_fresh_legacy_start_but_records_owned_source_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = _source_checkout(root)
            state, request, checkout = _owned_worker(root, source)
            plan = root / "plan.json"
            legacy = build_investigation_plan(_prepared(), _judgments(), [])
            plan.write_text(json.dumps(legacy))
            session_script = Path("scripts/investigation_session.py").resolve()
            result_script = Path("scripts/investigation_result.py").resolve()

            def invoke(script, *args):
                return subprocess.run(
                    [sys.executable, str(script), "--state-dir", str(state), "--plan", str(plan),
                     "--investigation-id", request["investigationId"], "--session-id", "worker",
                     "--recorded-at", "2026-09-08T12:01:00Z", "--checkout", str(checkout), *args],
                    capture_output=True, text=True, check=False,
                )

            legacy["requests"][0]["investigationId"] = request["investigationId"]
            plan.write_text(json.dumps(legacy))
            denied = invoke(session_script, "--status", "started")
            self.assertEqual(2, denied.returncode, denied.stderr)
            self.assertIn("source", denied.stderr)
            self.assertFalse((state / "ledgers/investigation-sessions.jsonl").exists())
            plan.write_text(json.dumps({"repository": request["repository"], "requests": [request]}))
            started = invoke(session_script, "--status", "started")
            self.assertEqual(0, started.returncode, started.stderr)
            result = root / "result.json"
            result.write_text(json.dumps(_evidence_result()))
            completed = invoke(result_script, "--result", str(result))
            self.assertEqual(0, completed.returncode, completed.stderr)
            plan.write_text(json.dumps({
                "repository": request["repository"], "requests": [],
                "reusedInvestigationIds": [request["investigationId"]],
            }))
            replay = invoke(result_script, "--result", str(result))
            self.assertEqual(0, replay.returncode, replay.stderr)
            self.assertEqual(json.loads(completed.stdout), json.loads(replay.stdout))
            plan.write_text(json.dumps({
                "repository": request["repository"],
                "requests": [{**request, "snapshotId": "a-later-cycle"}],
            }))
            replay = invoke(result_script, "--result", str(result))
            self.assertEqual(0, replay.returncode, replay.stderr)
            self.assertEqual(json.loads(completed.stdout), json.loads(replay.stdout))

    def test_scoped_registration_rejects_state_symlinks_before_any_ledger_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, request, checkout = _owned_worker(root, _source_checkout(root))
            alias = root / "state-alias"
            alias.symlink_to(state, target_is_directory=True)
            before = sorted(path.name for path in (state / "ledgers").iterdir())
            with self.assertRaisesRegex(ValueError, "symlink"):
                record_investigation_session_event(
                    alias, request, status="started", recorded_at="2026-09-08T12:00:00Z",
                    session_id="worker", checkout=checkout,
                )
            self.assertEqual(before, sorted(path.name for path in (state / "ledgers").iterdir()))


if __name__ == "__main__":
    unittest.main()
