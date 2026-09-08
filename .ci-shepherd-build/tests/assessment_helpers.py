from __future__ import annotations

import json
from pathlib import Path

import cycle


def write_assessment_receipts(work_dir: Path) -> dict[str, object]:
    """Test-only attestation for fixtures whose complete packets are reviewed by the test."""
    manifest = json.loads((work_dir / "assessment-batches.json").read_text(encoding="utf-8"))
    batches = []
    for batch in manifest["batches"]:
        packet = json.loads((work_dir / batch["file"]).read_text(encoding="utf-8"))
        batches.append({
            "batchId": packet["batchId"],
            "cases": [
                {"caseId": case["caseId"], "reviewedEvidenceIds": case["evidenceIds"]}
                for case in packet["cases"]
            ],
        })
    receipts = {
        "schemaVersion": 1, "assessmentId": manifest["assessmentId"], "batches": batches,
    }
    path = work_dir / "assessment-receipts.json"
    path.write_text(json.dumps(receipts), encoding="utf-8")
    path.chmod(0o600)
    return receipts


def finish_reviewed_cycle(*, work_dir: Path, **options: object) -> dict[str, object]:
    write_assessment_receipts(work_dir)
    return cycle.finish_cycle(work_dir=work_dir, **options)
