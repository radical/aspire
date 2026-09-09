from __future__ import annotations

import copy
import json
import unittest

from ci_shepherd.assessment_batches import (
    build_assessment_batches, merge_worker_responses, validate_assessment_receipts,
)
from ci_shepherd.models import stable_json


def issue_case(number: int) -> dict[str, object]:
    return {
        "caseId": f"issue:{number}",
        "evidenceIds": [f"issue:{number}"],
        "input": {
            "issueNumber": number,
            "evidenceBundle": [{"id": f"issue:{number}", "payload": {"body": "Complete evidence"}}],
        },
        "defaultJudgment": {"disposition": "no-action"},
    }


def worker_response(manifest, packets, group):
    return {
        "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
        "snapshotId": manifest["snapshotId"], "groupId": group["groupId"],
        "status": "complete", "issues": [], "pullRequests": [],
        "batches": [
            {
                "batchId": packets[name]["batchId"],
                "cases": [
                    {"caseId": case["caseId"], "reviewedEvidenceIds": case["evidenceIds"]}
                    for case in packets[name]["cases"]
                ],
            }
            for name in group["packetFiles"]
        ],
    }


class AssessmentBatchTests(unittest.TestCase):
    def test_large_case_fills_packets_instead_of_repeating_small_fragments(self) -> None:
        case = issue_case(1)
        case["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 180_000
        manifest, packets = build_assessment_batches(
            [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        parts = [part for packet in packets.values() for part in packet["cases"]]

        self.assertLessEqual(len(parts), 16)
        self.assertEqual(case, json.loads("".join(part["input"]["content"] for part in parts)))
        for packet in list(packets.values())[:-1]:
            self.assertGreater(len(stable_json(packet).encode("utf-8")), 15_000)
        self.assertTrue(all(
            len(stable_json(packet).encode("utf-8")) <= manifest["maxPacketBytes"]
            for packet in packets.values()
        ))

    def test_fragments_preserve_escaped_unicode_and_large_evidence_lists(self) -> None:
        case = issue_case(1)
        case["evidenceIds"] = [f"evidence:{index}" for index in range(100)]
        case["input"]["evidenceBundle"][0]["payload"]["body"] = (
            '\\path\\"quoted"\n\u00e9\U0001f331' * 2_000
        )
        manifest, packets = build_assessment_batches(
            [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        parts = [part for packet in packets.values() for part in packet["cases"]]

        self.assertGreater(len(parts), 1)
        self.assertEqual(case, json.loads("".join(part["input"]["content"] for part in parts)))
        self.assertEqual(list(range(1, len(parts) + 1)), [part["input"]["partIndex"] for part in parts])
        self.assertTrue(all(part["input"]["partCount"] == len(parts) for part in parts))
        self.assertTrue(all(part["evidenceIds"] == case["evidenceIds"] for part in parts))
        self.assertTrue(all(
            len(stable_json(packet).encode("utf-8")) <= manifest["maxPacketBytes"]
            for packet in packets.values()
        ))
        response = worker_response(manifest, packets, manifest["workerGroups"][0])
        _, receipts, _ = merge_worker_responses(manifest, packets, [response])
        self.assertEqual([1], validate_assessment_receipts(manifest, packets, receipts)["completedIssueNumbers"])

    def test_large_realistic_case_fits_the_bounded_worker_budget_without_splitting_workers(self) -> None:
        case = issue_case(1)
        case["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 180_000
        manifest, packets = build_assessment_batches(
            [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        group, = manifest["workerGroups"]
        self.assertEqual("ready", group["status"])
        self.assertEqual(256_000, manifest["maxWorkerInputBytes"])
        self.assertEqual(["issue:1"], group["caseIds"])
        self.assertGreater(group["byteCount"], 64_000)
        self.assertLessEqual(group["byteCount"], 256_000)
        self.assertTrue(all(
            len(stable_json(packet).encode("utf-8")) <= 16_000
            for packet in packets.values()
        ))
        response = worker_response(manifest, packets, group)
        _, receipts, summary = merge_worker_responses(manifest, packets, [response])
        self.assertEqual("complete", summary["status"])
        self.assertEqual([1], validate_assessment_receipts(manifest, packets, receipts)["completedIssueNumbers"])

    def test_packet_snapshot_identity_must_match_the_assessment_manifest(self) -> None:
        manifest, packets = build_assessment_batches(
            [issue_case(1)], snapshot_id="snapshot:owner/repo:current", source_fingerprints={},
        )
        response = worker_response(manifest, packets, manifest["workerGroups"][0])
        receipts = {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "batches": response["batches"],
        }
        manifest["snapshotId"] = "snapshot:owner/repo:other"
        with self.assertRaisesRegex(ValueError, "snapshot"):
            validate_assessment_receipts(manifest, packets, receipts)

    def test_merge_rejects_partial_case_groups_stale_or_cross_group_responses(self) -> None:
        cases = [issue_case(number) for number in range(1, 12)]
        cases[0]["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 40_000
        manifest, packets = build_assessment_batches(
            cases, snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        response = worker_response(manifest, packets, manifest["workerGroups"][0])
        self.assertGreater(len(response["batches"]), 1)
        mutations = {
            "partial fragments": lambda value: value.update(batches=value["batches"][:1]),
            "stale assessment": lambda value: value.update(assessmentId="assessment:old"),
            "stale snapshot": lambda value: value.update(snapshotId="snapshot:old"),
            "foreign override": lambda value: value.update(issues=[{"issueNumber": 11}]),
            "duplicate override": lambda value: value.update(issues=[{"issueNumber": 1}, {"issueNumber": 1}]),
            "incomplete with claims": lambda value: value.update(status="incomplete"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(response)
                mutate(changed)
                with self.assertRaises(ValueError):
                    merge_worker_responses(manifest, packets, [changed])
        first_half = {**response, "batches": response["batches"][:1]}
        second_half = {**response, "batches": response["batches"][1:]}
        with self.assertRaisesRegex(ValueError, "Duplicate.*groupId"):
            merge_worker_responses(manifest, packets, [first_half, second_half])

    def test_merges_independent_complete_group_responses_without_losing_sparse_overrides(self) -> None:
        manifest, packets = build_assessment_batches(
            [issue_case(number) for number in range(1, 24)],
            snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        responses = [worker_response(manifest, packets, group) for group in manifest["workerGroups"]]
        responses[0]["issues"] = [{"issueNumber": 1, "summary": "Explicit reviewed override"}]
        combined, receipts, summary = merge_worker_responses(manifest, packets, responses[:1])
        self.assertEqual("incomplete", summary["status"])
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_assessment_receipts(manifest, packets, receipts)
        result = merge_worker_responses(manifest, packets, responses)
        combined, receipts, summary = result
        self.assertEqual(responses[0]["issues"], combined["issues"])
        self.assertEqual([], combined["pullRequests"])
        self.assertEqual("complete", summary["status"])
        self.assertEqual(23, validate_assessment_receipts(manifest, packets, receipts)["caseCount"])
        self.assertEqual(result, merge_worker_responses(manifest, packets, list(reversed(responses))))

    def test_case_exceeding_worker_budget_stays_incomplete_even_with_fragment_receipts(self) -> None:
        case = issue_case(1)
        case["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 270_000
        manifest, packets = build_assessment_batches(
            [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        self.assertEqual("incomplete", manifest["workerGroups"][0]["status"])
        self.assertEqual("worker-input-limit", manifest["workerGroups"][0]["reason"])
        receipts = {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "batches": [
                {
                    "batchId": packet["batchId"],
                    "cases": [
                        {"caseId": part["caseId"], "reviewedEvidenceIds": part["evidenceIds"]}
                        for part in packet["cases"]
                    ],
                }
                for packet in packets.values()
            ],
        }
        with self.assertRaisesRegex(ValueError, "worker.*incomplete"):
            validate_assessment_receipts(manifest, packets, receipts)

    def test_worker_groups_keep_all_case_parts_together_with_bounded_total_input(self) -> None:
        cases = [issue_case(number) for number in range(1, 24)]
        cases[0]["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 40_000
        manifest, packets = build_assessment_batches(
            cases, snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        self.assertGreater(len(manifest["workerGroups"]), 1)
        ownership = {}
        for group in manifest["workerGroups"]:
            self.assertEqual("ready", group["status"])
            self.assertLessEqual(len(group["caseIds"]), 10)
            self.assertLessEqual(group["byteCount"], manifest["maxWorkerInputBytes"])
            self.assertEqual(
                sum(len(stable_json(packets[name]).encode("utf-8")) for name in group["packetFiles"]),
                group["byteCount"],
            )
            for filename in group["packetFiles"]:
                packet = packets[filename]
                self.assertEqual(group["groupId"], packet["groupId"])
                for part in packet["cases"]:
                    parent = part.get("parentCaseId", part["caseId"])
                    ownership.setdefault(parent, set()).add(group["groupId"])
                    self.assertIn(parent, group["caseIds"])
        self.assertEqual({case["caseId"] for case in cases}, set(ownership))
        self.assertTrue(all(len(groups) == 1 for groups in ownership.values()))

    def test_stale_duplicate_or_inexact_evidence_receipts_cannot_complete_assessment(self) -> None:
        manifest, packets = build_assessment_batches(
            [issue_case(1)], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        valid = {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "batches": [{
                "batchId": "batch:1",
                "cases": [{"caseId": "issue:1", "reviewedEvidenceIds": ["issue:1"]}],
            }],
        }
        mutations = {
            "stale assessment": lambda value: value.update(assessmentId="assessment:old"),
            "missing batch": lambda value: value.update(batches=[]),
            "unknown batch": lambda value: value["batches"][0].update(batchId="batch:unknown"),
            "duplicate batch": lambda value: value["batches"].append(copy.deepcopy(value["batches"][0])),
            "duplicate case": lambda value: value["batches"][0]["cases"].append(copy.deepcopy(value["batches"][0]["cases"][0])),
            "unknown case": lambda value: value["batches"][0]["cases"][0].update(caseId="issue:999"),
            "missing evidence": lambda value: value["batches"][0]["cases"][0].update(reviewedEvidenceIds=[]),
            "unknown evidence": lambda value: value["batches"][0]["cases"][0].update(reviewedEvidenceIds=["issue:999"]),
            "duplicate evidence": lambda value: value["batches"][0]["cases"][0].update(reviewedEvidenceIds=["issue:1", "issue:1"]),
            "bad schema": lambda value: value.update(schemaVersion=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                receipts = copy.deepcopy(valid)
                mutate(receipts)
                with self.assertRaises(ValueError):
                    validate_assessment_receipts(manifest, packets, receipts)

        stale_packets = copy.deepcopy(packets)
        next(iter(stale_packets.values()))["cases"][0]["input"]["title"] = "Changed after review"
        with self.assertRaisesRegex(ValueError, "packet"):
            validate_assessment_receipts(manifest, stale_packets, valid)

    def test_single_oversized_case_splits_without_truncating_evidence(self) -> None:
        case = issue_case(1)
        case["input"]["evidenceBundle"][0]["payload"]["body"] = "x" * 16_000
        manifest, packets = build_assessment_batches(
            [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        parts = [part for packet in packets.values() for part in packet["cases"]]
        self.assertEqual(1, manifest["caseCount"])
        self.assertGreater(len(parts), 1)
        self.assertEqual(case, json.loads("".join(part["input"]["content"] for part in parts)))
        self.assertEqual(list(range(1, len(parts) + 1)), [part["input"]["partIndex"] for part in parts])
        self.assertTrue(all(part["input"]["partCount"] == len(parts) for part in parts))
        self.assertTrue(all(
            len(stable_json(packet).encode("utf-8")) <= manifest["maxPacketBytes"]
            for packet in packets.values()
        ))
        receipts = {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "batches": [
                {
                    "batchId": packet["batchId"],
                    "cases": [
                        {"caseId": part["caseId"], "reviewedEvidenceIds": part["evidenceIds"]}
                        for part in packet["cases"]
                    ],
                }
                for packet in packets.values()
            ],
        }
        completed = validate_assessment_receipts(manifest, packets, receipts)
        self.assertEqual([1], completed["completedIssueNumbers"])
        self.assertEqual(1, completed["caseCount"])
        receipts["batches"][-1]["cases"].pop()
        with self.assertRaisesRegex(ValueError, "missing cases"):
            validate_assessment_receipts(manifest, packets, receipts)

    def test_unsplittable_case_metadata_fails_visibly(self) -> None:
        case = issue_case(1)
        case["evidenceIds"] = [f"source:{number:040d}" for number in range(500)]
        with self.assertRaisesRegex(ValueError, "issue:1 metadata exceeds.*not truncated"):
            build_assessment_batches(
                [case], snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
            )

    def test_every_case_requires_a_receipt_even_when_no_judgment_is_overridden(self) -> None:
        manifest, packets = build_assessment_batches(
            [issue_case(1), issue_case(2)],
            snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )
        receipts = {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "batches": [{
                "batchId": manifest["batches"][0]["batchId"],
                "cases": [{"caseId": "issue:1", "reviewedEvidenceIds": ["issue:1"]}],
            }],
        }
        with self.assertRaisesRegex(ValueError, "missing.*issue:2"):
            validate_assessment_receipts(manifest, packets, receipts)

        receipts["batches"][0]["cases"].append({
            "caseId": "issue:2", "reviewedEvidenceIds": ["issue:2"],
        })
        result = validate_assessment_receipts(manifest, packets, receipts)
        self.assertEqual("complete", result["status"])
        self.assertEqual(2, result["caseCount"])
        self.assertEqual(2, result["issueCount"])
        self.assertEqual(0, result["pullRequestCount"])
        self.assertEqual(1, result["batchCount"])

    def test_materializes_every_case_and_its_evidence_in_bounded_packets(self) -> None:
        cases = [issue_case(number) for number in range(1, 24)]
        manifest, packets = build_assessment_batches(
            cases, snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )

        self.assertEqual([10, 10, 3], [len(packet["cases"]) for packet in packets.values()])
        self.assertEqual(cases, [case for packet in packets.values() for case in packet["cases"]])
        self.assertEqual(23, manifest["caseCount"])
        for batch in manifest["batches"]:
            packet = packets[batch["file"]]
            self.assertEqual(manifest["assessmentId"], packet["assessmentId"])
            self.assertEqual(len(stable_json(packet).encode("utf-8")), batch["byteCount"])
            self.assertLessEqual(batch["byteCount"], manifest["maxPacketBytes"])

    def test_packet_size_limit_counts_the_complete_serialized_utf8_document(self) -> None:
        cases = [issue_case(number) for number in range(1, 11)]
        for case in cases:
            case["input"]["evidenceBundle"][0]["payload"]["body"] = "🌱" * 600
        manifest, packets = build_assessment_batches(
            cases, snapshot_id="snapshot:owner/repo:now", source_fingerprints={},
        )

        self.assertGreater(len(packets), 1)
        self.assertEqual(cases, [case for packet in packets.values() for case in packet["cases"]])
        self.assertTrue(all(
            len(stable_json(packet).encode("utf-8")) <= manifest["maxPacketBytes"]
            for packet in packets.values()
        ))
