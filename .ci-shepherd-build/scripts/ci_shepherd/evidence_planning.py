from __future__ import annotations

from pathlib import Path
from datetime import timedelta
from typing import Any, Mapping

from .poc_state import record_review_wakeup
from .refresh import historical_run_retry_at
from .timeutils import format_utc_z, parse_aware_iso8601


MAX_EVIDENCE_REQUESTS = 25


def build_proposal_evidence_requests(
    snapshot: Mapping[str, Any],
    proposal_document: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    repository = snapshot.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Snapshot repository must be a nonempty string.")
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Snapshot evidence must be an object.")
    proposals = proposal_document.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("Proposal document proposals must be a list.")

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for proposal in sorted(
        proposals,
        key=lambda item: (
            int(item.get("issueNumber", 0)) if isinstance(item, Mapping) else 0,
            str(item.get("actionId", "")) if isinstance(item, Mapping) else "",
        ),
    ):
        if not isinstance(proposal, Mapping):
            raise ValueError("Proposal entries must be objects.")
        issue_number = proposal.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("Proposal issueNumber must be a positive integer.")
        eligibility = proposal.get("executionEligibility")
        if not isinstance(eligibility, Mapping):
            raise ValueError("Proposal executionEligibility must be an object.")
        blocking_reasons = eligibility.get("blockingReasons", [])
        if not isinstance(blocking_reasons, list) or any(not isinstance(reason, str) for reason in blocking_reasons):
            raise ValueError("Proposal blockingReasons must be a list of strings.")
        unavailable = eligibility.get("unavailableEvidenceIds")
        if not isinstance(unavailable, list):
            raise ValueError(
                "Proposal unavailableEvidenceIds must be a list."
            )
        cited = proposal.get("evidenceIds")
        if not isinstance(cited, list):
            raise ValueError("Proposal evidenceIds must be a list.")
        cited_ids = set(cited)
        for evidence_id in sorted(unavailable):
            if not isinstance(evidence_id, str) or evidence_id not in cited_ids:
                raise ValueError(
                    "Unavailable proposal evidence must be a cited evidence ID."
                )
            identity = (issue_number, evidence_id)
            if identity in seen:
                continue
            record = evidence.get(evidence_id)
            if not isinstance(record, Mapping):
                raise ValueError(
                    f"Proposal cites unknown unavailable evidence: {evidence_id}."
                )
            if (
                record.get("kind") != "workflow-run"
                or record.get("availability") not in {"partial", "not-enriched"}
            ):
                continue
            if {"missing-ci-label", "untrusted-reference-provenance"}.intersection(blocking_reasons):
                # Fetching a run cannot change label authority or prove the source
                # of an unrelated issue/PR link. Keep the blocker, not another round.
                continue
            retry_at = historical_run_retry_at(record)
            if retry_at is not None and (
                retry_at - timedelta(hours=24)
                <= parse_aware_iso8601(snapshot["collectedAt"], "snapshot collectedAt") < retry_at
            ):
                continue
            seen.add(identity)
            candidates.append(
                {
                    "type": "workflow-run",
                    "sourceIssueNumber": issue_number,
                    "evidenceId": evidence_id,
                    "decisionGate": "current-failing-run",
                    "reason": (
                        "Refresh the exact run cited by a projected status action "
                        "before deciding whether that action is executable."
                    ),
                }
            )

    selected = candidates[:MAX_EVIDENCE_REQUESTS]
    deferred = [
        str(request["evidenceId"])
        for request in candidates[MAX_EVIDENCE_REQUESTS:]
    ]
    return (
        {
            "schemaVersion": 1,
            "repository": repository,
            "round": 1,
            "requests": selected,
        },
        deferred,
    )


def record_unavailable_evidence_wakeups(state_directory: Path, snapshot: Mapping[str, Any]) -> None:
    """Reuse the existing typed schedule; observation time survives later reuse."""
    observed_at = parse_aware_iso8601(snapshot["collectedAt"], "snapshot collectedAt")
    open_issues = set(snapshot["openIssues"])
    for record in snapshot["evidence"].values():
        retry_at = historical_run_retry_at(record)
        if retry_at is None or retry_at <= observed_at:
            continue
        for reference in record["payload"]["referencedBy"]:
            issue_number = reference.get("sourceIssueNumber")
            if type(issue_number) is int and issue_number in open_issues:
                record_review_wakeup(
                    state_directory, snapshot["repository"], target_kind="issue",
                    target_number=issue_number, evaluate_at=format_utc_z(retry_at), reason="retry-backoff",
                )
