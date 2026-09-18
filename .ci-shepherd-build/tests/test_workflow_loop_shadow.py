from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from ci_shepherd.workflow_loop.models import TaskState, WorkflowKey
from ci_shepherd.workflow_loop.shadow import prepare_shadow, read_shadow_metadata
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from test_workflow_loop_state import NOW, _intent, _reservation, _run


class WorkflowLoopShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / f".shadow-test-{uuid.uuid4().hex}"
        self.root.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.canonical = self.root / "canonical"
        self.shadow = self.root / "shadow"

    def store(self, path: Path) -> WorkflowLoopStore:
        return WorkflowLoopStore(path, repository="owner/repo", branch="main")

    def prepare(self, **kwargs: object) -> Path:
        arguments = {
            "repository": "owner/repo",
            "branch": "main",
            "workflow_ids": None,
        }
        arguments.update(kwargs)
        return prepare_shadow(self.canonical, self.shadow, **arguments)

    def test_missing_canonical_stays_missing_and_empty_canonical_stays_empty(self) -> None:
        self.assertEqual(self.shadow, self.prepare())
        self.assertFalse(self.canonical.exists())
        self.assertEqual((), self.store(self.shadow).list_items())
        self.assertEqual([], read_shadow_metadata(self.shadow)["frozen_item_ids"])
        shutil.rmtree(self.shadow)
        self.canonical.mkdir(mode=0o700)
        self.prepare()
        self.assertEqual([], list(self.canonical.iterdir()))

    def test_backup_includes_wal_and_preserves_rows_links_and_history(self) -> None:
        canonical = self.store(self.canonical)
        canonical.initialize()
        with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as held:
            held.execute("PRAGMA wal_autocheckpoint = 0")
            item = canonical.upsert_failure(_run(), NOW)
            canonical.update_item(
                replace(
                    item, issue_number=71, task_id="task-1",
                    task_state=TaskState.IN_PROGRESS, pull_request_number=99,
                ),
                history_event="linked",
                summary="Owned work linked.",
                detail={"retained": True},
            )
            held.execute("INSERT INTO passes(pass_id, started_at) VALUES ('wal-only', ?)", (NOW,))
            held.commit()
            self.assertGreater((self.canonical / "workflow-loop.sqlite3-wal").stat().st_size, 0)
            before = tuple(held.iterdump())
            self.prepare()
            self.assertEqual(before, tuple(held.iterdump()))
        self.assertEqual(canonical.list_items(), self.store(self.shadow).list_items())
        with closing(sqlite3.connect(self.shadow / "workflow-loop.sqlite3")) as snapshot:
            snapshot.execute("DELETE FROM meta WHERE key = 'shadow_source'")
            self.assertEqual(before, tuple(snapshot.iterdump()))
            snapshot.rollback()

    def test_resume_retains_shadow_progress_and_validates_binding(self) -> None:
        self.prepare(workflow_ids=(42, 17))
        shadow = self.store(self.shadow)
        shadow.upsert_failure(_run(), NOW)
        marker = (self.shadow / "shadow.json").read_bytes()
        self.assertEqual(self.shadow, self.prepare(workflow_ids=(17, 42)))
        self.assertEqual(1, len(shadow.list_items()))
        self.assertEqual(marker, (self.shadow / "shadow.json").read_bytes())
        for mismatch in (
            {"repository": "different/repo", "workflow_ids": (17, 42)},
            {"branch": "release", "workflow_ids": (17, 42)},
            {"workflow_ids": (17,)},
            {"workflow_ids": None},
        ):
            with self.subTest(mismatch=mismatch), self.assertRaises(ValueError):
                self.prepare(**mismatch)
        with self.assertRaises(ValueError):
            prepare_shadow(
                self.root / "different-source", self.shadow,
                repository="owner/repo", branch="main", workflow_ids=(17, 42),
            )

    def test_resume_accepts_provider_permissions_beneath_private_shadow(self) -> None:
        self.prepare()
        home = self.shadow / "workers" / "worker-1" / "copilot-home"
        for directory in (home.parent.parent, home.parent, home):
            directory.mkdir(mode=0o700)
        sessions = home / "session-state"
        sessions.mkdir(mode=0o755)
        sessions.chmod(0o755)
        provider_file = sessions / "session.log"
        provider_file.write_text("provider output", encoding="utf-8")
        provider_file.chmod(0o644)

        self.assertEqual(self.shadow, self.prepare())
        self.assertEqual(0o755, sessions.stat().st_mode & 0o777)
        self.assertEqual(0o644, provider_file.stat().st_mode & 0o777)
        self.assertEqual("provider output", provider_file.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "binding"):
            self.prepare(branch="different")

    def test_resume_rejects_unsafe_provider_descendants(self) -> None:
        self.prepare()
        sessions = self.shadow / "workers" / "worker-1" / "copilot-home" / "session-state"
        sessions.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("untouched", encoding="utf-8")
        provider_file = sessions / "session.log"
        provider_file.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.prepare()
        provider_file.unlink()
        os.link(outside, provider_file)
        with self.assertRaisesRegex(ValueError, "safe regular file"):
            self.prepare()
        provider_file.unlink()
        os.mkfifo(provider_file)
        with self.assertRaisesRegex(ValueError, "safe regular file"):
            self.prepare()
        self.assertEqual("untouched", outside.read_text(encoding="utf-8"))

    def test_inherited_workers_and_actions_are_frozen_and_rehomed_without_ownership(self) -> None:
        store = self.store(self.canonical)
        store.initialize()
        frozen = []
        workers = []
        artifacts = {}
        for index, (state, consumed) in enumerate(
            (("queued", False), ("running", False), ("succeeded", False),
             ("failed", False), ("succeeded", True)),
            start=1,
        ):
            item = store.upsert_failure(
                replace(_run(), key=WorkflowKey("owner/repo", index, "main")), NOW,
            )
            reservation = _reservation(
                self.canonical, item.id, item.episode, item.evidence_fingerprint,
            )
            store.reserve_worker(reservation, capacity_limit=10)
            workers.append(reservation.worker_id)
            if not consumed:
                frozen.append(item.id)
            for name in ("request_path", "result_path", "detail_path", "lifetime_lock_path"):
                path = Path(getattr(reservation, name))
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                content = json.dumps({
                    "requestPath": reservation.request_path,
                    "resultPath": reservation.result_path,
                    "detailPath": reservation.detail_path,
                }).encode()
                path.write_bytes(content)
                artifacts[path] = content
            with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as connection:
                connection.execute(
                    "UPDATE workers SET state = ?, pid = 12345, consumed_at = ? "
                    "WHERE worker_id = ?",
                    (state, NOW if consumed else None, reservation.worker_id),
                )
                connection.commit()
        for index, state in enumerate(("prepared", "invoking", "uncertain", "confirmed"), start=20):
            item = store.upsert_failure(
                replace(_run(), key=WorkflowKey("owner/repo", index, "main")), NOW,
            )
            intent = _intent(item.id, item.episode)
            store.prepare_action(intent, capacity_limit=30)
            if state != "confirmed":
                frozen.append(item.id)
            if state != "prepared":
                with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as connection:
                    connection.execute(
                        "UPDATE action_attempts SET state = ?, invoked_at = ?, "
                        "invocation_pass_id = 'canonical-pass', invocation_owner_id = 'canonical-owner' "
                        "WHERE action_id = ?",
                        (state, NOW, intent.action_id),
                    )
                    connection.commit()
        with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as connection:
            original = tuple(connection.iterdump())
        self.prepare()
        metadata = read_shadow_metadata(self.shadow)
        self.assertEqual(sorted(frozen), metadata["frozen_item_ids"])
        self.assertEqual(sorted(workers), metadata["inherited_worker_ids"])
        self.assertEqual({str(item) for item in frozen}, set(metadata["frozen_reasons"]))
        for reasons in metadata["frozen_reasons"].values():
            self.assertTrue(reasons)
            self.assertTrue(all("inherited" in reason.lower() for reason in reasons))
        for worker in self.store(self.shadow).list_workers():
            self.assertIsNone(worker.pid)
            self.assertEqual(f"shadow-inherited:{worker.worker_id}", worker.session_id)
            for name in ("request_path", "result_path", "detail_path", "lifetime_lock_path"):
                path = Path(getattr(worker, name))
                self.assertTrue(path.is_relative_to(self.shadow))
                self.assertFalse(path.exists(), "Inherited artifacts must not be executable.")
        for worker in metadata["inherited_workers"]:
            self.assertEqual("unavailable: inherited artifacts are not copied", worker["artifacts_status"])
            self.assertEqual(12345, worker["original_pid"])
        self.assertEqual(store.list_items(), self.store(self.shadow).list_items())
        self.assertEqual(store.list_actions(), self.store(self.shadow).list_actions())
        with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as connection:
            self.assertEqual(original, tuple(connection.iterdump()))
        for path, content in artifacts.items():
            self.assertEqual(content, path.read_bytes())

    def test_worker_path_outside_source_is_rejected_without_completed_marker(self) -> None:
        store = self.store(self.canonical)
        store.initialize()
        item = store.upsert_failure(_run(), NOW)
        reservation = _reservation(self.canonical, item.id, item.episode, item.evidence_fingerprint)
        store.reserve_worker(reservation, capacity_limit=1)
        outside = self.root / "outside.json"
        outside.write_text("untouched", encoding="utf-8")
        with closing(sqlite3.connect(self.canonical / "workflow-loop.sqlite3")) as connection:
            connection.execute("UPDATE workers SET request_path = ?", (str(outside),))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "worker.*path|Worker.*path"):
            self.prepare()
        self.assertFalse((self.shadow / "shadow.json").exists())
        self.assertEqual("untouched", outside.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_owner_only_shadow_and_source_permissions_are_unchanged(self) -> None:
        store = self.store(self.canonical)
        store.initialize()
        os.chmod(self.canonical, 0o755)
        os.chmod(self.canonical / "workflow-loop.sqlite3", 0o644)
        self.prepare()
        self.assertEqual(0o755, self.canonical.stat().st_mode & 0o777)
        self.assertEqual(0o644, (self.canonical / "workflow-loop.sqlite3").stat().st_mode & 0o777)
        for path in (self.shadow, *self.shadow.rglob("*")):
            with self.subTest(path=path):
                self.assertEqual(0o700 if path.is_dir() else 0o600, path.stat().st_mode & 0o777)
        for path in (
            self.shadow, self.shadow / "shadow.json",
            self.shadow / "workflow-loop.sqlite3",
        ):
            with self.subTest(path=path):
                private_mode = 0o700 if path.is_dir() else 0o600
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
                try:
                    with self.assertRaisesRegex(ValueError, "owner-only"):
                        self.prepare()
                finally:
                    os.chmod(path, private_mode)

    def test_overlapping_relative_or_nonempty_shadow_is_rejected(self) -> None:
        for canonical, shadow in (
            (self.canonical, self.canonical),
            (self.canonical, self.canonical / "nested"),
            (self.shadow / "nested", self.shadow),
            (self.canonical, Path("relative-shadow")),
            (self.canonical, self.root / "segment" / ".." / "shadow"),
        ):
            with self.subTest(canonical=canonical, shadow=shadow), self.assertRaises(ValueError):
                prepare_shadow(canonical, shadow, repository="owner/repo", branch="main", workflow_ids=None)
        self.shadow.mkdir(mode=0o700)
        (self.shadow / "unrelated").write_text("retained", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual("retained", (self.shadow / "unrelated").read_text(encoding="utf-8"))

    def test_missing_database_with_unexpected_canonical_artifacts_fails_closed(self) -> None:
        self.canonical.mkdir()
        (self.canonical / "workers").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected artifacts"):
            self.prepare()
        self.assertFalse(self.shadow.exists())
        self.assertEqual(["workers"], [path.name for path in self.canonical.iterdir()])

    def test_symlink_source_shadow_ancestor_and_database_are_rejected(self) -> None:
        self.canonical.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.prepare()
        self.canonical.unlink()
        self.shadow.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.prepare()
        self.shadow.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            prepare_shadow(
                self.canonical, alias / "shadow", repository="owner/repo",
                branch="main", workflow_ids=None,
            )
        alias.unlink()
        self.canonical.mkdir()
        (self.canonical / "workflow-loop.sqlite3").symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.prepare()

    def test_worker_symlinks_and_hardlinks_fail_closed(self) -> None:
        store = self.store(self.canonical)
        store.initialize()
        item = store.upsert_failure(_run(), NOW)
        reservation = _reservation(self.canonical, item.id, item.episode, item.evidence_fingerprint)
        store.reserve_worker(reservation, capacity_limit=1)
        request = Path(reservation.request_path)
        request.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("retained", encoding="utf-8")
        request.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.prepare()
        request.unlink()
        os.link(outside, request)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.prepare()
        self.assertEqual("retained", outside.read_text(encoding="utf-8"))

    def test_database_scope_mismatch_is_rejected_before_creating_shadow(self) -> None:
        store = self.store(self.canonical)
        store.initialize(workflow_ids=(42,))
        with self.assertRaisesRegex(ValueError, "scope"):
            self.prepare()
        self.assertFalse(self.shadow.exists())
        self.prepare(workflow_ids=(42,))

    def test_interrupted_creation_cannot_be_resumed(self) -> None:
        with patch(
            "ci_shepherd.workflow_loop.shadow.WorkflowLoopStore.initialize",
            side_effect=RuntimeError("interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.prepare()
        self.assertFalse((self.shadow / "shadow.json").exists())
        self.assertIsNone(read_shadow_metadata(self.shadow))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.prepare()
        self.assertFalse(self.canonical.exists())

    def test_malformed_marker_cannot_disable_inherited_worker_guards(self) -> None:
        self.prepare()
        marker = self.shadow / "shadow.json"
        original = json.loads(marker.read_text(encoding="utf-8"))
        for changes in (
            {"frozen_item_ids": "none"},
            {"frozen_item_ids": [True]},
            {"inherited_worker_ids": None},
            {"frozen_reasons": []},
            {"inherited_workers": "none"},
        ):
            marker.write_text(json.dumps({**original, **changes}), encoding="utf-8")
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare()
        marker.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_shadow_metadata(self.shadow)

    def test_resumed_worker_paths_cannot_escape_shadow(self) -> None:
        self.prepare()
        store = self.store(self.shadow)
        item = store.upsert_failure(_run(), NOW)
        reservation = _reservation(self.shadow, item.id, item.episode, item.evidence_fingerprint)
        store.reserve_worker(reservation, capacity_limit=1)
        with closing(sqlite3.connect(self.shadow / "workflow-loop.sqlite3")) as connection:
            connection.execute(
                "UPDATE workers SET lifetime_lock_path = ?",
                (str(self.canonical / "workers" / "lifetime.lock"),),
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "worker.*path|Worker.*path"):
            self.prepare()

    def test_database_retains_shadow_provenance_when_marker_is_deleted(self) -> None:
        canonical = self.store(self.canonical)
        canonical.initialize()
        self.prepare()
        with closing(sqlite3.connect(self.shadow / "workflow-loop.sqlite3")) as connection:
            self.assertEqual(
                (str(self.canonical),),
                connection.execute("SELECT value FROM meta WHERE key = 'shadow_source'").fetchone(),
            )
        self.assertIsNone(read_shadow_metadata(self.canonical))
        (self.shadow / "shadow.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing.*marker|marker.*missing"):
            read_shadow_metadata(self.shadow)
        with self.assertRaises(ValueError):
            self.prepare()
        copied = self.root / "copied"
        copied.mkdir(mode=0o700)
        shutil.copyfile(self.shadow / "workflow-loop.sqlite3", copied / "workflow-loop.sqlite3")
        with self.assertRaises(ValueError):
            read_shadow_metadata(copied)
        with self.assertRaises(ValueError):
            prepare_shadow(
                copied, self.root / "second-shadow", repository="owner/repo",
                branch="main", workflow_ids=None,
            )

    def test_empty_shadow_database_is_bound_and_marker_cannot_disagree(self) -> None:
        self.prepare()
        database = self.shadow / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                (str(self.canonical),),
                connection.execute("SELECT value FROM meta WHERE key = 'shadow_source'").fetchone(),
            )
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'shadow_source'",
                (str(self.root / "different"),),
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "binding|source"):
            read_shadow_metadata(self.shadow)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("DELETE FROM meta WHERE key = 'shadow_source'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "binding|source"):
            read_shadow_metadata(self.shadow)


if __name__ == "__main__":
    unittest.main()
