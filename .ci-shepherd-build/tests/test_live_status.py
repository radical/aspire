from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.live_status import (
    MAX_LIVE_STATUS_BYTES,
    build_live_status,
    is_terminal,
    render_live_status_html,
    render_live_status_markdown,
    write_live_status,
)


class LiveStatusTests(unittest.TestCase):
    def test_projects_collection_and_assessment_progress_without_claiming_worker_launches(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "primary"
            work.mkdir()
            (root / "invocation.json").write_text(json.dumps({
                "schemaVersion": 1,
                "invocationId": "manual-1",
                "repository": "microsoft/aspire",
                "mode": "action-free",
                "status": "running",
            }))
            (work / "progress.json").write_text(json.dumps({
                "schemaVersion": 1,
                "status": "complete",
                "events": [
                    {
                        "timestamp": "2026-09-10T20:00:00Z",
                        "stage": "collection",
                        "status": "completed",
                        "message": "Collected evidence.",
                    },
                ],
            }))
            (work / "cycle.json").write_text(json.dumps({
                "schemaVersion": 1,
                "repository": "microsoft/aspire",
                "stage": "awaiting-review",
                "assessment": "assessment-batches.json",
            }))
            (work / "assessment-batches.json").write_text(json.dumps({
                "schemaVersion": 1,
                "assessmentId": "assessment:1",
                "caseCount": 3,
                "workerGroups": [
                    {
                        "groupId": "group:1",
                        "caseIds": ["issue:1", "issue:2"],
                        "status": "ready",
                        "reason": None,
                        "responseFile": "assessment-response-0001.json",
                    },
                    {
                        "groupId": "group:2",
                        "caseIds": ["issue:3"],
                        "status": "ready",
                        "reason": None,
                        "responseFile": "assessment-response-0002.json",
                    },
                ],
            }))
            (work / "assessment-response-0001.json").write_text(json.dumps({
                "schemaVersion": 1,
                "status": "complete",
            }))

            status = build_live_status(
                root,
                work,
                now=lambda: datetime(2026, 9, 10, 20, 1, tzinfo=UTC),
            )

            self.assertTrue(status["advisoryOnly"])
            self.assertEqual("assessment", status["phase"]["id"])
            self.assertEqual("running", status["phase"]["state"])
            self.assertEqual(1, status["assessment"]["completedGroups"])
            self.assertEqual(2, status["assessment"]["totalGroups"])
            self.assertEqual("complete", status["workers"][0]["state"])
            self.assertEqual("awaiting-observation", status["workers"][1]["state"])
            self.assertEqual("artifact-observed", status["workers"][0]["basis"])

    def test_projection_is_bounded_and_renderers_escape_untrusted_text(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "primary"
            work.mkdir()
            dangerous = "<script>alert('x')</script>"
            (root / "invocation.json").write_text(json.dumps({
                "schemaVersion": 1,
                "invocationId": "manual-1",
                "repository": dangerous,
                "mode": "action-free",
                "status": "running",
            }))
            (work / "progress.json").write_text(json.dumps({
                "schemaVersion": 1,
                "status": "running",
                "events": [
                    {
                        "timestamp": f"2026-09-10T20:00:{index % 60:02d}Z",
                        "stage": f"stage-{index}",
                        "status": "progress",
                        "message": dangerous * 200,
                    }
                    for index in range(100)
                ],
            }))

            status = build_live_status(root, work)
            markdown = render_live_status_markdown(status)
            html = render_live_status_html(status)

            self.assertLessEqual(
                len(json.dumps(status, sort_keys=True, separators=(",", ":")).encode("utf-8")),
                MAX_LIVE_STATUS_BYTES,
            )
            self.assertLessEqual(len(status["recentEvents"]), 50)
            self.assertLessEqual(len(markdown.encode("utf-8")), MAX_LIVE_STATUS_BYTES)
            self.assertLessEqual(len(html.encode("utf-8")), MAX_LIVE_STATUS_BYTES)
            self.assertNotIn(dangerous, html)
            self.assertIn("&lt;script", html)
            self.assertIn("\\<script\\>", markdown)

    def test_atomic_writer_creates_private_json_markdown_and_html(self) -> None:
        with TemporaryDirectory() as scratch:
            output = Path(scratch)
            status = {
                "schemaVersion": 1,
                "advisoryOnly": True,
                "generatedAt": "2026-09-10T20:00:00Z",
                "staleAfterSeconds": 30,
                "invocation": {
                    "id": "manual-1",
                    "repository": "microsoft/aspire",
                    "mode": "action-free",
                },
                "phase": {
                    "id": "initialized",
                    "state": "running",
                    "message": "Waiting for collection.",
                    "completedItems": None,
                    "totalItems": None,
                },
                "assessment": None,
                "workers": [],
                "blockers": [],
                "recentEvents": [],
                "artifacts": {},
            }

            write_live_status(output, status)

            for name in ("live-status.json", "live-status.md", "live-status.html"):
                path = output / name
                self.assertTrue(path.is_file())
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(
                status,
                json.loads((output / "live-status.json").read_text(encoding="utf-8")),
            )
            self.assertFalse(any(path.suffix == ".tmp" for path in output.iterdir()))

    def test_projection_does_not_modify_canonical_run_artifacts(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "primary"
            work.mkdir()
            invocation_path = root / "invocation.json"
            progress_path = work / "progress.json"
            invocation_path.write_text(json.dumps({
                "schemaVersion": 1,
                "invocationId": "manual-1",
                "repository": "microsoft/aspire",
                "mode": "action-free",
                "status": "running",
            }))
            progress_path.write_text(json.dumps({
                "schemaVersion": 1,
                "status": "running",
                "events": [],
            }))
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (invocation_path, progress_path)
            }

            write_live_status(root, build_live_status(root, work))

            self.assertEqual(
                before,
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (invocation_path, progress_path)
                },
            )

    def test_assessment_counts_include_groups_beyond_worker_detail_limit(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "primary"
            work.mkdir()
            groups = []
            for index in range(34):
                groups.append({
                    "groupId": f"group:{index + 1}",
                    "caseIds": [f"issue:{index + 1}"],
                    "status": "incomplete" if index == 33 else "ready",
                    "reason": "worker-input-limit" if index == 33 else None,
                    "responseFile": f"assessment-response-{index + 1:04d}.json",
                })
                if index < 33:
                    (work / f"assessment-response-{index + 1:04d}.json").write_text(
                        json.dumps({"schemaVersion": 1, "status": "complete"})
                    )
            (work / "cycle.json").write_text(json.dumps({
                "schemaVersion": 1,
                "repository": "microsoft/aspire",
                "stage": "awaiting-review",
            }))
            (work / "assessment-batches.json").write_text(json.dumps({
                "schemaVersion": 1,
                "assessmentId": "assessment:many",
                "caseCount": 34,
                "workerGroups": groups,
            }))

            status = build_live_status(root, work)

            self.assertEqual(33, status["assessment"]["completedGroups"])
            self.assertEqual(1, status["assessment"]["blockedGroups"])
            self.assertEqual(34, status["assessment"]["totalGroups"])
            self.assertEqual(32, len(status["workers"]))
            self.assertTrue(any(worker["state"] == "blocked" for worker in status["workers"]))

    def test_investigation_status_uses_ledger_observation(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "primary"
            state = root / "state"
            ledger = state / "ledgers" / "investigation-sessions.jsonl"
            work.mkdir()
            ledger.parent.mkdir(parents=True)
            (work / "cycle.json").write_text(json.dumps({
                "schemaVersion": 1,
                "repository": "microsoft/aspire",
                "stateDirectory": str(state),
                "stage": "completed",
            }))
            (work / "investigation-plan.json").write_text(json.dumps({
                "schemaVersion": 1,
                "investigationRequestCount": 1,
                "requests": [{"investigationId": "investigation:1"}],
            }))
            ledger.write_text(json.dumps({
                "schemaVersion": 1,
                "repository": "microsoft/aspire",
                "investigationId": "investigation:1",
                "status": "completed",
                "recordedAt": "2026-09-10T20:00:00Z",
            }) + "\n")

            status = build_live_status(root, work)

            worker = next(worker for worker in status["workers"] if worker["role"] == "investigator")
            self.assertEqual("completed", worker["state"])
            self.assertEqual("ledger-recorded", worker["basis"])
            self.assertEqual("post-assessment", status["phase"]["id"])

    def test_retrospective_outputs_are_terminal_markers(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            self.assertFalse(is_terminal(root))
            (root / "retrospective-error.txt").write_text("retrospective failed\n")
            self.assertTrue(is_terminal(root))


if __name__ == "__main__":
    unittest.main()
