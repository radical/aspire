from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import record_poc
from ci_shepherd.ci_failure_triage import (
    _fingerprint,
    attach_ci_failure_triage,
    build_ci_failure_triage,
)

from ci_shepherd.history import (
    FRESHNESS_CLASSES,
    HistoryError,
    load_current,
    load_recorded_run,
    record_history,
    record_poc_history,
)
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input
from test_workflow_health import workflow_snapshot


TEST_TEMP_ROOT = Path(__file__).parent / ".tmp"
OBSERVED_AT = "2026-08-19T05:55:14Z"


def evidence_record(
    kind: str = "issue-event",
    *,
    availability: str = "available",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "url": "https://example.invalid/evidence",
        "collectedAt": OBSERVED_AT,
        "availability": availability,
        "payload": payload or {},
    }


def snapshot(
    *,
    repository: str = "owner/repo",
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
        "collectedAt": OBSERVED_AT,
        "openIssues": [1],
        "evidence": evidence or {"issue:1": evidence_record()},
        "collectionErrors": [],
    }


def report(*, repository: str = "owner/repo") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
        "decisions": [
            {
                "issueNumber": 1,
                "issueUrl": f"https://github.com/{repository}/issues/1",
                "issueKind": "incident",
                "state": "observing",
                "proposedAction": "wait",
                "confidence": "high",
                "summary": "Keep observing.",
                "reasoning": "The current evidence does not justify action.",
                "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {
                    "type": "monitor",
                    "description": "Wait for another workflow run.",
                },
                "suggestedOwners": [
                    {
                        "name": "team-a",
                        "reason": "Owns the affected workflow.",
                    }
                ],
                "relatedIssues": [],
                "changedSincePreviousRun": False,
            }
        ],
    }


def prepared_assessment() -> dict[str, object]:
    snapshot_id = f"snapshot:owner/repo:{OBSERVED_AT}"
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": OBSERVED_AT,
        "snapshotId": snapshot_id,
        "issues": [
            {
                "issueNumber": 1,
                "evidenceBundle": [
                    {
                        "id": "issue:1",
                        "kind": "issue-event",
                        "availability": "available",
                        "payload": {"number": 1},
                    }
                ],
            }
        ],
    }


