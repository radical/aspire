from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.retrospective import (
    build_retrospective_request,
    normalize_retrospective_result,
    render_retrospective_markdown,
)


class RetrospectiveTests(unittest.TestCase):
    def test_builds_bounded_request_from_completed_run_artifacts(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "run-completion.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "sealedAt": "2026-08-29T12:30:00Z",
                        "actionResults": [],
                        "investigationResults": [],
                        "missingInvestigationIds": [],
                        "unrecordedActionIds": [],
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "action-proposals.json").write_text("{}\n", encoding="utf-8")
            (work_dir / "operator-log.jsonl").write_text(
                '{"event":"action-failed"}\n',
                encoding="utf-8",
            )

            request = build_retrospective_request(
                work_dir,
                reviewed_session_id="session-123",
            )

            self.assertEqual("owner/repo", request["repository"])
            self.assertEqual("session-123", request["reviewedSessionId"])
            self.assertEqual(
                [
                    "action-proposals.json",
                    "cycle.json",
                    "report.md",
                    "run-completion.json",
                ],
                request["evidencePaths"],
            )
            self.assertIn("fresh, read-only reviewer", request["workerPrompt"])
            self.assertIn("Do not access GitHub", request["workerPrompt"])
            self.assertIn("Do not edit code", request["workerPrompt"])
            self.assertIn("Return only JSON", request["workerPrompt"])

    def test_rejects_run_that_has_not_completed_post_action_reconciliation(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "post-action reconciliation",
            ):
                build_retrospective_request(
                    work_dir,
                    reviewed_session_id="session-123",
                )

    def test_rejects_findings_that_cite_evidence_outside_request(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["report.md"],
        }
        result = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "summary": "The run completed with one actionable process gap.",
            "observations": [
                {
                    "severity": "medium",
                    "category": "process",
                    "title": "Action required manual recovery",
                    "detail": "The operator retried a failed action manually.",
                    "recommendation": "Record retry classification in the executor.",
                    "evidencePaths": ["missing.log"],
                }
            ],
            "watchItems": [],
            "successfulSafeguards": [],
        }

        with self.assertRaisesRegex(
            ValueError,
            "outside the retrospective request",
        ):
            normalize_retrospective_result(request, result)

    def test_rejects_result_for_a_different_run(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["report.md"],
        }
        result = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-30T12:00:00Z",
            "reviewedSessionId": "session-123",
            "summary": "This result came from a different run.",
            "observations": [],
            "watchItems": [],
            "successfulSafeguards": [],
        }

        with self.assertRaisesRegex(ValueError, "identity"):
            normalize_retrospective_result(request, result)

    def test_renders_normalized_retrospective_as_markdown(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            "reviewedSessionId": "session-123",
            "evidencePaths": ["operator-log.jsonl", "report.md"],
        }
        result = normalize_retrospective_result(
            request,
            {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                "reviewedSessionId": "session-123",
                "summary": "The run completed and exposed one retry gap.",
                "observations": [
                    {
                        "severity": "medium",
                        "category": "reliability",
                        "title": "Transient failures need classification",
                        "detail": "The operator log contains an unclassified failure.",
                        "recommendation": "Classify retryable transport errors.",
                        "evidencePaths": ["operator-log.jsonl"],
                    }
                ],
                "watchItems": [
                    {
                        "condition": "The same transport error recurs.",
                        "reason": "Repeated failures may require a bounded retry.",
                        "evidencePaths": ["operator-log.jsonl"],
                    }
                ],
                "successfulSafeguards": [
                    {
                        "title": "Report remained evidence-linked",
                        "detail": "Every terminal action was represented in the report.",
                        "evidencePaths": ["report.md"],
                    }
                ],
            },
        )

        markdown = render_retrospective_markdown(request, result)

        self.assertIn("# CI Shepherd Run Retrospective", markdown)
        self.assertIn("## Improvement findings", markdown)
        self.assertIn("Transient failures need classification", markdown)
        self.assertIn("## Watch items", markdown)
        self.assertIn("## Safeguards that worked", markdown)
        self.assertIn("`operator-log.jsonl`", markdown)

    def test_cli_prepares_and_finalizes_retrospective_artifacts(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            (work_dir / "cycle.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "stage": "completed",
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "action-proposals.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "proposals": [
                            {"actionId": "action:current"},
                            {"actionId": "action:unrecorded"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (work_dir / "investigation-plan.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "requests": [
                            {"investigationId": "investigation:complete"},
                            {"investigationId": "investigation:missing"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_dir = work_dir / "state"
            (state_dir / "ledgers").mkdir(parents=True)
            (state_dir / "action-results.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "results": [
                            {
                                "actionId": "action:current",
                                "outcome": "executed",
                            },
                            {
                                "actionId": "action:other-run",
                                "outcome": "executed",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "ledgers" / "investigation-results.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "investigationId": "investigation:complete",
                                "outcome": "recovered",
                            }
                        ),
                        json.dumps(
                            {
                                "investigationId": "investigation:other-run",
                                "outcome": "needs-evidence",
                            }
                        ),
                    ]
                )
                + '\n{"investigationId":"truncated',
                encoding="utf-8",
            )
            result_path = work_dir / "agent-retrospective.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                        "reviewedSessionId": "session-123",
                        "summary": "The run completed without a supported finding.",
                        "observations": [],
                        "watchItems": [],
                        "successfulSafeguards": [],
                    }
                ),
                encoding="utf-8",
            )
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )
            request_path = work_dir / "retrospective-request.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "seal",
                    "--work-dir",
                    str(work_dir),
                    "--state-dir",
                    str(state_dir),
                    "--sealed-at",
                    "2026-08-29T12:30:00Z",
                    "--output",
                    str(work_dir / "run-completion.json"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare",
                    "--work-dir",
                    str(work_dir),
                    "--reviewed-session-id",
                    "session-123",
                    "--output",
                    str(request_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "finalize",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                    "--json-output",
                    str(work_dir / "retrospective.json"),
                    "--markdown-output",
                    str(work_dir / "retrospective.md"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue(request_path.is_file())
            self.assertTrue((work_dir / "retrospective.json").is_file())
            completion = json.loads(
                (work_dir / "run-completion.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["action:current"],
                [entry["actionId"] for entry in completion["actionResults"]],
            )
            self.assertEqual(
                ["investigation:complete"],
                [
                    entry["investigationId"]
                    for entry in completion["investigationResults"]
                ],
            )
            self.assertEqual(
                ["investigation:missing"],
                completion["missingInvestigationIds"],
            )
            self.assertEqual(
                ["action:unrecorded"],
                completion["unrecordedActionIds"],
            )
            for path in (
                work_dir / "run-completion.json",
                request_path,
                work_dir / "retrospective.json",
                work_dir / "retrospective.md",
            ):
                with self.subTest(path=path):
                    self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertIn(
                "The run completed without a supported finding.",
                (work_dir / "retrospective.md").read_text(encoding="utf-8"),
            )

    def test_finalize_rejects_symlink_output_before_writing_any_artifact(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            request = {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
                "reviewedSessionId": "session-123",
                "evidencePaths": ["report.md"],
            }
            result = {
                **request,
                "summary": "The run completed.",
                "observations": [],
                "watchItems": [],
                "successfulSafeguards": [],
            }
            request.pop("summary", None)
            request_path = work_dir / "retrospective-request.json"
            result_path = work_dir / "agent-retrospective.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path.write_text(json.dumps(result), encoding="utf-8")
            outside = work_dir / "outside.md"
            outside.write_text("unchanged\n", encoding="utf-8")
            markdown_output = work_dir / "retrospective.md"
            markdown_output.symlink_to(outside)
            json_output = work_dir / "retrospective.json"
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "finalize",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                    "--json-output",
                    str(json_output),
                    "--markdown-output",
                    str(markdown_output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(json_output.exists())
            self.assertEqual("unchanged\n", outside.read_text(encoding="utf-8"))

    def test_prepare_rejects_overwriting_allowlisted_run_evidence(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            work_dir = Path(scratch)
            identity = {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:2026-08-29T12:00:00Z",
            }
            (work_dir / "cycle.json").write_text(
                json.dumps({**identity, "stage": "completed"}),
                encoding="utf-8",
            )
            (work_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (work_dir / "run-completion.json").write_text(
                json.dumps(
                    {
                        **identity,
                        "sealedAt": "2026-08-29T12:30:00Z",
                        "actionResults": [],
                        "investigationResults": [],
                        "missingInvestigationIds": [],
                        "unrecordedActionIds": [],
                    }
                ),
                encoding="utf-8",
            )
            proposals_path = work_dir / "action-proposals.json"
            proposals_path.write_text(
                json.dumps({**identity, "proposals": []}),
                encoding="utf-8",
            )
            original = proposals_path.read_text(encoding="utf-8")
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_retrospective.py"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare",
                    "--work-dir",
                    str(work_dir),
                    "--reviewed-session-id",
                    "session-123",
                    "--output",
                    str(proposals_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertEqual(original, proposals_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
