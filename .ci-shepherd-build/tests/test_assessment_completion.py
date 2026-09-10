from __future__ import annotations

import json
import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cycle
from ci_shepherd.assessment_batches import load_assessment_packets
from ci_shepherd.investigations import _fingerprint
from ci_shepherd.models import stable_json
from tests.assessment_helpers import write_assessment_receipts
from tests.test_assessment_batches import worker_response
from tests.test_cycle import snapshot, pull_request_snapshot


class AssessmentCompletionTests(unittest.TestCase):
    def test_worker_completion_serializes_exact_receipts_after_explicit_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            value = snapshot("2026-09-08T18:00:00Z")
            value["evidence"]["issue:1"]["payload"]["body"] = "Diagnostic context.\n" * 400
            source.write_text(json.dumps(value), encoding="utf-8")
            work = root / "work"
            with patch("ci_shepherd.assessment_batches.MAX_ASSESSMENT_PACKET_BYTES", 2000):
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                )
            manifest, packets = load_assessment_packets(work, started["assessment"])
            group, = manifest["workerGroups"]
            self.assertGreater(len(group["packetFiles"]), 1)
            response_path = work / group["responseFile"]
            response = json.loads(response_path.read_text())
            compact = json.loads((work / "assessment-defaults.json").read_text())
            override = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
            override["recommendations"][0]["summary"] = "Determine the missing failure diagnostic."
            response["issues"] = [override]
            response_path.write_text(json.dumps(response), encoding="utf-8")
            original_receipts = (work / "assessment-receipts.json").read_bytes()

            result = cycle.complete_assessment_response(
                work_dir=work, group_id=group["groupId"],
                assessment_id=manifest["assessmentId"], reviewed_case_ids=["issue:1"],
            )

            completed = json.loads(response_path.read_text())
            self.assertEqual([override], completed["issues"])
            self.assertEqual("complete", completed["status"])
            self.assertEqual(
                worker_response(manifest, packets, group)["batches"], completed["batches"],
            )
            self.assertEqual(1, result["completedCaseCount"])
            self.assertEqual(original_receipts, (work / "assessment-receipts.json").read_bytes())
            self.assertEqual("awaiting-review", json.loads((work / "cycle.json").read_text())["stage"])
            before = response_path.stat().st_mtime_ns
            cycle.complete_assessment_response(
                work_dir=work, group_id=group["groupId"],
                assessment_id=manifest["assessmentId"], reviewed_case_ids=["issue:1"],
            )
            self.assertEqual(before, response_path.stat().st_mtime_ns)
            self.assertEqual("complete", cycle.merge_assessments(work_dir=work)["status"])
            self.assertEqual("completed", cycle.finish_cycle(
                work_dir=work, agent_assessment_path=work / "agent-assessment.json",
            )["stage"])

    def test_worker_completion_rejects_unreviewed_stale_and_invalid_drafts_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "work"
            started = cycle.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            manifest, _ = load_assessment_packets(work, started["assessment"])
            group, = manifest["workerGroups"]
            response_path = work / group["responseFile"]
            initial = response_path.read_bytes()
            for reviewed in ([], ["issue:2"], ["issue:1", "issue:1"]):
                with self.subTest(reviewed=reviewed), self.assertRaises(ValueError):
                    cycle.complete_assessment_response(
                        work_dir=work, group_id=group["groupId"],
                        assessment_id=manifest["assessmentId"], reviewed_case_ids=reviewed,
                    )
                self.assertEqual(initial, response_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "stale"):
                cycle.complete_assessment_response(
                    work_dir=work, group_id=group["groupId"],
                    assessment_id="assessment:stale", reviewed_case_ids=["issue:1"],
                )
            for fields in (
                {"issues": [{"issueNumber": 1, "summary": "Not a judgment"}]},
                {"issues": [{"issueNumber": 2, "summary": "Foreign case"}]},
                {"snapshotId": "stale-snapshot"},
            ):
                response = {**json.loads(initial), **fields}
                response_path.write_text(json.dumps(response), encoding="utf-8")
                before = response_path.read_bytes()
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    cycle.complete_assessment_response(
                        work_dir=work, group_id=group["groupId"],
                        assessment_id=manifest["assessmentId"], reviewed_case_ids=["issue:1"],
                    )
                self.assertEqual(before, response_path.read_bytes())
            self.assertFalse((root / "state/current.json").exists())

    def test_worker_completion_cli_uses_the_frozen_group_binding(self) -> None:
        for factory, case_id in ((snapshot, "issue:1"), (pull_request_snapshot, "pull-request:23")):
            with self.subTest(case_id=case_id), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.json"
                source.write_text(json.dumps(factory("2026-09-08T18:00:00Z")), encoding="utf-8")
                work = root / "work"
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                )
                manifest, _ = load_assessment_packets(work, started["assessment"])
                group, = manifest["workerGroups"]
                command = [*group["completionCommand"], "--reviewed-case", case_id]
                result = subprocess.run(command, check=False, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(group["groupId"], json.loads(result.stdout)["groupId"])
                response_path = work / group["responseFile"]
                response = json.loads(response_path.read_text())
                self.assertEqual("complete", response["status"])
                if case_id == "pull-request:23":
                    response["pullRequests"] = [{
                        "pullRequestNumber": 23, "disposition": "review-close",
                        "summary": "PR closure is not permitted.", "evidenceIds": ["pr:23"],
                    }]
                    response_path.write_text(json.dumps(response), encoding="utf-8")
                    before = response_path.read_bytes()
                    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
                    self.assertNotEqual(0, rejected.returncode)
                    self.assertEqual(before, response_path.read_bytes())

    def test_worker_completion_cannot_override_an_incomplete_group_budget(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "work"
            with patch("ci_shepherd.assessment_batches.MAX_ASSESSMENT_WORKER_BYTES", 100):
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                )
            manifest, _ = load_assessment_packets(work, started["assessment"])
            group, = manifest["workerGroups"]
            path = work / group["responseFile"]
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "remains incomplete"):
                cycle.complete_assessment_response(
                    work_dir=work, group_id=group["groupId"],
                    assessment_id=manifest["assessmentId"], reviewed_case_ids=["issue:1"],
                )
            self.assertEqual(before, path.read_bytes())

    def test_materialization_keeps_full_evidence_once_and_preserves_decision_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "work"
            started = cycle.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            prepared = json.loads((work / "assessment-input.json").read_text())
            compact = json.loads((work / "assessment-defaults.json").read_text())["issues"][0]
            selection = json.loads((work / "review-selection.json").read_text())["selected"][0]
            _, packets = load_assessment_packets(work, started["assessment"])
            parts = [part for packet in packets.values() for part in packet["cases"]]
            case = (
                json.loads("".join(part["input"]["content"] for part in parts))
                if "parentCaseId" in parts[0] else parts[0]
            )
            expected = {
                "caseId": "issue:1",
                "evidenceIds": [record["id"] for record in prepared["issues"][0]["evidenceBundle"]],
                "input": prepared["issues"][0],
                "defaultJudgment": compact["defaultJudgment"],
                "decisionContext": {
                    key: value for key, value in compact.items()
                    if key not in {"allowedEvidence", "defaultJudgment"}
                    and (key not in prepared["issues"][0] or prepared["issues"][0][key] != value)
                },
                "selection": selection,
            }
            self.assertEqual(expected, case)
            legacy = {key: value for key, value in expected.items() if key != "decisionContext"}
            legacy["defaultJudgment"] = compact
            self.assertLess(
                len(stable_json(case).encode("utf-8")),
                len(stable_json(legacy).encode("utf-8")),
            )

    def test_matching_digests_do_not_replace_source_snapshot_identity_validation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "work"
            started = cycle.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            manifest_path = work / "assessment-batches.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["snapshotId"] = "snapshot:owner/repo:other"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected = {**started["assessment"], "manifestFingerprint": _fingerprint(manifest)}
            with self.assertRaisesRegex(ValueError, "snapshot"):
                load_assessment_packets(work, expected)

    def test_failed_or_malformed_revision_probe_has_an_explicit_diagnostic(self) -> None:
        for result, message in (
            (subprocess.CompletedProcess("git", 1, "", "not a checkout"), "not a checkout"),
            (subprocess.CompletedProcess("git", 0, "invalid-head", ""), "invalid commit identity"),
        ):
            with self.subTest(message=message), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.json"
                source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
                with patch.object(cycle.subprocess, "run", return_value=result):
                    started = cycle.start_cycle(
                        repository="owner/repo", state_dir=root / "state", work_dir=root / "work",
                        checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                    )
                self.assertIsNone(started["invocation"]["coordinatorRevision"])
                self.assertIn(message, started["invocation"]["provenanceDiagnostics"][0]["message"])

    def test_revision_probe_is_bounded_sanitized_and_reports_unavailable_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            value = snapshot("2026-09-08T18:00:00Z")
            value["sourceRevision"] = "a" * 40
            source.write_text(json.dumps(value), encoding="utf-8")
            with (
                patch.dict(os.environ, {"GIT_DIR": "/invalid/git", "GIT_CONFIG_COUNT": "1"}),
                patch.object(cycle.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)) as git,
            ):
                started = cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=root / "work",
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                )
            self.assertEqual(10, git.call_args.kwargs["timeout"])
            self.assertEqual(
                {"GIT_OPTIONAL_LOCKS": "0"},
                {key: value for key, value in git.call_args.kwargs["env"].items() if key.startswith("GIT_")},
            )
            self.assertIsNone(started["invocation"]["coordinatorRevision"])
            self.assertEqual(["a" * 40], started["invocation"]["frozenSourceRevisions"])
            diagnostic = started["invocation"]["provenanceDiagnostics"][0]
            self.assertEqual("coordinator", diagnostic["source"])
            self.assertIn("timed out", diagnostic["message"])

    def test_replay_mints_new_receipt_identity_even_for_identical_snapshot_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            manifests = []
            for name in ("first", "second"):
                manifests.append(cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=root / name,
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                ))
            write_assessment_receipts(root / "first")
            self.assertEqual(manifests[0]["snapshotId"], manifests[1]["snapshotId"])
            self.assertNotEqual(manifests[0]["assessment"]["assessmentId"], manifests[1]["assessment"]["assessmentId"])
            with self.assertRaisesRegex(ValueError, "stale"):
                cycle.finish_cycle(
                    work_dir=root / "second",
                    agent_assessment_path=root / "second/agent-assessment.json",
                    assessment_receipts_path=root / "first/assessment-receipts.json",
                )
            self.assertFalse((root / "second/judgments.json").exists())

    def test_large_mixed_inventory_requires_all_121_issue_and_27_pr_receipts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-09-08T18:00:00Z")
            issue = value["evidence"].pop("issue:1")
            value["openIssues"] = list(range(1, 122))
            for number in value["openIssues"]:
                record = copy.deepcopy(issue)
                url = f"https://github.com/owner/repo/issues/{number}"
                record["url"] = url
                record["payload"].update(number=number, url=url, title=f"Failure {number}")
                value["evidence"][f"issue:{number}"] = record
            pr_value = pull_request_snapshot(value["collectedAt"])
            value["openPullRequests"] = list(range(1001, 1028))
            for number in value["openPullRequests"]:
                pr = copy.deepcopy(pr_value["pullRequests"][0])
                url = f"https://github.com/owner/repo/pull/{number}"
                pr.update(number=number, url=url)
                value["pullRequests"].append(pr)
                record = copy.deepcopy(pr_value["evidence"]["pr:23"])
                record["url"] = url
                record["payload"]["number"] = number
                value["evidence"][f"pr:{number}"] = record
            source = root / "source.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            work = root / "work"
            started = cycle.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            self.assertEqual(121, started["issueReviewCount"])
            self.assertEqual(27, started["pullRequestReviewCount"])
            batches = json.loads((work / "assessment-batches.json").read_text())
            self.assertEqual(148, batches["caseCount"])
            for batch in batches["batches"]:
                path = work / batch["file"]
                self.assertLessEqual(len(path.read_bytes()), 16_000)
                self.assertLessEqual(len(json.loads(path.read_text())["cases"]), 10)
            receipts = write_assessment_receipts(work)
            receipts["batches"][-1]["cases"].pop()
            (work / "assessment-receipts.json").write_text(json.dumps(receipts), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing cases"):
                cycle.finish_cycle(work_dir=work, agent_assessment_path=work / "agent-assessment.json")
            self.assertFalse((root / "state/current.json").exists())
            for group in batches["workerGroups"]:
                worker_packets = {
                    name: json.loads((work / name).read_text()) for name in group["packetFiles"]
                }
                response = worker_response(batches, worker_packets, group)
                (work / group["responseFile"]).write_text(json.dumps(response), encoding="utf-8")
            for options in (["--response", str(work / batches["workerGroups"][0]["responseFile"])], []):
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", ["cycle.py", "merge-assessments", "--work-dir", str(work), *options]),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(0, cycle.main())
                self.assertEqual("incomplete" if options else "complete", json.loads(output.getvalue())["status"])
                if options:
                    with self.assertRaisesRegex(ValueError, "missing"):
                        cycle.finish_cycle(work_dir=work, agent_assessment_path=work / "agent-assessment.json")
            result = cycle.finish_cycle(work_dir=work, agent_assessment_path=work / "agent-assessment.json")
            self.assertEqual(121, result["assessment"]["issueCount"])
            self.assertEqual(27, result["assessment"]["pullRequestCount"])
            self.assertIn("121 issues / 27 PRs with assessment acknowledgements", (work / "report.md").read_text())

    def test_changed_packets_inputs_and_legacy_cycles_cannot_reuse_receipts(self) -> None:
        for changed in ("assessment-input.json", "assessment-batch-0001.json", "legacy-cycle"):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.json"
                source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
                work = root / "work"
                cycle.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="shepherd[bot]", input_path=source,
                )
                write_assessment_receipts(work)
                target = work / ("cycle.json" if changed == "legacy-cycle" else changed)
                document = json.loads(target.read_text())
                if changed == "legacy-cycle":
                    document.pop("assessment")
                else:
                    document["changedAfterReview"] = True
                target.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed|Legacy cycle"):
                    cycle.finish_cycle(work_dir=work, agent_assessment_path=work / "agent-assessment.json")
                self.assertFalse((work / "judgments.json").exists())
                self.assertFalse((root / "state/current.json").exists())

    def test_cli_uses_canonical_state_by_default_and_preserves_explicit_state(self) -> None:
        for supplied in ([], ["--state-dir", "isolated-state"]):
            with self.subTest(supplied=supplied):
                with (
                    patch.object(sys, "argv", [
                        "cycle.py", "start", "--repository", "owner/repo",
                        "--shepherd-author", "shepherd[bot]", *supplied,
                    ]),
                    patch.object(cycle, "start_cycle", return_value={}) as start,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(0, cycle.main())
                expected = Path("isolated-state") if supplied else Path.home() / ".copilot/ci-shepherd/state"
                self.assertEqual(expected, start.call_args.kwargs["state_dir"])

    def test_explicit_receipts_record_coverage_and_expose_bootstrap_resume_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "first"
            state = root / "state"
            started = cycle.start_cycle(
                repository="owner/repo", state_dir=state, work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            original = source.read_bytes()
            write_assessment_receipts(work)
            completed = cycle.finish_cycle(
                work_dir=work, agent_assessment_path=work / "agent-assessment.json",
            )
            self.assertEqual("completed", completed["stage"])
            self.assertEqual(1, completed["assessment"]["caseCount"])
            self.assertEqual("bootstrap", started["invocation"]["stateMode"])
            self.assertEqual("replay", started["invocation"]["collectionMode"])
            self.assertEqual("explicit", started["invocation"]["stateOrigin"])
            self.assertRegex(started["invocation"]["coordinatorRevision"], r"^[0-9a-f]{40}$")
            recorded = Path(completed["runDirectory"])
            self.assertEqual(
                (work / "assessment-receipts.json").read_bytes(),
                (recorded / "assessment-receipts.json").read_bytes(),
            )
            self.assertEqual(
                [1], json.loads((recorded / "assessment-completion.json").read_text())["completedIssueNumbers"],
            )
            self.assertEqual(original, source.read_bytes())
            source.write_text(json.dumps(snapshot("2026-09-08T19:00:00Z")), encoding="utf-8")
            resumed = cycle.start_cycle(
                repository="owner/repo", state_dir=state, work_dir=root / "second",
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            self.assertEqual("resume", resumed["invocation"]["stateMode"])
            self.assertEqual("completed", resumed["stage"])
            self.assertEqual(0, resumed["assessment"]["caseCount"])

    def test_empty_overrides_without_case_receipts_do_not_finalize_or_record_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps(snapshot("2026-09-08T18:00:00Z")), encoding="utf-8")
            work = root / "work"
            state = root / "state"
            cycle.start_cycle(
                repository="owner/repo", state_dir=state, work_dir=work,
                checkout=None, shepherd_author="shepherd[bot]", input_path=source,
            )
            before = {
                str(path.relative_to(state)): path.read_bytes()
                for path in state.rglob("*") if path.is_file()
            }
            with self.assertRaisesRegex(ValueError, "receipt"):
                cycle.finish_cycle(
                    work_dir=work, agent_assessment_path=work / "agent-assessment.json",
                )
            self.assertEqual("awaiting-review", json.loads((work / "cycle.json").read_text())["stage"])
            self.assertFalse((work / "judgments.json").exists())
            self.assertEqual(before, {
                str(path.relative_to(state)): path.read_bytes()
                for path in state.rglob("*") if path.is_file()
            })