def poc_judgments() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": f"snapshot:owner/repo:{OBSERVED_AT}",
        "issues": [
            {
                "issueNumber": 1,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": "watch",
                        "target": {
                            "kind": "test",
                            "value": "Namespace.Type.Test",
                        },
                        "confidence": "low",
                        "summary": "One independent occurrence is not enough to quarantine.",
                        "evidenceIds": ["issue:1"],
                        "missingEvidence": [],
                        "reassessWhen": "The test fails in another independent run.",
                    }
                ],
            }
        ],
    }


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temporary_directory.name) / "state"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_record_creates_immutable_run_and_current_view(self) -> None:
        current = record_history(
            self.root,
            "owner/repo",
            "run-001",
            snapshot(),
            report(),
            {"report.md": b"# Report\n", "logs/job.txt": b"failure\n"},
        )

        run = self.root / "runs" / "run-001"
        self.assertEqual(current.run_id, "run-001")
        self.assertEqual(current.run_directory, run)
        self.assertEqual(json.loads((run / "snapshot.json").read_text()), snapshot())
        self.assertEqual(json.loads((run / "report.json").read_text()), report())
        self.assertEqual((run / "report.md").read_bytes(), b"# Report\n")
        self.assertEqual((run / "logs" / "job.txt").read_bytes(), b"failure\n")
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["runId"], "run-001")
        self.assertTrue(manifest["complete"])
        self.assertEqual(load_current(self.root, "owner/repo"), current)

    def test_duplicate_run_id_is_rejected_without_changing_current(self) -> None:
        first = record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        with self.assertRaisesRegex(HistoryError, "already exists"):
            record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertEqual(load_current(self.root, "owner/repo"), first)

    def test_record_poc_creates_immutable_run_and_rejects_duplicate_cycle(self) -> None:
        current = record_poc_history(
            self.root,
            "owner/repo",
            "cycle-001",
            snapshot(),
            prepared_assessment(),
            poc_judgments(),
            "# CI Shepherd POC Assessment\n",
        )

        run = self.root / "runs" / "cycle-001"
        self.assertEqual("cycle-001", current.run_id)
        self.assertEqual(snapshot(), json.loads((run / "snapshot.json").read_text()))
        self.assertEqual(
            prepared_assessment(),
            json.loads((run / "assessment-input.json").read_text()),
        )
        self.assertEqual(
            poc_judgments(),
            json.loads((run / "judgments.json").read_text()),
        )
        self.assertEqual(
            "# CI Shepherd POC Assessment\n",
            (run / "report.md").read_text(),
        )

        with self.assertRaisesRegex(HistoryError, "already exists"):
            record_poc_history(
                self.root,
                "owner/repo",
                "cycle-001",
                snapshot(),
                prepared_assessment(),
                poc_judgments(),
                "# CI Shepherd POC Assessment\n",
            )

        self.assertEqual(current, load_current(self.root, "owner/repo"))

    def test_record_poc_rejects_prepared_identity_from_another_evidence_round(self) -> None:
        expanded_snapshot = snapshot()
        expanded_snapshot["expansions"] = [
            {
                "round": 1,
                "requests": [],
                "status": "complete",
                "errors": [],
            }
        ]

        with self.assertRaisesRegex(HistoryError, "snapshot identity"):
            record_poc_history(
                self.root,
                "owner/repo",
                "cycle-001",
                expanded_snapshot,
                prepared_assessment(),
                poc_judgments(),
                "# CI Shepherd POC Assessment\n",
            )

        self.assertFalse(self.root.exists())

    def test_record_poc_rejects_malformed_triage_before_persisting_state(self) -> None:
        prepared = prepared_assessment()
        prepared["triageRuleVersion"] = "aspire-ci-triage-v1"
        prepared["issues"][0]["ciFailureTriage"] = {
            "ruleVersion": "aspire-ci-triage-v2",
            "cases": [],
        }

        with self.assertRaisesRegex(HistoryError, "ruleVersion"):
            record_poc_history(
                self.root,
                "owner/repo",
                "cycle-001",
                snapshot(),
                prepared,
                poc_judgments(),
                "# CI Shepherd POC Assessment\n",
            )

        self.assertFalse(self.root.exists())

    def test_standalone_triage_is_validated_before_recording_history(self) -> None:
        prepared = prepared_assessment()
        triage = build_ci_failure_triage(prepared)
        prepared = attach_ci_failure_triage(prepared, triage)
        for name, content in (
            ("malformed", b"{broken"),
            ("wrong-snapshot", json.dumps({**triage, "snapshotId": "wrong"}).encode()),
            ("different-rules", json.dumps({**triage, "ruleVersion": "different"}).encode()),
        ):
            with self.subTest(name=name):
                state = self.root / name
                with self.assertRaisesRegex(HistoryError, "triage"):
                    record_poc_history(
                        state, "owner/repo", "cycle-001", snapshot(), prepared,
                        poc_judgments(), "# report\n", {"ci-failure-triage.json": content},
                    )
                self.assertFalse(state.exists())

    def test_standalone_triage_is_validated_on_persisted_reads(self) -> None:
        prepared = prepared_assessment()
        triage = build_ci_failure_triage(prepared)
        prepared = attach_ci_failure_triage(prepared, triage)
        record_poc_history(
            self.root, "owner/repo", "cycle-001", snapshot(), prepared,
            poc_judgments(), "# report\n",
            {"ci-failure-triage.json": json.dumps(triage).encode()},
        )
        run = self.root / "runs" / "cycle-001"
        content = json.dumps({**triage, "ruleVersion": "different"}).encode()
        (run / "ci-failure-triage.json").write_bytes(content)
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        entry = next(item for item in manifest["files"] if item["path"] == "ci-failure-triage.json")
        entry.update(size=len(content), crc32=f"{zlib.crc32(content):08x}")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(HistoryError, "triage"):
            load_recorded_run(self.root, "owner/repo", "cycle-001")

    def test_recorded_cycle_replay_cannot_hide_invalid_standalone_triage(self) -> None:
        inputs = self.root.parent / "triage-replay-inputs"
        inputs.mkdir()
        prepared = prepared_assessment()
        triage = build_ci_failure_triage(prepared)
        prepared = attach_ci_failure_triage(prepared, triage)
        for name, value in (
            ("input.json", snapshot()), ("prepared.json", prepared),
            ("judgments.json", poc_judgments()), ("ci-failure-triage.json", triage),
        ):
            (inputs / name).write_text(json.dumps(value), encoding="utf-8")
        (inputs / "report.md").write_text("# report\n", encoding="utf-8")
        options = {
            "state_dir": self.root, "input_path": inputs / "input.json",
            "prepared_path": inputs / "prepared.json", "judgments_path": inputs / "judgments.json",
            "report_path": inputs / "report.md", "artifact_paths": [inputs / "ci-failure-triage.json"],
        }
        record_poc.record_poc_cycle(**options)
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        (inputs / "ci-failure-triage.json").write_text(
            json.dumps({**triage, "ruleVersion": "different"}), encoding="utf-8",
        )
        with self.assertRaisesRegex(HistoryError, "triage"):
            record_poc.record_poc_cycle(**options)
        self.assertEqual(
            before,
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
        )

    def test_record_poc_cycle_rejects_malformed_triage_before_any_state_write(self) -> None:
        self.root.mkdir(parents=True)
        preserved = {
            "current.json": b'{"runId":"preserved"}\n',
            "runs/preserved/manifest.json": b'{"complete":true}\n',
            "ledgers/fingerprints.jsonl": b'{"preserved":true}\n',
        }
        for relative, content in preserved.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        inputs = self.root.parent / "record-inputs"
        inputs.mkdir()
        paths = {
            "input": inputs / "input.json",
            "prepared": inputs / "prepared.json",
            "judgments": inputs / "judgments.json",
            "report": inputs / "report.md",
        }
        paths["input"].write_text(json.dumps(snapshot()), encoding="utf-8")
        prepared = prepared_assessment()
        prepared["triageRuleVersion"] = "aspire-ci-triage-v1"
        prepared["issues"][0]["ciFailureTriage"] = {
            "ruleVersion": "aspire-ci-triage-v2",
            "cases": [],
        }
        paths["prepared"].write_text(json.dumps(prepared), encoding="utf-8")
        paths["judgments"].write_text(json.dumps(poc_judgments()), encoding="utf-8")
        paths["report"].write_text("# report\n", encoding="utf-8")

        with self.assertRaisesRegex(HistoryError, "ruleVersion"):
            record_poc.record_poc_cycle(
                state_dir=self.root,
                input_path=paths["input"],
                prepared_path=paths["prepared"],
                judgments_path=paths["judgments"],
                report_path=paths["report"],
                artifact_paths=[],
            )

        self.assertEqual(
            preserved,
            {
                relative: (self.root / relative).read_bytes()
                for relative in preserved
            },
        )

    def test_record_poc_cycle_rejects_dangling_and_mismatched_triage_before_state_write(
        self,
    ) -> None:
        raw = workflow_snapshot()
        raw["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        prepared = prepare_assessment(raw)
        prepared = attach_ci_failure_triage(
            prepared,
            build_ci_failure_triage(prepared),
        )
        compact = build_compact_poc_input(prepared)
        judgments = {
            "schemaVersion": 1,
            "snapshotId": prepared["snapshotId"],
            "issues": [compact["issues"][0]["defaultJudgment"]],
        }

        mutations = {}
        dangling = json.loads(json.dumps(prepared))
        dangling["observations"]["occurrences"] = []
        mutations["dangling"] = dangling
        mismatched = json.loads(json.dumps(prepared))
        family = mismatched["issues"][0]["ciFailureTriage"]["cases"][0]["family"]
        family["dimensions"]["workflowId"] = "not-a-workflow"
        family["dimensions"]["testName"] = "Foreign.Test"
        family["familyId"] = _fingerprint(family["dimensions"])
        mutations["mismatched"] = mismatched

        for name, candidate in mutations.items():
            with self.subTest(name=name):
                inputs = self.root.parent / f"record-inputs-{name}"
                inputs.mkdir()
                paths = {
                    "input": inputs / "input.json",
                    "prepared": inputs / "prepared.json",
                    "judgments": inputs / "judgments.json",
                    "report": inputs / "report.md",
                }
                paths["input"].write_text(json.dumps(raw), encoding="utf-8")
                paths["prepared"].write_text(json.dumps(candidate), encoding="utf-8")
                paths["judgments"].write_text(json.dumps(judgments), encoding="utf-8")
                paths["report"].write_text("# report\n", encoding="utf-8")
                state = self.root / name

                with self.assertRaises((HistoryError, KeyError, ValueError)):
                    record_poc.record_poc_cycle(
                        state_dir=state,
                        input_path=paths["input"],
                        prepared_path=paths["prepared"],
                        judgments_path=paths["judgments"],
                        report_path=paths["report"],
                        artifact_paths=[],
                    )

                self.assertFalse(state.exists())

    def test_load_recorded_run_rejects_malformed_persisted_triage(self) -> None:
        record_poc_history(
            self.root,
            "owner/repo",
            "cycle-001",
            snapshot(),
            prepared_assessment(),
            poc_judgments(),
            "# CI Shepherd POC Assessment\n",
        )
        run_directory = self.root / "runs" / "cycle-001"
        prepared_path = run_directory / "assessment-input.json"
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        prepared["triageRuleVersion"] = "aspire-ci-triage-v1"
        prepared["issues"][0]["ciFailureTriage"] = {
            "ruleVersion": "aspire-ci-triage-v2",
            "cases": [],
        }
        content = (
            json.dumps(prepared, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        prepared_path.write_bytes(content)

        manifest_path = run_directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(
            item
            for item in manifest["files"]
            if item["path"] == "assessment-input.json"
        )
        entry["size"] = len(content)
        entry["crc32"] = f"{zlib.crc32(content):08x}"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(HistoryError, "ruleVersion"):
            load_recorded_run(self.root, "owner/repo", "cycle-001")

    def test_missing_or_malformed_current_is_rebuilt_from_runs(self) -> None:
        expected = record_history(self.root, "owner/repo", "run-001", snapshot(), report())
        (self.root / "current.json").unlink()
        self.assertEqual(load_current(self.root, "owner/repo"), expected)

        (self.root / "current.json").write_text("{not-json")
        self.assertEqual(load_current(self.root, "owner/repo"), expected)
        self.assertEqual(json.loads((self.root / "current.json").read_text())["runId"], "run-001")

    def test_corrupt_latest_run_falls_back_to_latest_valid_run(self) -> None:
        older_snapshot = snapshot()
        older_snapshot["collectedAt"] = "2026-08-18T00:00:00Z"
        older = record_history(
            self.root,
            "owner/repo",
            "run-001",
            older_snapshot,
            report(),
        )
        record_history(self.root, "owner/repo", "run-002", snapshot(), report())
        (self.root / "runs" / "run-002" / "report.json").write_bytes(b"{corrupt")

        recovered = load_current(self.root, "owner/repo")

        self.assertEqual(recovered, older)
        self.assertEqual(json.loads((self.root / "current.json").read_text())["runId"], "run-001")

    def test_malformed_runs_are_not_silently_accepted(self) -> None:
        malformed = self.root / "runs" / "run-001"
        malformed.mkdir(mode=0o700, parents=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.root / "runs", 0o700)
        (malformed / "manifest.json").write_text("{not-json")

        with self.assertRaisesRegex(HistoryError, "no valid immutable runs"):
            load_current(self.root, "owner/repo")

    def test_interrupted_staging_directory_is_ignored(self) -> None:
        expected = record_history(self.root, "owner/repo", "run-001", snapshot(), report())
        (self.root / "current.json").unlink()
        staging = self.root / "runs" / ".run-999.staging-interrupted"
        staging.mkdir(mode=0o700)
        (staging / "manifest.json").write_text("{not-json")

        self.assertEqual(load_current(self.root, "owner/repo"), expected)

    def test_only_interrupted_staging_directory_yields_empty_history(self) -> None:
        staging = self.root / "runs" / ".run-999.staging-interrupted"
        staging.mkdir(mode=0o700, parents=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.root / "runs", 0o700)
        (staging / "manifest.json").write_text("{not-json")

        self.assertIsNone(load_current(self.root, "owner/repo"))

    def test_latest_valid_run_is_selected_by_observation_then_run_id(self) -> None:
        latest_snapshot = snapshot()
        latest_snapshot["collectedAt"] = "2026-08-20T00:00:00Z"
        expected = record_history(
            self.root,
            "owner/repo",
            "run-001",
            latest_snapshot,
            report(),
        )
        older_snapshot = snapshot()
        older_snapshot["collectedAt"] = "2026-08-18T00:00:00Z"
        record_history(self.root, "owner/repo", "run-999", older_snapshot, report())
        (self.root / "current.json").unlink()

        self.assertEqual(load_current(self.root, "owner/repo"), expected)

    def test_current_update_failure_recovers_from_immutable_run(self) -> None:
        from ci_shepherd import history

        real_replace = os.replace

        def fail_current(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(destination).name == "current.json":
                raise OSError("injected current failure")
            real_replace(source, destination)

        with mock.patch.object(history.os, "replace", side_effect=fail_current):
            with self.assertRaisesRegex(HistoryError, "current.json"):
                record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertTrue((self.root / "runs" / "run-001" / "manifest.json").is_file())
        self.assertEqual(load_current(self.root, "owner/repo").run_id, "run-001")

    def test_current_replacement_uses_temporary_file_in_current_parent(self) -> None:
        from ci_shepherd import history

        real_write = history._write_new_private_file
        temporary_parents: list[Path] = []

        def capture_write(path: Path, content: bytes) -> None:
            if path.name.startswith(".current.json.tmp-"):
                temporary_parents.append(path.parent)
            real_write(path, content)

        with mock.patch.object(history, "_write_new_private_file", side_effect=capture_write):
            record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertEqual(temporary_parents, [self.root])

    def test_nested_artifact_directories_are_synced_bottom_up_before_promotion(self) -> None:
        from ci_shepherd import history

        real_rename = history.os.rename
        synced: list[Path] = []

        def assert_synced_before_rename(source: Path, destination: Path) -> None:
            staging = Path(source)
            self.assertEqual(
                synced,
                [
                    staging / "logs" / "archive",
                    staging / "reports" / "detail",
                    staging / "logs",
                    staging / "reports",
                    staging,
                ],
            )
            real_rename(source, destination)

        with (
            mock.patch.object(history, "_fsync_directory", side_effect=synced.append),
            mock.patch.object(history.os, "rename", side_effect=assert_synced_before_rename),
        ):
            record_history(
                self.root,
                "owner/repo",
                "run-001",
                snapshot(),
                report(),
                {
                    "logs/archive/job.txt": b"failure\n",
                    "reports/detail/report.md": b"# Report\n",
                },
            )

    def test_permissions_are_private_under_permissive_umask(self) -> None:
        previous_umask = os.umask(0)
        try:
            record_history(
                self.root,
                "owner/repo",
                "run-001",
                snapshot(),
                report(),
                {"nested/artifact.bin": b"content"},
            )
        finally:
            os.umask(previous_umask)

        directories = [
            self.root,
            self.root / "runs",
            self.root / "runs" / "run-001",
            self.root / "runs" / "run-001" / "nested",
        ]
        files = list((self.root / "runs" / "run-001").rglob("*")) + [self.root / "current.json"]
        for directory in directories:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        for file in (path for path in files if path.is_file()):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600, file)

    def test_rejects_repository_mismatch_before_creating_state(self) -> None:
        for requested, input_snapshot, input_report in (
            ("other/repo", snapshot(), report()),
            ("owner/repo", snapshot(repository="other/repo"), report(repository="other/repo")),
            ("owner/repo", snapshot(), report(repository="other/repo")),
        ):
            with self.subTest(requested=requested, snapshot=input_snapshot["repository"]):
                with self.assertRaises((HistoryError, ValueError)):
                    record_history(
                        self.root,
                        requested,
                        "run-001",
                        input_snapshot,
                        input_report,
                    )
                self.assertFalse(self.root.exists())

    def test_rejects_invalid_snapshot_or_report_before_creating_state(self) -> None:
        invalid_snapshot = snapshot()
        invalid_snapshot["openIssues"] = [1, 1]
        invalid_report = report()
        invalid_report["decisions"] = []

        for input_snapshot, input_report in (
            (invalid_snapshot, report()),
            (snapshot(), invalid_report),
        ):
            with self.subTest():
                with self.assertRaises((HistoryError, ValueError)):
                    record_history(
                        self.root,
                        "owner/repo",
                        "run-001",
                        input_snapshot,
                        input_report,
                    )
                self.assertFalse(self.root.exists())

    def test_rejects_run_id_traversal_and_unsafe_types(self) -> None:
        for run_id in ("../escape", "a/b", ".", "..", ".hidden", "a\\b", "", b"run-001"):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(HistoryError, "Run ID"):
                    record_history(
                        self.root,
                        "owner/repo",
                        run_id,  # type: ignore[arg-type]
                        snapshot(),
                        report(),
                    )
                self.assertFalse(self.root.exists())

    def test_rejects_artifact_traversal_reserved_names_and_aliases(self) -> None:
        unsafe_artifacts = (
            [("../escape", b"x")],
            [("/absolute", b"x")],
            [("a/../../escape", b"x")],
            [("a\\b", b"x")],
            [("manifest.json", b"x")],
            [("Report.md", b"x"), ("report.md", b"y")],
            [("café.txt", b"x"), ("cafe\u0301.txt", b"y")],
            [("logs", b"x"), ("logs/job.txt", b"y")],
            [("Logs/job.txt", b"x"), ("logs", b"y")],
        )
        for artifacts in unsafe_artifacts:
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(HistoryError):
                    record_history(
                        self.root,
                        "owner/repo",
                        "run-001",
                        snapshot(),
                        report(),
                        artifacts,
                    )
                self.assertFalse(self.root.exists())

    def test_rejects_noncanonical_artifact_path_before_creating_state(self) -> None:
        with self.assertRaisesRegex(HistoryError, "canonical"):
            record_history(
                self.root,
                "owner/repo",
                "run-001",
                snapshot(),
                report(),
                {"./a.txt": b"content"},
            )

        self.assertFalse(self.root.exists())

    def test_rejects_duplicate_artifact_names_and_invalid_content_types(self) -> None:
        for artifacts in (
            [("same.bin", b"one"), ("same.bin", b"two")],
            [(b"name.bin", b"content")],
            [("name.bin", "content")],
            [("name.bin", bytearray(b"content"))],
        ):
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(HistoryError):
                    record_history(
                        self.root,
                        "owner/repo",
                        "run-001",
                        snapshot(),
                        report(),
                        artifacts,  # type: ignore[arg-type]
                    )
                self.assertFalse(self.root.exists())

    def test_rejects_non_json_compatible_records_before_creating_state(self) -> None:
        input_snapshot = snapshot()
        input_snapshot["evidence"]["issue:1"]["payload"]["bad"] = {object()}

        with self.assertRaisesRegex(HistoryError, "JSON-compatible"):
            record_history(
                self.root,
                "owner/repo",
                "run-001",
                input_snapshot,
                report(),
            )

        self.assertFalse(self.root.exists())

    def test_rejects_nonfinite_json_numbers_before_creating_state(self) -> None:
        for record_type in ("snapshot", "report"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(record_type=record_type, value=value):
                    input_snapshot = snapshot()
                    input_report = report()
                    if record_type == "snapshot":
                        input_snapshot["evidence"]["issue:1"]["payload"]["value"] = value
                    else:
                        input_report["nonfinite"] = value

                    with self.assertRaisesRegex(HistoryError, "JSON-compatible"):
                        record_history(
                            self.root,
                            "owner/repo",
                            "run-001",
                            input_snapshot,
                            input_report,
                        )

                    self.assertFalse(self.root.exists())

    def test_rejects_run_id_alias_collision(self) -> None:
        record_history(self.root, "owner/repo", "Run-001", snapshot(), report())

        with self.assertRaisesRegex(HistoryError, "aliases"):
            record_history(self.root, "owner/repo", "run-001", snapshot(), report())

    def test_rejects_symlink_that_redirects_runs_outside_state(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        self.root.mkdir()
        (self.root / "runs").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(HistoryError, "symlink"):
            record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertEqual(list(outside.iterdir()), [])

    def test_dangling_state_symlink_is_rejected_by_load_and_prepare(self) -> None:
        missing_target = Path(self.temporary_directory.name) / "missing-state"
        self.root.symlink_to(missing_target, target_is_directory=True)

        operations = (
            lambda: load_current(self.root, "owner/repo"),
            lambda: record_history(
                self.root,
                "owner/repo",
                "run-001",
                snapshot(),
                report(),
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(HistoryError, "symlink"):
                    operation()

        self.assertFalse(missing_target.exists())

    def test_dangling_runs_symlink_is_rejected_by_load_and_prepare(self) -> None:
        missing_target = Path(self.temporary_directory.name) / "missing-runs"
        self.root.mkdir(mode=0o700)
        (self.root / "runs").symlink_to(missing_target, target_is_directory=True)

        operations = (
            lambda: load_current(self.root, "owner/repo"),
            lambda: record_history(
                self.root,
                "owner/repo",
                "run-001",
                snapshot(),
                report(),
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(HistoryError, "symlink"):
                    operation()

        self.assertFalse(missing_target.exists())

    def test_cold_start_concurrent_recorders_create_distinct_recoverable_runs(self) -> None:
        from ci_shepherd import history

        start = threading.Barrier(8)

        def record(index: int) -> str:
            run_id = f"run-{index:03d}"
            start.wait()
            record_history(
                self.root,
                "owner/repo",
                run_id,
                snapshot(),
                report(),
            )
            return run_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            run_ids = set(executor.map(record, range(8)))

        self.assertEqual(run_ids, {f"run-{index:03d}" for index in range(8)})
        self.assertEqual(
            {path.name for path in (self.root / "runs").iterdir()},
            run_ids,
        )
        valid_runs, invalid_runs = history._scan_valid_runs(
            self.root / "runs",
            "owner/repo",
        )
        self.assertEqual(invalid_runs, [])
        self.assertEqual({run["runId"] for run in valid_runs}, run_ids)
        self.assertIn(load_current(self.root, "owner/repo").run_id, run_ids)

    def test_failed_staging_write_is_cleaned_and_run_id_can_be_reused(self) -> None:
        from ci_shepherd import history

        real_write = history._write_new_private_file

        def fail_report(path: Path, content: bytes) -> None:
            if path.name == "report.json":
                raise OSError("injected staging failure")
            real_write(path, content)

        with mock.patch.object(history, "_write_new_private_file", side_effect=fail_report):
            with self.assertRaisesRegex(HistoryError, "injected staging failure"):
                record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertEqual(list((self.root / "runs").iterdir()), [])
        self.assertEqual(
            record_history(self.root, "owner/repo", "run-001", snapshot(), report()).run_id,
            "run-001",
        )

    def test_valid_json_content_tamper_is_rejected_by_manifest_checksum(self) -> None:
        record_history(self.root, "owner/repo", "run-001", snapshot(), report())
        report_path = self.root / "runs" / "run-001" / "report.json"
        tampered = report_path.read_text().replace("Keep observing.", "Wait observing.")
        json.loads(tampered)
        report_path.write_text(tampered)
        os.chmod(report_path, 0o600)

        with self.assertRaisesRegex(HistoryError, "no valid immutable runs"):
            load_current(self.root, "owner/repo")

    def test_extra_file_invalidates_run(self) -> None:
        record_history(self.root, "owner/repo", "run-001", snapshot(), report())
        extra = self.root / "runs" / "run-001" / "extra.txt"
        extra.write_bytes(b"extra")
        os.chmod(extra, 0o600)

        with self.assertRaisesRegex(HistoryError, "no valid immutable runs"):
            load_current(self.root, "owner/repo")

    def test_incomplete_manifest_and_run_id_directory_mismatch_are_rejected(self) -> None:
        for mutation in ("incomplete", "mismatched"):
            with self.subTest(mutation=mutation):
                state = self.root / mutation
                record_history(state, "owner/repo", "run-001", snapshot(), report())
                manifest_path = state / "runs" / "run-001" / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                if mutation == "incomplete":
                    manifest["complete"] = False
                else:
                    manifest["runId"] = "run-002"
                manifest_path.write_text(json.dumps(manifest))
                os.chmod(manifest_path, 0o600)

                with self.assertRaisesRegex(HistoryError, "no valid immutable runs"):
                    load_current(state, "owner/repo")

    def test_persists_all_freshness_classes_and_source_timestamps(self) -> None:
        full_snapshot = snapshot(
            evidence={
                "issue:1": evidence_record(
                    payload={"number": 1, "updatedAt": "2026-08-18T11:00:00Z"},
                ),
                f"commit:{'a' * 40}": evidence_record("commit"),
                "source:src%2Fapp.py": evidence_record(
                    "source-path",
                    payload={"updatedAt": "2026-08-18T12:00:00Z"},
                ),
                "issue:1:comment:2": evidence_record(
                    "issue-comment",
                    payload={"updatedAt": "2026-08-18T12:30:00Z"},
                ),
                "run:2": evidence_record("workflow-run"),
                "run:4": evidence_record(
                    "workflow-run",
                    payload={"runId": 4, "status": "completed"},
                ),
                "pr:5": evidence_record(
                    "pull-request",
                    payload={
                        "number": 5,
                        "state": "closed",
                        "mergedAt": "2026-08-18T10:00:00Z",
                    },
                ),
                "issue:2": evidence_record(
                    payload={"source": "derived", "updatedAt": "2026-08-18T13:00:00Z"},
                ),
                "run:3": evidence_record("workflow-run", availability="partial"),
                "run:6": evidence_record(
                    "workflow-run",
                    payload={"errorRetryable": False},
                    availability="partial",
                ),
            }
        )

        current = record_history(
            self.root,
            "owner/repo",
            "run-001",
            full_snapshot,
            report(),
        )

        self.assertEqual(
            FRESHNESS_CLASSES,
            ("immutable", "source-versioned", "volatile", "derived", "retryable"),
        )
        evidence = current.evidence
        self.assertEqual(evidence["issue:1"]["freshnessClass"], "source-versioned")
        self.assertEqual(evidence[f"commit:{'a' * 40}"]["freshnessClass"], "immutable")
        self.assertEqual(evidence["source:src%2Fapp.py"]["freshnessClass"], "source-versioned")
        self.assertEqual(evidence["issue:1:comment:2"]["freshnessClass"], "source-versioned")
        self.assertEqual(evidence["run:2"]["freshnessClass"], "volatile")
        self.assertEqual(evidence["run:4"]["freshnessClass"], "immutable")
        self.assertEqual(evidence["pr:5"]["freshnessClass"], "immutable")
        self.assertEqual(evidence["issue:2"]["freshnessClass"], "derived")
        self.assertEqual(evidence["run:3"]["freshnessClass"], "retryable")
        self.assertEqual(evidence["run:6"]["freshnessClass"], "immutable")
        self.assertEqual(evidence["issue:1"]["observedAt"], OBSERVED_AT)
        self.assertEqual(
            evidence["source:src%2Fapp.py"]["sourceUpdatedAt"],
            "2026-08-18T12:00:00Z",
        )

    def test_previous_decisions_are_isolated_from_factual_evidence(self) -> None:
        current = record_history(self.root, "owner/repo", "run-001", snapshot(), report())

        self.assertEqual(current.previous_decisions, report()["decisions"])
        self.assertNotIn("decisions", current.evidence)
        self.assertTrue(
            all(
                record.get("payload", {}).get("source") != "previous-report"
                for record in current.evidence.values()
            )
        )
        persisted = json.loads((self.root / "current.json").read_text())
        self.assertIn("previousDecisions", persisted)
        self.assertNotIn("previousDecisions", persisted["evidence"])

    def test_rejects_previous_report_records_inside_snapshot_evidence(self) -> None:
        input_snapshot = snapshot()
        input_snapshot["evidence"]["issue:1"]["payload"]["source"] = "previous-report"

        with self.assertRaisesRegex(HistoryError, "previous report"):
            record_history(
                self.root,
                "owner/repo",
                "run-001",
                input_snapshot,
                report(),
            )

        self.assertFalse(self.root.exists())

    def test_rejects_previous_report_contamination_anywhere_in_evidence_record(self) -> None:
        contaminations = (
            ("previousDecision", {"state": "close"}),
            ("previousDecisions", [{"state": "close"}]),
            ("source", "previous-report"),
            ("metadata", {"source": "previous-report"}),
        )
        for key, value in contaminations:
            with self.subTest(key=key):
                input_snapshot = snapshot()
                input_snapshot["evidence"]["issue:1"][key] = value

                with self.assertRaisesRegex(HistoryError, "previous report"):
                    record_history(
                        self.root,
                        "owner/repo",
                        "run-001",
                        input_snapshot,
                        report(),
                    )

                self.assertFalse(self.root.exists())

    def test_current_evidence_projects_only_schema_fields(self) -> None:
        input_snapshot = snapshot()
        input_snapshot["evidence"]["issue:1"]["unknownTopLevel"] = "must not persist"

        current = record_history(
            self.root,
            "owner/repo",
            "run-001",
            input_snapshot,
            report(),
        )

        self.assertEqual(
            set(current.evidence["issue:1"]),
            {
                "kind",
                "url",
                "collectedAt",
                "availability",
                "payload",
                "observedAt",
                "freshnessClass",
            },
        )


if __name__ == "__main__":
    unittest.main()
