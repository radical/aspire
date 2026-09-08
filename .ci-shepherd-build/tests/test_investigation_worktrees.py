from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ci_shepherd import investigation_worktrees as worktrees


NOW = "2026-09-08T18:00:00Z"


def git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-pager", "-C", str(checkout), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class InvestigationWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path("tests/.tmp")
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.make_repository("source")
        self.state = self.root / "state"
        self.managed = self.root / "managed"
        self.revision = git(self.source, "rev-parse", "HEAD")
        self.request = {
            "repository": "owner/repo",
            "investigationId": "investigation:issue:21:source",
            "sourceEvidenceFingerprint": "fnv1a64:0123456789abcdef",
            "sourceRevision": self.revision,
            "investigationScope": {"sourceRevision": self.revision},
            "question": "Which source line explains the failure?",
        }

    def make_repository(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        git(path, "init", "-q")
        git(path, "config", "user.name", "Worktree test")
        git(path, "config", "user.email", "worktree@example.invalid")
        git(path, "config", "commit.gpgsign", "false")
        git(path, "remote", "add", "origin", "https://github.com/owner/repo.git")
        (path / "source.txt").write_text("original source\n", encoding="utf-8")
        (path / ".gitignore").write_text("ignored-output/\n", encoding="utf-8")
        git(path, "add", "--", "source.txt", ".gitignore")
        git(path, "commit", "-qm", "Initial test source")
        return path

    def provision(self, request: dict | None = None, attempt: int = 1, **kwargs) -> dict:
        return worktrees.provision_investigation_worktree(
            self.state, request or self.request, source_checkout=self.source,
            attempt=attempt, recorded_at=NOW, managed_root=self.managed, **kwargs,
        )

    def test_dirty_coordinator_produces_clean_detached_exact_revision(self) -> None:
        (self.source / "source.txt").write_text("local edits\n", encoding="utf-8")
        (self.source / "caller-scratch").write_text("preserve\n", encoding="utf-8")
        before = git(self.source, "status", "--porcelain")
        branch = git(self.source, "symbolic-ref", "HEAD")

        record = self.provision()
        checkout = Path(record["checkoutPath"])

        self.assertEqual("ready", record["state"])
        self.assertEqual(self.revision, git(checkout, "rev-parse", "HEAD"))
        self.assertEqual("", git(checkout, "status", "--porcelain", "--ignored"))
        self.assertEqual("original source\n", (checkout / "source.txt").read_text())
        self.assertEqual(before, git(self.source, "status", "--porcelain"))
        self.assertEqual(branch, git(self.source, "symbolic-ref", "HEAD"))
        self.assertIn("detached", git(self.source, "worktree", "list", "--porcelain"))
        self.assertTrue(checkout.is_relative_to(self.managed))
        self.assertEqual("1", checkout.name)
        self.assertEqual(0o700, self.managed.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.state / "ledgers/investigation-worktrees.jsonl").stat().st_mode & 0o777)

    def test_repeated_provision_is_idempotent_and_request_is_frozen(self) -> None:
        first = self.provision()
        ledger = self.state / "ledgers/investigation-worktrees.jsonl"
        before = ledger.read_bytes()
        self.assertEqual(first, self.provision())
        self.assertEqual(before, ledger.read_bytes())
        changed = {**self.request, "question": "Different request"}
        with self.assertRaisesRegex(ValueError, "request"):
            self.provision(changed)
        self.assertEqual([first], worktrees.list_investigation_worktrees(self.state))

    def test_pin_may_precede_coordinator_head_but_must_be_exact_commit(self) -> None:
        (self.source / "source.txt").write_text("new committed source\n", encoding="utf-8")
        git(self.source, "commit", "-qam", "Second test source")
        first = self.provision()
        self.assertEqual(self.revision, git(Path(first["checkoutPath"]), "rev-parse", "HEAD"))
        for revision in ("HEAD", self.revision[:12], "f" * 40):
            request = copy.deepcopy(self.request)
            request["sourceRevision"] = revision
            request["investigationScope"]["sourceRevision"] = revision
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.provision(request)

    def test_repository_and_common_git_identity_cannot_change(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository"):
            self.provision({**self.request, "repository": "other/repo"})
        self.provision()
        other = self.make_repository("other")
        with self.assertRaisesRegex(ValueError, "Git|repository"):
            worktrees.provision_investigation_worktree(
                self.state, self.request, source_checkout=other,
                attempt=1, recorded_at=NOW, managed_root=self.managed,
            )

    def bind(self, record: dict, session: str = "worker-one", request: dict | None = None) -> dict:
        return worktrees.bind_investigation_worktree(
            self.state, request or self.request, checkout=Path(record["checkoutPath"]),
            session_id=session, recorded_at=NOW,
        )

    def finish(self, record: dict, **kwargs) -> dict:
        return worktrees.finish_investigation_worktree(
            self.state, self.request, checkout=Path(record["checkoutPath"]),
            session_id="worker-one", recorded_at=NOW, **kwargs,
        )

    def cleanup(self, record: dict, **kwargs) -> dict:
        return worktrees.cleanup_investigation_worktree(
            self.state, self.request, checkout=Path(record["checkoutPath"]),
            session_id="worker-one", recorded_at=NOW, **kwargs,
        )

    def test_concurrent_distinct_requests_receive_distinct_owned_worktrees(self) -> None:
        requests = [{**self.request, "investigationId": f"investigation:{index}"} for index in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(self.provision, requests))
        self.assertEqual(3, len({row["checkoutPath"] for row in records}))
        self.assertEqual(3, len({row["ownershipId"] for row in records}))
        for index, (record, request) in enumerate(zip(records, requests)):
            self.bind(record, f"worker-{index}", request)
        self.assertEqual(["bound"] * 3, [row["state"] for row in worktrees.list_investigation_worktrees(self.state)])

    def test_concurrent_duplicate_provision_creates_only_one_worktree(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda _: self.provision(), range(2)))
        self.assertEqual(records[0], records[1])
        self.assertEqual(1, len(worktrees.list_investigation_worktrees(self.state)))

    def test_binding_is_exclusive_for_request_checkout_and_session(self) -> None:
        record = self.bind(self.provision())
        self.assertEqual(record, self.bind(record))
        with self.assertRaisesRegex(ValueError, "session"):
            self.bind(record, "worker-two")
        other = {**self.request, "investigationId": "investigation:other"}
        with self.assertRaisesRegex(ValueError, "request"):
            self.bind(record, "worker-two", other)
        other_record = self.provision(other)
        with self.assertRaisesRegex(ValueError, "session"):
            self.bind(other_record, "worker-one", other)
        with self.assertRaisesRegex(ValueError, "owned|registry"):
            worktrees.validate_investigation_worktree(
                self.state, self.request, checkout=self.source, session_id="worker-one",
            )
        with self.assertRaisesRegex(ValueError, "session"):
            worktrees.validate_investigation_worktree(
                self.state, self.request, checkout=Path(record["checkoutPath"]), session_id="wrong",
            )

    def test_cleanup_requires_terminal_stopped_and_preserves_results_and_other_trees(self) -> None:
        record = self.bind(self.provision())
        other = self.provision({**self.request, "investigationId": "investigation:other"})
        result = self.state / "ledgers/investigation-results.jsonl"
        result.write_text('{"result":"retained"}\n')
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.cleanup(record, confirm_worker_stopped=True)
        terminal = self.finish(record, status="completed")
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.cleanup(terminal)
        removed = self.cleanup(terminal, confirm_worker_stopped=True)
        self.assertEqual("cleaned", removed["state"])
        self.assertEqual("completed", removed["terminalStatus"])
        self.assertTrue(removed["workerStopped"])
        self.assertFalse(Path(record["checkoutPath"]).exists())
        self.assertTrue(Path(other["checkoutPath"]).exists())
        self.assertEqual('{"result":"retained"}\n', result.read_text())
        before = (self.state / "ledgers/investigation-worktrees.jsonl").read_bytes()
        self.assertEqual(removed, self.cleanup(removed, confirm_worker_stopped=True))
        self.assertEqual(before, (self.state / "ledgers/investigation-worktrees.jsonl").read_bytes())
        self.assertEqual(2, len(worktrees.list_investigation_worktrees(self.state)))

    def test_replacement_needs_terminal_stopped_and_never_reuses_old_path(self) -> None:
        record = self.bind(self.provision())
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.provision(attempt=2)
        self.finish(record, status="failed")
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.provision(attempt=2)
        self.finish(record, status="failed", confirm_worker_stopped=True)
        replacement = self.provision(attempt=2)
        self.assertNotEqual(record["checkoutPath"], replacement["checkoutPath"])
        self.assertTrue(Path(record["checkoutPath"]).exists())

    def test_tracked_untracked_and_ignored_files_block_validation_and_cleanup(self) -> None:
        for name in ("source.txt", "untracked.txt", "ignored-output/artifact"):
            with self.subTest(name=name):
                request = {**self.request, "investigationId": f"investigation:{name}"}
                record = self.bind(self.provision(request), name, request)
                checkout = Path(record["checkoutPath"])
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("retain this content\n")
                with self.assertRaisesRegex(ValueError, "clean"):
                    worktrees.validate_investigation_worktree(
                        self.state, request, checkout=checkout, session_id=name,
                    )
                worktrees.finish_investigation_worktree(
                    self.state, request, checkout=checkout, session_id=name,
                    status="failed", recorded_at=NOW, confirm_worker_stopped=True,
                )
                with self.assertRaisesRegex(ValueError, "clean"):
                    worktrees.cleanup_investigation_worktree(
                        self.state, request, checkout=checkout, session_id=name,
                        recorded_at=NOW, confirm_worker_stopped=True,
                    )
                self.assertEqual("retain this content\n", target.read_text())
                self.assertIn(str(checkout), git(self.source, "worktree", "list", "--porcelain"))

    def test_head_lock_and_registration_changes_are_never_removed(self) -> None:
        record = self.bind(self.provision())
        checkout = Path(record["checkoutPath"])
        self.finish(record, status="failed", confirm_worker_stopped=True)
        git(checkout, "commit", "--allow-empty", "-qm", "Changed worker HEAD")
        with self.assertRaisesRegex(ValueError, "HEAD|revision"):
            self.cleanup(record, confirm_worker_stopped=True)
        git(checkout, "reset", "--hard", self.revision)
        git(self.source, "worktree", "unlock", str(checkout))
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.cleanup(record, confirm_worker_stopped=True)
        self.assertTrue(checkout.exists())

    def test_default_root_is_fixed_not_derived_from_cycle_or_checkout(self) -> None:
        with patch.object(Path, "home", return_value=self.root / "home"):
            self.assertEqual(
                self.root / "home/.copilot/ci-shepherd/worktrees",
                worktrees.default_worktree_root(),
            )

    def reconcile(self, record: dict) -> dict:
        return worktrees.reconcile_investigation_worktree(
            self.state, self.request, checkout=Path(record["checkoutPath"]), recorded_at=NOW,
        )

    def test_durable_intent_precedes_git_mutation_and_restart_uses_git_ownership_token(self) -> None:
        append = worktrees._append

        def crash_before_ready(path, record, **updates):
            if updates.get("state") == "ready":
                raise KeyboardInterrupt("simulated termination after Git add")
            return append(path, record, **updates)

        with patch.object(worktrees, "_append", side_effect=crash_before_ready):
            with self.assertRaises(KeyboardInterrupt):
                self.provision()
        intent = worktrees.list_investigation_worktrees(self.state)[0]
        self.assertEqual("provisioning", intent["state"])
        self.assertIn(
            f"locked ci-shepherd:{intent['ownershipId']}",
            git(self.source, "worktree", "list", "--porcelain"),
        )
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.provision()
        recovered = self.reconcile(intent)
        self.assertEqual("ready", recovered["state"])
        self.assertEqual(intent["ownershipId"], recovered["ownershipId"])
        self.assertEqual(recovered, self.provision())

    def test_restart_never_adopts_directory_name_or_unowned_git_registration(self) -> None:
        invoke = worktrees._git

        def crash_before_add(directory, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "add"):
                rows = worktrees.list_investigation_worktrees(self.state)
                self.assertEqual("provisioning", rows[0]["state"])
                raise KeyboardInterrupt("simulated termination before Git add")
            return invoke(directory, *arguments, **kwargs)

        with patch.object(worktrees, "_git", side_effect=crash_before_add):
            with self.assertRaises(KeyboardInterrupt):
                self.provision()
        intent = worktrees.list_investigation_worktrees(self.state)[0]
        blocked = self.reconcile(intent)
        self.assertEqual("blocked", blocked["state"])
        self.assertTrue(blocked["error"])
        checkout = Path(intent["checkoutPath"])
        git(self.source, "worktree", "add", "--detach", str(checkout), self.revision)
        self.assertEqual("blocked", self.reconcile(blocked)["state"])
        with self.assertRaisesRegex(ValueError, "terminal"):
            worktrees.cleanup_investigation_worktree(
                self.state, self.request, checkout=checkout, recorded_at=NOW,
                confirm_worker_stopped=True,
            )
        self.assertTrue(checkout.exists())

    def test_partial_git_failure_is_visible_and_never_silently_retried(self) -> None:
        invoke = worktrees._git

        def fail_add(directory, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "add"):
                raise ValueError("Git add simulated failure")
            return invoke(directory, *arguments, **kwargs)

        with patch.object(worktrees, "_git", side_effect=fail_add):
            with self.assertRaisesRegex(ValueError, "simulated"):
                self.provision()
        record = worktrees.list_investigation_worktrees(self.state)[0]
        self.assertEqual("provisioning-failed", record["state"])
        self.assertIn("simulated", record["error"])
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.provision()

    def test_cleanup_crashes_before_and_after_remove_are_reconcilable(self) -> None:
        record = self.bind(self.provision())
        self.finish(record, status="completed", confirm_worker_stopped=True)
        invoke = worktrees._git

        def crash_after_unlock(directory, *arguments, **kwargs):
            output = invoke(directory, *arguments, **kwargs)
            if arguments[:2] == ("worktree", "unlock"):
                raise KeyboardInterrupt("simulated termination after unlock")
            return output

        with patch.object(worktrees, "_git", side_effect=crash_after_unlock):
            with self.assertRaises(KeyboardInterrupt):
                self.cleanup(record, confirm_worker_stopped=True)
        pending = worktrees.list_investigation_worktrees(self.state)[0]
        self.assertEqual("cleanup-pending", pending["state"])
        self.assertEqual("cleanup-pending", self.reconcile(pending)["state"])
        append = worktrees._append

        def crash_before_cleaned(path, row, **updates):
            if updates.get("state") == "cleaned":
                raise KeyboardInterrupt("simulated termination after removal")
            return append(path, row, **updates)

        with patch.object(worktrees, "_append", side_effect=crash_before_cleaned):
            with self.assertRaises(KeyboardInterrupt):
                self.cleanup(record, confirm_worker_stopped=True)
        self.assertFalse(Path(record["checkoutPath"]).exists())
        cleaned = self.reconcile(pending)
        self.assertEqual("cleaned", cleaned["state"])
        self.assertEqual("completed", cleaned["terminalStatus"])

    def test_symlink_root_ancestor_checkout_and_registry_fail_closed(self) -> None:
        external = self.root / "external"
        external.mkdir()
        (external / "keep.txt").write_text("preserve")
        self.managed.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.provision()
        self.managed.unlink()
        record = self.bind(self.provision())
        self.finish(record, status="failed", confirm_worker_stopped=True)
        checkout = Path(record["checkoutPath"])
        renamed = checkout.with_name("retained-checkout")
        checkout.rename(renamed)
        checkout.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.cleanup(record, confirm_worker_stopped=True)
        checkout.unlink()
        renamed.rename(checkout)
        parent = checkout.parent
        moved = parent.with_name("retained-parent")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.cleanup(record, confirm_worker_stopped=True)
        parent.unlink()
        moved.rename(parent)
        ledger = self.state / "ledgers/investigation-worktrees.jsonl"
        saved = ledger.with_suffix(".saved")
        ledger.rename(saved)
        ledger.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.cleanup(record, confirm_worker_stopped=True)
        self.assertEqual("preserve", (external / "keep.txt").read_text())

    def test_unsafe_root_registry_overlap_and_preexisting_paths_are_rejected(self) -> None:
        for root in (self.source / "workers", self.state / "workers", self.root / "../escape"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                worktrees.provision_investigation_worktree(
                    self.state, self.request, source_checkout=self.source, attempt=1,
                    recorded_at=NOW, managed_root=root,
                )
        record = self.provision()
        with self.assertRaisesRegex(ValueError, "unowned|occupies"):
            worktrees.provision_investigation_worktree(
                self.root / "other-state", self.request, source_checkout=self.source, attempt=1,
                recorded_at=NOW, managed_root=self.managed,
            )
        self.assertTrue(Path(record["checkoutPath"]).exists())

    def test_forged_registry_path_and_torn_rows_block_cleanup(self) -> None:
        record = self.bind(self.provision())
        self.finish(record, status="completed", confirm_worker_stopped=True)
        ledger = self.state / "ledgers/investigation-worktrees.jsonl"
        original = ledger.read_bytes()
        rows = [json.loads(line) for line in original.splitlines()]
        for row in rows:
            row["checkoutPath"] = str(self.source)
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.cleanup(record, confirm_worker_stopped=True)
        ledger.write_bytes(original + b'{"incomplete"')
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.cleanup(record, confirm_worker_stopped=True)
        self.assertTrue(Path(record["checkoutPath"]).exists())

    def test_copied_registry_cannot_claim_the_original_state_ownership(self) -> None:
        record = self.bind(self.provision())
        copied = self.root / "copied-state"
        (copied / "ledgers").mkdir(parents=True)
        (copied / "ledgers/investigation-worktrees.jsonl").write_bytes(
            (self.state / "ledgers/investigation-worktrees.jsonl").read_bytes(),
        )
        with self.assertRaisesRegex(ValueError, "state"):
            worktrees.validate_investigation_worktree(
                copied, self.request, checkout=Path(record["checkoutPath"]), session_id="worker-one",
            )

    def test_hidden_tracked_changes_and_ignored_submodule_config_are_not_clean(self) -> None:
        record = self.bind(self.provision())
        checkout = Path(record["checkoutPath"])
        git(checkout, "update-index", "--assume-unchanged", "--", "source.txt")
        (checkout / "source.txt").write_text("hidden edit\n")
        self.assertEqual("", git(checkout, "status", "--porcelain"))
        with self.assertRaisesRegex(ValueError, "clean|index"):
            worktrees.validate_investigation_worktree(
                self.state, self.request, checkout=checkout, session_id="worker-one",
            )

    def test_malformed_lifecycle_metadata_cannot_authorize_cleanup(self) -> None:
        record = self.bind(self.provision())
        ledger = self.state / "ledgers/investigation-worktrees.jsonl"
        original = ledger.read_bytes()
        for field, value in (
            ("workerStopped", "true"), ("terminalStatus", "invented"),
            ("sessionId", []), ("state", "terminal"), ("gitDirectoryIdentity", {}),
        ):
            latest = {**record, field: value}
            ledger.write_bytes(original + (json.dumps(latest) + "\n").encode())
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "registry"):
                worktrees.list_investigation_worktrees(self.state)
        ledger.write_bytes(original)

    def test_repository_checkout_hooks_do_not_execute_during_provision(self) -> None:
        hook = self.source / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 79\n")
        hook.chmod(0o700)
        record = self.provision()
        self.assertEqual("ready", record["state"])

    def test_empty_ignored_directory_is_not_silently_removed(self) -> None:
        record = self.bind(self.provision())
        checkout = Path(record["checkoutPath"])
        ignored = checkout / "ignored-output"
        ignored.mkdir()
        self.finish(record, status="failed", confirm_worker_stopped=True)
        with self.assertRaisesRegex(ValueError, "clean"):
            self.cleanup(record, confirm_worker_stopped=True)
        self.assertTrue(ignored.exists())

    def test_cli_provisions_and_manages_inventory_without_original_plan_at_cleanup(self) -> None:
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({
            "schemaVersion": 1, "repository": "owner/repo",
            "requests": [self.request], "activeInvestigationIds": [],
        }))
        script = Path("scripts/investigation_worktree.py").resolve()

        def cli(operation: str, *args: str, success: bool = True):
            result = subprocess.run(
                [sys.executable, str(script), operation, "--state-dir", str(self.state), *args],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0 if success else 2, result.returncode, result.stderr)
            return json.loads(result.stdout) if success else result

        record = cli(
            "provision", "--plan", str(plan), "--investigation-id", self.request["investigationId"],
            "--source-checkout", str(self.source), "--attempt", "1",
            "--managed-root", str(self.managed), "--recorded-at", NOW,
        )
        self.assertEqual([record], cli("list")["worktrees"])
        identity = ["--ownership-id", record["ownershipId"], "--recorded-at", NOW]
        cli("bind", *identity, "--session-id", "worker-one")
        plan.unlink()
        cli("finish", *identity, "--session-id", "worker-one", "--status", "completed")
        denied = cli("cleanup", *identity, "--session-id", "worker-one", success=False)
        self.assertIn("stopped", denied.stderr)
        removed = cli("cleanup", *identity, "--session-id", "worker-one", "--confirm-worker-stopped")
        self.assertEqual("cleaned", removed["state"])
        self.assertFalse(Path(record["checkoutPath"]).exists())
        self.assertEqual("cleaned", cli("list")["worktrees"][0]["state"])


if __name__ == "__main__":
    unittest.main()
