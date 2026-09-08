from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .investigations import _fingerprint
from .lifecycle import snapshot_id_for
from .models import stable_json


MAX_ASSESSMENT_CASES = 10
MAX_ASSESSMENT_PACKET_BYTES = 16_000
MAX_ASSESSMENT_WORKER_BYTES = 256_000
ASSESSMENT_SOURCE_FILES = (
    "input.json", "assessment-input.json", "assessment-defaults.json",
    "agent-input.json", "review-selection.json", "pull-request-review.json",
)


def build_assessment_batches(
    cases: Sequence[Mapping[str, Any]],
    *,
    snapshot_id: str,
    source_fingerprints: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    assessment_id = f"assessment:{uuid4()}"
    packets: dict[str, dict[str, Any]] = {}
    batches = []
    groups = []
    selected_cases: list[Mapping[str, Any]] = []
    selected_parts: list[Mapping[str, Any]] = []
    selected_packets: dict[str, dict[str, Any]] = {}

    def emit_group() -> None:
        byte_count = sum(len(stable_json(packet).encode("utf-8")) for packet in selected_packets.values())
        group_id = f"group:{len(groups) + 1}"
        groups.append({
            "groupId": group_id,
            "caseIds": [case["caseId"] for case in selected_cases],
            "packetFiles": list(selected_packets),
            "batchIds": [packet["batchId"] for packet in selected_packets.values()],
            "responseFile": f"assessment-response-{len(groups) + 1:04d}.json",
            "byteCount": byte_count,
            "status": "ready" if byte_count <= MAX_ASSESSMENT_WORKER_BYTES else "incomplete",
            "reason": None if byte_count <= MAX_ASSESSMENT_WORKER_BYTES else "worker-input-limit",
            "instructions": "Use one fresh worker for this whole group. Read every packet and every case part before claiming complete.",
        })
        for filename, packet in selected_packets.items():
            packets[filename] = packet
            batches.append({
                "batchId": packet["batchId"], "file": filename,
                "caseIds": [case["caseId"] for case in packet["cases"]],
                "byteCount": len(stable_json(packet).encode("utf-8")),
                "packetFingerprint": _fingerprint(packet),
            })

    for case in cases:
        parts = _split_case(case, assessment_id, snapshot_id)
        candidate = _pack_group(
            [*selected_parts, *parts], assessment_id, snapshot_id,
            f"group:{len(groups) + 1}", len(batches),
        )
        if selected_cases and (
            len(selected_cases) == MAX_ASSESSMENT_CASES
            or sum(len(stable_json(packet).encode("utf-8")) for packet in candidate.values())
            > MAX_ASSESSMENT_WORKER_BYTES
        ):
            emit_group()
            selected_cases, selected_parts = [], []
            candidate = _pack_group(
                parts, assessment_id, snapshot_id, f"group:{len(groups) + 1}", len(batches),
            )
        selected_cases.append(case)
        selected_parts.extend(parts)
        selected_packets = candidate
        if sum(len(stable_json(packet).encode("utf-8")) for packet in candidate.values()) > MAX_ASSESSMENT_WORKER_BYTES:
            emit_group()
            selected_cases, selected_parts, selected_packets = [], [], {}
    if selected_cases:
        emit_group()
    return ({
        "schemaVersion": 1,
        "assessmentId": assessment_id,
        "snapshotId": snapshot_id,
        "sourceFingerprints": dict(source_fingerprints),
        "maxCasesPerBatch": MAX_ASSESSMENT_CASES,
        "maxPacketBytes": MAX_ASSESSMENT_PACKET_BYTES,
        "maxWorkerInputBytes": MAX_ASSESSMENT_WORKER_BYTES,
        "caseCount": len(cases),
        "batches": batches,
        "workerGroups": groups,
    }, packets)


def _pack_group(
    parts: Sequence[Mapping[str, Any]],
    assessment_id: str,
    snapshot_id: str,
    group_id: str,
    first_batch: int,
) -> dict[str, dict[str, Any]]:
    remaining = list(parts)
    packets = {}
    while remaining:
        selected = []
        batch_id = f"batch:{first_batch + len(packets) + 1}"
        filename = f"assessment-batch-{first_batch + len(packets) + 1:04d}.json"
        packet = {
            "schemaVersion": 1,
            "assessmentId": assessment_id,
            "snapshotId": snapshot_id,
            "batchId": batch_id,
            "groupId": group_id,
            "cases": selected,
        }
        while remaining and len(selected) < MAX_ASSESSMENT_CASES:
            selected.append(remaining[0])
            if len(stable_json(packet).encode("utf-8")) > MAX_ASSESSMENT_PACKET_BYTES:
                selected.pop()
                break
            remaining.pop(0)
        if not selected:
            raise ValueError(
                f"Assessment case {remaining[0]['caseId']} exceeds the "
                f"{MAX_ASSESSMENT_PACKET_BYTES}-byte packet limit; evidence was not truncated."
            )
        packets[filename] = packet
    return packets


def validate_assessment_receipts(
    manifest: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    receipts: Mapping[str, Any],
) -> dict[str, Any]:
    groups = manifest.get("workerGroups")
    if not isinstance(groups, list):
        raise ValueError("Legacy assessment lacks bounded worker groups; restart the cycle.")
    for group in groups:
        if group["status"] != "ready":
            raise ValueError(
                f"Assessment worker group {group['groupId']} remains incomplete: {group['reason']}."
            )
    if (
        not isinstance(receipts, Mapping)
        or set(receipts) != {"schemaVersion", "assessmentId", "batches"}
        or type(receipts["schemaVersion"]) is not int
        or receipts["schemaVersion"] != 1
    ):
        raise ValueError("Assessment receipts must use the version 1 coverage schema.")
    if receipts["assessmentId"] != manifest["assessmentId"]:
        raise ValueError("Assessment receipts are stale for the current assessment.")
    expected_batches = _unique_records(manifest["batches"], "batchId")
    received_batches = _unique_records(receipts["batches"], "batchId")
    if set(expected_batches) != set(received_batches):
        raise ValueError("Assessment receipts have missing or unknown batches.")
    reviewed_cases = []
    for batch_id, batch in expected_batches.items():
        packet = packets.get(batch["file"])
        if packet is not None and packet.get("snapshotId") != manifest["snapshotId"]:
            raise ValueError(f"Assessment packet {batch_id} has a mismatched snapshot identity.")
        if (
            packet is None
            or _fingerprint(packet) != batch["packetFingerprint"]
            or packet.get("assessmentId") != manifest["assessmentId"]
            or packet.get("batchId") != batch_id
            or len(stable_json(packet).encode("utf-8")) != batch["byteCount"]
        ):
            raise ValueError(f"Assessment packet {batch_id} is missing or changed.")
        received_batch = received_batches[batch_id]
        if set(received_batch) != {"batchId", "cases"}:
            raise ValueError("Batch receipts require only batchId and cases.")
        expected = _unique_records(packet["cases"], "caseId")
        received = _unique_records(received_batch["cases"], "caseId")
        if set(expected) != set(received):
            raise ValueError(
                f"Assessment receipts have missing cases {sorted(set(expected) - set(received))} "
                f"or unknown cases {sorted(set(received) - set(expected))}."
            )
        for case_id, case in expected.items():
            receipt = received[case_id]
            if set(receipt) != {"caseId", "reviewedEvidenceIds"}:
                raise ValueError("Case receipts require only caseId and reviewedEvidenceIds.")
            evidence_ids = receipt["reviewedEvidenceIds"]
            if (
                not isinstance(evidence_ids, list)
                or any(not isinstance(value, str) for value in evidence_ids)
                or len(set(evidence_ids)) != len(evidence_ids)
                or set(evidence_ids) != set(case["evidenceIds"])
            ):
                raise ValueError(f"Assessment receipt {case_id} must cover exactly its reviewed evidence.")
        reviewed_cases.extend(
            case.get("parentCaseId", case_id) for case_id, case in expected.items()
        )
    reviewed_cases = list(dict.fromkeys(reviewed_cases))
    return {
        "schemaVersion": 1,
        "assessmentId": manifest["assessmentId"],
        "snapshotId": manifest["snapshotId"],
        "status": "complete",
        "batchCount": len(expected_batches),
        "caseCount": len(reviewed_cases),
        "issueCount": sum(case.startswith("issue:") for case in reviewed_cases),
        "pullRequestCount": sum(case.startswith("pull-request:") for case in reviewed_cases),
        "caseIds": reviewed_cases,
        "completedIssueNumbers": [
            int(case.removeprefix("issue:")) for case in reviewed_cases if case.startswith("issue:")
        ],
        "completedPullRequestNumbers": [
            int(case.removeprefix("pull-request:")) for case in reviewed_cases if case.startswith("pull-request:")
        ],
        "limitation": "Receipts attest packet and case coverage, not the quality of reasoning.",
    }


def merge_worker_responses(
    manifest: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    responses: list[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    groups = _unique_records(manifest["workerGroups"], "groupId")
    submitted = _unique_records(responses, "groupId")
    if set(submitted) - set(groups):
        raise ValueError("Worker responses contain unknown assessment groups.")
    combined = {
        "schemaVersion": 1, "snapshotId": manifest["snapshotId"],
        "issues": [], "pullRequests": [],
    }
    receipts = {"schemaVersion": 1, "assessmentId": manifest["assessmentId"], "batches": []}
    completed = []
    for group_id, group in groups.items():
        response = submitted.get(group_id)
        if response is None:
            continue
        if (
            set(response) != {
                "schemaVersion", "snapshotId", "assessmentId", "groupId", "status",
                "issues", "pullRequests", "batches",
            }
            or type(response["schemaVersion"]) is not int
            or response["schemaVersion"] != 1
            or response["assessmentId"] != manifest["assessmentId"]
            or response["snapshotId"] != manifest["snapshotId"]
        ):
            raise ValueError(f"Worker response {group_id} has a stale identity or invalid schema.")
        if response["status"] == "incomplete":
            if any(response[key] != [] for key in ("issues", "pullRequests", "batches")):
                raise ValueError("Incomplete workers cannot submit overrides or coverage receipts.")
            continue
        if response["status"] != "complete":
            raise ValueError("Worker response status must be complete or incomplete.")
        group_manifest = {
            **manifest, "workerGroups": [group],
            "batches": [batch for batch in manifest["batches"] if batch["batchId"] in group["batchIds"]],
        }
        validate_assessment_receipts(
            group_manifest, packets, {**receipts, "batches": response["batches"]},
        )
        for field, number_field, prefix in (
            ("issues", "issueNumber", "issue:"),
            ("pullRequests", "pullRequestNumber", "pull-request:"),
        ):
            overrides = response[field]
            if not isinstance(overrides, list):
                raise ValueError(f"Worker {field} overrides must be an array.")
            seen = set()
            for override in overrides:
                number = override.get(number_field) if isinstance(override, Mapping) else None
                if (
                    type(number) is not int or f"{prefix}{number}" not in group["caseIds"]
                    or number in seen
                ):
                    raise ValueError(f"Worker {group_id} contains duplicate or out-of-group overrides.")
                seen.add(number)
                combined[field].append(override)
        submitted_batches = _unique_records(response["batches"], "batchId")
        receipts["batches"].extend(submitted_batches[batch_id] for batch_id in group["batchIds"])
        completed.append(group_id)
    combined["issues"].sort(key=lambda item: item["issueNumber"])
    combined["pullRequests"].sort(key=lambda item: item["pullRequestNumber"])
    return combined, receipts, {
        "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
        "status": "complete" if len(completed) == len(groups) else "incomplete",
        "completedGroupIds": completed,
        "missingGroupIds": [group_id for group_id in groups if group_id not in completed],
        "completedCaseCount": sum(len(groups[group_id]["caseIds"]) for group_id in completed),
    }


def _split_case(
    case: Mapping[str, Any], assessment_id: str, snapshot_id: str,
) -> list[Mapping[str, Any]]:
    def fits(part: Mapping[str, Any]) -> bool:
        packet = {
            "schemaVersion": 1, "assessmentId": assessment_id,
            "snapshotId": snapshot_id, "batchId": "batch:pending", "cases": [part],
        }
        # Leave room for final sequence numbers; count the serialized outer
        # document too, since embedded JSON fragments need additional escaping.
        return len(stable_json(packet).encode("utf-8")) <= MAX_ASSESSMENT_PACKET_BYTES - 128

    if fits(case):
        return [case]
    content = stable_json(case)
    parts = []
    offset = 0
    while offset < len(content):
        length = min(4096, len(content) - offset)
        part = {
            "caseId": f"{case['caseId']}/part/{len(parts) + 1}",
            "parentCaseId": case["caseId"],
            "evidenceIds": case["evidenceIds"],
            "input": {
                "format": "json-fragment",
                "instruction": "Read every part in order; join content to reconstruct the complete case JSON.",
                "partIndex": len(parts) + 1,
                "partCount": len(content),
                "content": content[offset:offset + length],
            },
        }
        while not fits(part) and length > 1:
            length //= 2
            part["input"]["content"] = content[offset:offset + length]
        if not fits(part):
            raise ValueError(
                f"Assessment case {case['caseId']} metadata exceeds the "
                f"{MAX_ASSESSMENT_PACKET_BYTES}-byte packet limit; evidence was not truncated."
            )
        parts.append(part)
        offset += length
    for part in parts:
        part["input"]["partCount"] = len(parts)
    return parts


def _unique_records(values: object, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(values, list):
        raise ValueError(f"Assessment {key} records must be an array.")
    indexed = {}
    for value in values:
        if not isinstance(value, Mapping) or not isinstance(value.get(key), str):
            raise ValueError(f"Assessment {key} records require a string identity.")
        identity = value[key]
        if identity in indexed:
            raise ValueError(f"Duplicate assessment {key}: {identity}.")
        indexed[identity] = value
    return indexed


def materialize_assessment(work_dir: Path) -> dict[str, Any]:
    previous_paths = []
    if (work_dir / "assessment-batches.json").is_file():
        previous_manifest = _read_json(work_dir / "assessment-batches.json")
        for index, batch in enumerate(previous_manifest["batches"], start=1):
            filename = f"assessment-batch-{index:04d}.json"
            if batch["file"] != filename:
                raise ValueError("Assessment packet path is invalid.")
            previous_paths.append(work_dir / filename)
        for index, group in enumerate(previous_manifest.get("workerGroups", []), start=1):
            filename = f"assessment-response-{index:04d}.json"
            if group["responseFile"] != filename:
                raise ValueError("Assessment response path is invalid.")
            previous_paths.append(work_dir / filename)
    documents = {name: _read_json(work_dir / name) for name in ASSESSMENT_SOURCE_FILES}
    prepared = documents["assessment-input.json"]
    issues = {issue["issueNumber"]: issue for issue in prepared["issues"]}
    defaults = {
        issue["issueNumber"]: issue for issue in documents["assessment-defaults.json"]["issues"]
    }
    cases = []
    for selected in documents["review-selection.json"]["selected"]:
        number = selected["issueNumber"]
        issue = issues[number]
        cases.append({
            "caseId": f"issue:{number}",
            "evidenceIds": [record["id"] for record in issue["evidenceBundle"]],
            "input": issue,
            "defaultJudgment": defaults[number],
            "selection": selected,
        })
    snapshot_evidence = documents["input.json"]["evidence"]
    for task in documents["pull-request-review.json"]["tasks"]:
        cases.append({
            "caseId": f"pull-request:{task['target']['number']}",
            "evidenceIds": task["evidenceIds"],
            "input": {
                "task": task,
                "evidence": {evidence_id: snapshot_evidence[evidence_id] for evidence_id in task["evidenceIds"]},
            },
            "defaultJudgment": task["defaultJudgment"],
        })
    _unique_records(cases, "caseId")
    manifest, packets = build_assessment_batches(
        cases, snapshot_id=prepared["snapshotId"],
        source_fingerprints=_source_fingerprints(work_dir),
    )
    for name, packet in packets.items():
        _write_json(work_dir / name, packet)
    for path in previous_paths:
        if path.name not in packets:
            path.unlink(missing_ok=True)
    _write_json(work_dir / "assessment-batches.json", manifest)
    for group in manifest["workerGroups"]:
        _write_json(work_dir / group["responseFile"], {
            "schemaVersion": 1, "assessmentId": manifest["assessmentId"],
            "snapshotId": manifest["snapshotId"], "groupId": group["groupId"],
            "status": "incomplete", "issues": [], "pullRequests": [], "batches": [],
        })
    _write_json(work_dir / "assessment-receipts.json", {
        "schemaVersion": 1, "assessmentId": manifest["assessmentId"], "batches": [],
    })
    (work_dir / "assessment-completion.json").unlink(missing_ok=True)
    return {
        "assessmentId": manifest["assessmentId"],
        "manifestFingerprint": _fingerprint(manifest),
        "manifest": "assessment-batches.json",
        "receipts": "assessment-receipts.json",
        "batchCount": len(manifest["batches"]),
        "caseCount": manifest["caseCount"],
        "workerGroupCount": len(manifest["workerGroups"]),
        "incompleteWorkerGroupIds": [
            group["groupId"] for group in manifest["workerGroups"] if group["status"] != "ready"
        ],
        "status": "awaiting-receipts" if cases else "not-required",
    }


def load_assessment_packets(
    work_dir: Path,
    assessment: object,
    *,
    pre_expansion: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(assessment, Mapping):
        raise ValueError("Legacy cycle lacks assessment receipts; replay through cycle start before reviewing.")
    suffix = ".pre-expansion" if pre_expansion else ""
    manifest = _read_json(work_dir / f"assessment-batches{suffix}.json")
    if (
        assessment.get("assessmentId") != manifest.get("assessmentId")
        or assessment.get("manifestFingerprint") != _fingerprint(manifest)
        or (
            not pre_expansion
            and manifest.get("sourceFingerprints") != _source_fingerprints(work_dir)
        )
    ):
        raise ValueError("Assessment inputs or batch manifest changed; current receipts are stale.")
    source_snapshot = _read_json(work_dir / f"input{suffix}.json")
    if (
        manifest.get("snapshotId") != snapshot_id_for(source_snapshot)
        or (
            not pre_expansion
            and _read_json(work_dir / "assessment-input.json").get("snapshotId") != manifest.get("snapshotId")
        )
    ):
        raise ValueError("Assessment snapshot identity does not match its frozen inputs.")
    packets = {}
    for index, batch in enumerate(manifest["batches"], start=1):
        filename = f"assessment-batch-{index:04d}.json"
        if batch["file"] != filename:
            raise ValueError("Assessment packet path is invalid.")
        packets[filename] = _read_json(work_dir / f"assessment-batch-{index:04d}{suffix}.json")
    return manifest, packets


def verify_assessment_completion(
    work_dir: Path,
    assessment: object,
    *,
    receipts_path: Path | None = None,
    pre_expansion: bool = False,
) -> dict[str, Any]:
    manifest, packets = load_assessment_packets(work_dir, assessment, pre_expansion=pre_expansion)
    suffix = ".pre-expansion" if pre_expansion else ""
    receipts = _read_json(receipts_path or work_dir / f"assessment-receipts{suffix}.json")
    completion = validate_assessment_receipts(manifest, packets, receipts)
    if pre_expansion and (
        receipts_path is not None
        or _read_json(work_dir / "assessment-completion.pre-expansion.json") != completion
        or _fingerprint((work_dir / "input.pre-expansion.json").read_bytes().decode("utf-8"))
        != manifest["sourceFingerprints"]["input.json"]
    ):
        raise ValueError("Preserved pre-expansion assessment completion is stale.")
    # A receipt is an explicit coverage attestation, not proof of model reasoning.
    # Keep it separate from sparse judgment overrides and authorization grants.
    if receipts_path is not None and not pre_expansion:
        _write_json(work_dir / "assessment-receipts.json", receipts)
    return completion


def assessment_artifacts(work_dir: Path) -> list[Path]:
    manifest = _read_json(work_dir / "assessment-batches.json")
    return [
        work_dir / "assessment-batches.json",
        work_dir / "assessment-receipts.json",
        work_dir / "assessment-completion.json",
        *[work_dir / batch["file"] for batch in manifest["batches"]],
        *[work_dir / group["responseFile"] for group in manifest.get("workerGroups", [])],
    ]


def _source_fingerprints(work_dir: Path) -> dict[str, str]:
    return {
        name: _fingerprint((work_dir / name).read_bytes().decode("utf-8"))
        for name in ASSESSMENT_SOURCE_FILES
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read assessment artifact {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Assessment artifact {path.name} must contain an object.")
    return value


def _write_json(path: Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(stable_json(document), encoding="utf-8", newline="\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
