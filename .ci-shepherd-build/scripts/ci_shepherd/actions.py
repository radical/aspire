from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from ci_shepherd.comment_body import comment_bodies_materially_equal
from ci_shepherd.eligibility import executable_ci_labels
from ci_shepherd.handoff_reminders import reminder_action_identity
from ci_shepherd.investigations import derive_machine_actionability
from ci_shepherd.lifecycle import delegation_context, prepare_assessment
from ci_shepherd.models import stable_json
from ci_shepherd.poc import validate_poc_judgments
from ci_shepherd.timeutils import parse_aware_iso8601


_QUARANTINE_SOURCE_REVISION_RE = re.compile(
    r"(?m)^(\*\*Current source evidence\*\* \(revision `)[0-9a-f]{40}(`\):)$"
)


def _status_markers(issue_number: int) -> str:
    return (
        "<!-- ci-shepherd:role=status -->\n"
        f"<!-- ci-shepherd:idempotency-key=issue:{issue_number}:status -->"
    )


def _quarantine_comment_bodies_materially_equal(left: str, right: str) -> bool:
    if "ci-shepherd:finding-digest" in left:
        return False
    return comment_bodies_materially_equal(
        _QUARANTINE_SOURCE_REVISION_RE.sub(r"\1<revision>\2", left),
        _QUARANTINE_SOURCE_REVISION_RE.sub(r"\1<revision>\2", right),
    )


def _evidence_lines(
    snapshot: dict[str, object],
    evidence_ids: list[str],
) -> list[str]:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")

    lines: list[str] = []
    for evidence_id in evidence_ids:
        record = evidence.get(evidence_id)
        url = record.get("url") if isinstance(record, dict) else None
        lines.append(
            f"- [{evidence_id}]({url})"
            if isinstance(url, str) and url
            else f"- `{evidence_id}`"
        )
    return lines


def _render_watch_body(
    issue_number: int,
    recommendation: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    missing = recommendation.get("missingEvidence", [])
    if not isinstance(missing, list):
        raise TypeError("Watch missingEvidence must be a list.")

    reassess_when = str(recommendation.get("reassessWhen", "")).strip()
    if not reassess_when:
        raise ValueError(
            f"Watch recommendation for issue {issue_number} must name reassessWhen."
        )

    evidence_ids = recommendation.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(evidence_id, str) for evidence_id in evidence_ids
    ):
        raise TypeError("Watch evidenceIds must contain strings.")

    missing_lines = (
        [f"- {value}" for value in missing]
        if missing
        else ["- No additional evidence is currently fetchable."]
    )
    return "\n".join(
        [
            "[automated] The CI shepherd is watching this failure.",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            "**Evidence still needed:**",
            *missing_lines,
            "",
            f"**Reassess when:** {reassess_when}",
            "",
            "No quarantine, retry, closure, or investigation has been started.",
            "",
            _status_markers(issue_number),
        ]
    )


def _render_retired_status_body(
    issue_number: int,
    recommendation: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    evidence_ids = recommendation.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(evidence_id, str) for evidence_id in evidence_ids
    ):
        raise TypeError("Investigation evidenceIds must contain strings.")
    return "\n".join(
        [
            (
                "[automated] The CI shepherd is no longer watching or requesting "
                "input through this status comment."
            ),
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            (
                "**Status:** This case moved to report-only investigation. "
                "No GitHub action has been started."
            ),
            "",
            _status_markers(issue_number),
        ]
    )


def _render_ping_human_body(
    issue_number: int,
    recommendation: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    escalation = recommendation.get("humanEscalation")
    if not isinstance(escalation, dict):
        raise TypeError(
            "Validated ping-human recommendation must include humanEscalation."
        )
    steps = escalation.get("suggestedNextSteps")
    if not isinstance(steps, list) or not all(
        isinstance(step, str) and step.strip() for step in steps
    ):
        raise TypeError("Validated suggestedNextSteps must contain strings.")
    evidence_ids = recommendation.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(evidence_id, str) for evidence_id in evidence_ids
    ):
        raise TypeError("Ping-human evidenceIds must contain strings.")
    return "\n".join(
        [
            f"[automated] {escalation['context']}",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            f"**Why human input is needed:** {escalation['whyHuman']}",
            "",
            f"**Decision needed:** {escalation['question']}",
            "",
            "**Suggested next steps:**",
            *(f"- {step}" for step in steps),
            "",
            f"**Routing hint:** `{escalation['routingHint']}`",
            "",
            _status_markers(issue_number),
        ]
    )


def _render_close_body(
    issue_number: int,
    recommendation: dict[str, Any],
    prepared_issue: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    evidence_ids = recommendation.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(evidence_id, str) for evidence_id in evidence_ids
    ):
        raise TypeError("Review-close evidenceIds must contain strings.")

    missing = recommendation.get("missingEvidence")
    if not isinstance(missing, list):
        raise TypeError("Review-close missingEvidence must be a list.")
    if missing:
        raise ValueError(
            f"Issue {issue_number} review-close cannot have missing evidence."
        )

    resolution = prepared_issue["resolutionEvidence"]
    if not isinstance(resolution, dict):
        raise TypeError("Review-close resolutionEvidence must be an object.")
    run_evidence_id = resolution.get("runEvidenceId")
    pull_request_evidence_id = resolution.get("pullRequestEvidenceId")
    merge_commit_sha = resolution.get("mergeCommitSha")
    if not all(
        isinstance(value, str) and value
        for value in (
            run_evidence_id,
            pull_request_evidence_id,
            merge_commit_sha,
        )
    ):
        raise ValueError(
            f"Issue {issue_number} review-close resolution evidence is incomplete."
        )

    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    issue_record = evidence.get(f"issue:{issue_number}")
    run_record = evidence.get(run_evidence_id)
    pull_request_record = evidence.get(pull_request_evidence_id)
    if not all(
        isinstance(record, dict)
        for record in (issue_record, run_record, pull_request_record)
    ):
        raise ValueError(
            f"Issue {issue_number} review-close evidence records are unavailable."
        )

    issue_payload = issue_record.get("payload")
    run_payload = run_record.get("payload")
    pull_request_payload = pull_request_record.get("payload")
    if not all(
        isinstance(payload, dict)
        for payload in (issue_payload, run_payload, pull_request_payload)
    ):
        raise TypeError("Review-close evidence payloads must be objects.")
    if (
        run_record.get("availability") != "available"
        or run_payload.get("status") != "completed"
        or run_payload.get("conclusion") != "success"
        or run_payload.get("headSha") != merge_commit_sha
        or pull_request_record.get("availability") != "available"
        or pull_request_payload.get("mergeCommitSha") != merge_commit_sha
    ):
        raise ValueError(
            f"Issue {issue_number} review-close recovery evidence is inconsistent."
        )

    facts = issue_payload.get("facts", [])
    if not isinstance(facts, list):
        raise TypeError("Review-close issue facts must be a list.")
    fact_values = {
        str(fact["field"]): str(fact["normalized"])
        for fact in facts
        if isinstance(fact, dict)
        and isinstance(fact.get("field"), str)
        and isinstance(fact.get("normalized"), str)
    }
    failure_type = fact_values.get("failureType", "CI")
    failure_description = {
        "main-repository-breakage": "main-branch build",
    }.get(failure_type, failure_type.replace("-", " "))
    error_code = fact_values.get("errorCode")
    failure_line = f"- The issue records a {failure_description} failure"
    if error_code:
        failure_line += f" with compiler error `{error_code}`"
    failure_line += "."

    pull_request_number = pull_request_payload.get("number")
    run_id = run_payload.get("runId")
    branch = run_payload.get("branch")
    pull_request_url = pull_request_record.get("url")
    run_url = run_record.get("url")
    if not (
        isinstance(pull_request_number, int)
        and isinstance(run_id, int)
        and isinstance(branch, str)
        and branch
        and isinstance(pull_request_url, str)
        and pull_request_url
        and isinstance(run_url, str)
        and run_url
    ):
        raise ValueError(
            f"Issue {issue_number} review-close recovery details are incomplete."
        )

    return "\n".join(
        [
            "[automated] The CI shepherd found recovery evidence for this failure.",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Why this can be closed:**",
            failure_line,
            (
                f"- PR [#{pull_request_number}]({pull_request_url}) merged commit "
                f"`{merge_commit_sha}`."
            ),
            (
                f"- CI run [{run_id}]({run_url}) completed successfully on "
                f"`{branch}` for that exact merge commit."
            ),
            (
                "- That successful post-fix run satisfies the recovery gate, so "
                "the recorded failure is resolved rather than awaiting investigation."
            ),
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            (
                "**Resolution:** The recovery evidence supports closing this "
                "issue as completed."
            ),
            "",
            _status_markers(issue_number),
        ]
    )


def _render_recovered_run_close_body(
    issue_number: int,
    recommendation: dict[str, Any],
    recovered_run_evidence_id: str,
    snapshot: dict[str, object],
) -> str:
    evidence_ids = recommendation.get("evidenceIds")
    if (
        not isinstance(evidence_ids, list)
        or recovered_run_evidence_id not in evidence_ids
    ):
        raise ValueError(
            f"Issue {issue_number} review-close must cite its recovered run."
        )
    missing = recommendation.get("missingEvidence")
    run_recovery_satisfied = {
        "occurrence-run-timestamp-for-fix-day",
        "verified-fix",
        "verified-fix-or-current-recurrence-check",
    }
    if (
        not isinstance(missing, list)
        or any(
            not isinstance(item, str) or item not in run_recovery_satisfied
            for item in missing
        )
    ):
        raise ValueError(
            f"Issue {issue_number} review-close has unsupported missing evidence."
        )
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    run_record = evidence.get(recovered_run_evidence_id)
    run_payload = run_record.get("payload") if isinstance(run_record, dict) else None
    if (
        not isinstance(run_record, dict)
        or run_record.get("availability") != "available"
        or not isinstance(run_payload, dict)
        or run_payload.get("status") != "completed"
        or run_payload.get("conclusion") != "success"
        or run_payload.get("branch") != "main"
    ):
        raise ValueError(
            f"Issue {issue_number} recovered run evidence is inconsistent."
        )
    run_id = run_payload.get("runId")
    run_url = run_record.get("url")
    if (
        not isinstance(run_id, int)
        or isinstance(run_id, bool)
        or not isinstance(run_url, str)
        or not run_url
    ):
        raise ValueError(
            f"Issue {issue_number} recovered run details are incomplete."
        )
    return "\n".join(
        [
            "[automated] The CI shepherd found recovery evidence for this failure.",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            "**Recovery proof:**",
            (
                f"- CI run [{run_id}]({run_url}) completed successfully on `main` "
                "after the last recorded failure."
            ),
            (
                "- This directly issue-scoped later run satisfies the recovery "
                "gate without attributing the fix to a specific pull request."
            ),
            "",
            "**Resolution:** The recovery evidence supports closing this issue as completed.",
            "",
            _status_markers(issue_number),
        ]
    )


def _render_exact_recovery_body(
    issue_number: int, recommendation: Mapping[str, Any],
    recovery: Mapping[str, Any], snapshot: dict[str, object],
) -> str:
    proof_lines = []
    for subject in recovery["subjects"]:
        covered = subject["coverage"]
        scope = covered["verifiedScope"]
        label = scope.get("pullRequest", scope.get("ref", scope["kind"]))
        test = f"; exact test `{covered['testName']}` passed" if covered["testName"] else ""
        proof_lines.append(
            f"- `{covered['workflow']}` / `{covered['jobName']}` on `{covered['os']}` "
            f"in `{label}` completed successfully after the recorded failure{test}."
        )
    return "\n".join([
        "[automated] The CI shepherd found exact positive execution coverage for this failure.",
        "", f"**Current assessment:** {recommendation['summary']}",
        "", "**Recovery proof:**", *dict.fromkeys(proof_lines),
        "", "**Evidence reviewed:**", *_evidence_lines(snapshot, recommendation["evidenceIds"]),
        "", "**Resolution:** The matched execution evidence supports closing this issue as completed.",
        "", _status_markers(issue_number),
    ])


def _render_duplicate_close_body(
    issue_number: int,
    recommendation: dict[str, Any],
    action_cluster: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    evidence_ids = recommendation.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(evidence_id, str) for evidence_id in evidence_ids
    ):
        raise TypeError("Duplicate review-close evidenceIds must contain strings.")
    missing = recommendation.get("missingEvidence")
    if not isinstance(missing, list):
        raise TypeError("Duplicate review-close missingEvidence must be a list.")
    if missing:
        raise ValueError(
            f"Issue {issue_number} duplicate review-close cannot have missing evidence."
        )

    canonical_issue_number = action_cluster.get("canonicalIssueNumber")
    members = action_cluster.get("memberIssueNumbers")
    relationship = action_cluster.get("relationship")
    if (
        action_cluster.get("role") != "superseded"
        or not isinstance(canonical_issue_number, int)
        or isinstance(canonical_issue_number, bool)
        or canonical_issue_number <= 0
        or canonical_issue_number == issue_number
        or not isinstance(members, list)
        or issue_number not in members
        or canonical_issue_number not in members
        or relationship
        not in {"same-error-code", "same-test", "same-workflow-failure"}
    ):
        raise ValueError(
            f"Issue {issue_number} duplicate review-close cluster is invalid."
        )

    repository = snapshot.get("repository")
    if not isinstance(repository, str) or not repository:
        raise TypeError("Validated snapshot repository must be a string.")
    canonical_url = (
        f"https://github.com/{repository}/issues/{canonical_issue_number}"
    )
    relationship_description = {
        "same-error-code": "the same normalized error code",
        "same-test": "the same test failure",
        "same-workflow-failure": "the same workflow failure",
    }[relationship]
    return "\n".join(
        [
            "[automated] The CI shepherd found that this is a duplicate issue record.",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Why this can be closed:**",
            (
                f"- This issue and the canonical issue track "
                f"{relationship_description}."
            ),
            (
                f"- The shared failure remains tracked by canonical issue "
                f"[#{canonical_issue_number}]({canonical_url})."
            ),
            (
                "- Closing this duplicate does not claim that the shared failure "
                "has recovered."
            ),
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            (
                "**Resolution:** The duplicate relationship supports closing this "
                "issue as a duplicate."
            ),
            "",
            _status_markers(issue_number),
        ]
    )


DEFAULT_PROPOSAL_TTL_HOURS = 24
MAX_PROPOSALS_PER_ISSUE = 2
TRUSTED_ACTION_REFERENCE_METHODS = frozenset(
    {
        "full-issue-url",
        "full-pull-url",
        "triggering-pull-request",
        "occurrence-pull-request",
    }
)
_CONSECUTIVE_FAILURE_CLAIM = re.compile(
    r"\bfailed\s+[1-9]\d*\s+consecutive\s+times\b",
    re.IGNORECASE,
)


def _execution_eligibility(
    snapshot: dict[str, object],
    *,
    issue_number: int,
    evidence_ids: list[object],
    evidence_basis: str,
) -> dict[str, object]:
    if evidence_basis not in {
        "ci-occurrence",
        "issue-state",
        "source-reconciliation",
        "delegation-state",
    }:
        raise ValueError(f"Unsupported action evidence basis: {evidence_basis}.")
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    issue_record = evidence.get(f"issue:{issue_number}")
    issue_payload = (
        issue_record.get("payload")
        if isinstance(issue_record, dict)
        else None
    )
    if not isinstance(issue_payload, dict):
        raise ValueError(f"Issue {issue_number} has no factual issue evidence.")

    raw_labels = issue_payload.get("labels")

    occurrences = issue_payload.get("occurrences")
    occurrence_count = len(occurrences) if isinstance(occurrences, list) else 0
    collection_errors = snapshot.get("collectionErrors")
    if not isinstance(collection_errors, list):
        raise TypeError("Validated snapshot collectionErrors must be a list.")
    relevant_collection_errors = [
        error
        for error in collection_errors
        if not isinstance(error, dict)
        or not isinstance(error.get("scope"), dict)
        or error["scope"].get("kind") != "issue"
        or issue_number in error["scope"].get("issueNumbers", [])
    ]

    unavailable_evidence_ids = sorted(
        {
            evidence_id
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str)
            and (
                not isinstance(evidence.get(evidence_id), dict)
                or evidence[evidence_id].get("availability") != "available"
            )
        }
    )
    untrusted_reference_evidence_ids: list[str] = []
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str) or evidence_id == f"issue:{issue_number}":
            continue
        record = evidence.get(evidence_id)
        if (
            not isinstance(record, dict)
            or record.get("kind") not in {"issue-event", "pull-request"}
        ):
            continue
        payload = record.get("payload")
        referenced_by = payload.get("referencedBy") if isinstance(payload, dict) else None
        trusted = isinstance(referenced_by, list) and any(
            isinstance(reference, dict)
            and reference.get("sourceIssueNumber") == issue_number
            and (
                reference.get("extractionMethod")
                in TRUSTED_ACTION_REFERENCE_METHODS
                or reference.get("decisionValue") == "explicit-resolution"
            )
            for reference in referenced_by
        )
        if not trusted:
            untrusted_reference_evidence_ids.append(evidence_id)

    blocking_reasons: list[str] = []
    ci_labels = sorted(executable_ci_labels(raw_labels))
    if evidence_basis in {"ci-occurrence", "issue-state", "delegation-state"}:
        if not ci_labels:
            blocking_reasons.append("missing-ci-label")
    if evidence_basis == "ci-occurrence":
        if occurrence_count <= 0:
            blocking_reasons.append("no-parsed-occurrences")
    if relevant_collection_errors:
        blocking_reasons.append("incomplete-collection")
    if unavailable_evidence_ids:
        blocking_reasons.append("unavailable-evidence")
    if untrusted_reference_evidence_ids:
        blocking_reasons.append("untrusted-reference-provenance")

    return {
        "eligible": not blocking_reasons,
        "evidenceBasis": evidence_basis,
        "ciLabels": ci_labels,
        "occurrenceCount": occurrence_count,
        "collectionComplete": not relevant_collection_errors,
        "unavailableEvidenceIds": unavailable_evidence_ids,
        "untrustedReferenceEvidenceIds": sorted(
            untrusted_reference_evidence_ids
        ),
        "blockingReasons": blocking_reasons,
    }


def _finalize_execution_metadata(
    result: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    proposals = result.get("proposals")
    if not isinstance(proposals, list):
        raise TypeError("Validated proposals must be a list.")

    counts_by_issue: dict[int, int] = {}
    document_violations: list[dict[str, object]] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise TypeError("Validated proposal must be an object.")
        issue_number = proposal.get("issueNumber")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool):
            raise ValueError("Issue action proposal must have an issueNumber.")
        counts_by_issue[issue_number] = counts_by_issue.get(issue_number, 0) + 1
        evidence_ids = proposal.get("evidenceIds")
        if not isinstance(evidence_ids, list):
            raise TypeError("Action proposal evidenceIds must be a list.")
        evidence_basis = proposal.get("evidenceBasis")
        if not isinstance(evidence_basis, str):
            raise TypeError("Action proposal evidenceBasis must be explicit.")
        eligibility = _execution_eligibility(
            snapshot,
            issue_number=issue_number,
            evidence_ids=evidence_ids,
            evidence_basis=evidence_basis,
        )
        body = proposal.get("body")
        if (
            eligibility["occurrenceCount"] == 0
            and isinstance(body, str)
            and _CONSECUTIVE_FAILURE_CLAIM.search(body) is not None
        ):
            blocking_reasons = eligibility["blockingReasons"]
            assert isinstance(blocking_reasons, list)
            blocking_reasons.append("body-occurrence-contradiction")
            eligibility["eligible"] = False
        proposal["executionEligibility"] = eligibility
        evidence = snapshot.get("evidence")
        issue_record = (
            evidence.get(f"issue:{issue_number}")
            if isinstance(evidence, dict)
            else None
        )
        issue_payload = (
            issue_record.get("payload")
            if isinstance(issue_record, dict)
            else None
        )
        issue_updated_at = (
            issue_payload.get("updatedAt")
            if isinstance(issue_payload, dict)
            else None
        )
        if not isinstance(issue_updated_at, str) or not issue_updated_at:
            raise ValueError(
                f"Issue {issue_number} evidence must include updatedAt."
            )
        fingerprint = proposal.get("sourceEvidenceFingerprint")
        if fingerprint is None:
            fingerprint = {}
        if not isinstance(fingerprint, dict):
            raise TypeError("Action sourceEvidenceFingerprint must be an object.")
        fingerprint["issueUpdatedAt"] = issue_updated_at
        proposal["sourceEvidenceFingerprint"] = fingerprint
        if proposal.get("operation") == "edit-comment":
            comment_id = proposal.get("commentId")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool):
                raise ValueError(
                    f"Edit action {proposal['actionId']} must have a commentId."
                )
            comment_body = _source_comment_body(
                snapshot,
                issue_number=issue_number,
                comment_id=comment_id,
            )
            if comment_body is None:
                eligibility["blockingReasons"].append(
                    "source-comment-unavailable"
                )
                eligibility["eligible"] = False
            else:
                proposal["sourceCommentFingerprint"] = {
                    "bodySha256": hashlib.sha256(
                        comment_body.encode("utf-8")
                    ).hexdigest(),
                }
        if eligibility["eligible"] is not True:
            document_violations.append(
                {
                    "actionId": proposal["actionId"],
                    "blockingReasons": list(eligibility["blockingReasons"]),
                }
            )

    excessive = sorted(
        issue_number
        for issue_number, count in counts_by_issue.items()
        if count > MAX_PROPOSALS_PER_ISSUE
    )
    if excessive:
        raise ValueError(
            "Proposal count exceeds maxProposalsPerIssue for issues: "
            f"{excessive}"
        )

    generated_at = snapshot.get("collectedAt")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("Snapshot collectedAt must be a non-empty string.")
    result.update(
        {
            "schemaVersion": 2,
            "generatedAtUtc": generated_at,
            "proposalTtlHours": DEFAULT_PROPOSAL_TTL_HOURS,
            "maxProposalsPerIssue": MAX_PROPOSALS_PER_ISSUE,
            "executionEligibility": {
                "status": (
                    "eligible"
                    if not document_violations
                    else "blocked"
                    if len(document_violations) == len(proposals)
                    else "partially-eligible"
                ),
                "violations": document_violations,
            },
        }
    )


def _action_clusters(
    agent_input: object | None,
    *,
    snapshot_id: object,
) -> dict[int, dict[str, Any]]:
    if agent_input is None:
        return {}
    if not isinstance(agent_input, dict):
        raise TypeError("Compact agent input must be an object.")
    if agent_input.get("schemaVersion") != 1:
        raise ValueError("Compact agent input schemaVersion must be 1.")
    if agent_input.get("snapshotId") != snapshot_id:
        raise ValueError("Compact agent input snapshotId does not match prepared input.")
    issues = agent_input.get("issues")
    if not isinstance(issues, list):
        raise TypeError("Compact agent input issues must be a list.")

    clusters: dict[int, dict[str, Any]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise TypeError("Compact agent input issue must be an object.")
        issue_number = issue.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("Compact agent input issueNumber must be positive.")
        cluster = issue.get("actionCluster")
        if cluster is not None:
            if not isinstance(cluster, dict):
                raise TypeError("Compact agent input actionCluster must be an object.")
            clusters[issue_number] = cluster
    return clusters


def _compact_issues(
    agent_input: object | None,
    *,
    snapshot_id: object,
) -> dict[int, dict[str, Any]]:
    if agent_input is None:
        return {}
    if not isinstance(agent_input, dict):
        raise TypeError("Compact agent input must be an object.")
    if agent_input.get("schemaVersion") != 1:
        raise ValueError("Compact agent input schemaVersion must be 1.")
    if agent_input.get("snapshotId") != snapshot_id:
        raise ValueError("Compact agent input snapshotId does not match prepared input.")
    issues = agent_input.get("issues")
    if not isinstance(issues, list):
        raise TypeError("Compact agent input issues must be a list.")
    result: dict[int, dict[str, Any]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise TypeError("Compact agent input issue must be an object.")
        issue_number = issue.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("Compact agent input issueNumber must be positive.")
        result[issue_number] = issue
    return result


def _source_comment_body(
    snapshot: dict[str, object],
    *,
    issue_number: int,
    comment_id: int,
) -> str | None:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    matches = [
        payload
        for record in evidence.values()
        if isinstance(record, dict) and record.get("kind") == "issue-comment"
        for payload in [record.get("payload")]
        if isinstance(payload, dict)
        and payload.get("sourceIssueNumber") == issue_number
        and payload.get("id") == comment_id
    ]
    if len(matches) != 1:
        return None
    body = matches[0].get("body")
    if not isinstance(body, str):
        return None
    return body


def _owned_status_comments(
    snapshot: dict[str, object],
    issue_number: int,
    idempotency_key: str,
) -> list[dict[str, object]]:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")

    canonical_matches: list[dict[str, object]] = []
    legacy_matches: list[dict[str, object]] = []
    legacy_keys = {
        f"issue:{issue_number}:watch",
        f"issue:{issue_number}:review-close",
        f"issue:{issue_number}:investigate",
        f"issue:{issue_number}:ping-human",
    }
    for record in evidence.values():
        if not isinstance(record, dict) or record.get("kind") != "issue-comment":
            continue
        payload = record.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("sourceIssueNumber") != issue_number
        ):
            continue
        status = payload.get("shepherdStatus")
        if not isinstance(status, dict) or status.get("owned") is not True:
            continue
        status_key = status.get("idempotencyKey")
        if status_key == idempotency_key:
            canonical_matches.append(payload)
        elif status_key in legacy_keys:
            legacy_matches.append(payload)

    if len(canonical_matches) > 1:
        raise ValueError(
            f"Issue {issue_number} has multiple owned canonical status comments."
        )
    if canonical_matches:
        return canonical_matches
    if not legacy_matches:
        return []

    # The old scheme could legitimately leave one comment per disposition.
    # Migrate the newest one so the cycle can converge on a single status slot.
    return [
        max(
            legacy_matches,
            key=lambda comment: (
                int(comment["id"])
                if isinstance(comment.get("id"), int)
                and not isinstance(comment["id"], bool)
                else 0
            ),
        )
    ]


def _selected_status_recommendation(
    issue: dict[str, object],
) -> dict[str, object] | None:
    recommendations = issue.get("recommendations")
    if not isinstance(recommendations, list):
        raise TypeError("Validated recommendations must be a list.")
    by_disposition: dict[str, list[dict[str, object]]] = {}
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise TypeError("Validated recommendation must be an object.")
        disposition = recommendation.get("disposition")
        if disposition in {"watch", "ping-human", "review-close"}:
            by_disposition.setdefault(str(disposition), []).append(recommendation)
    for disposition, matches in by_disposition.items():
        if len(matches) > 1:
            raise ValueError(
                f"Issue {issue['issueNumber']} has multiple {disposition} recommendations."
            )
    for disposition in ("review-close", "ping-human", "watch"):
        matches = by_disposition.get(disposition)
        if matches:
            return matches[0]
    return None


def _selected_investigation_recommendation(
    issue: dict[str, object],
) -> dict[str, object] | None:
    recommendations = issue.get("recommendations")
    if not isinstance(recommendations, list):
        raise TypeError("Validated recommendations must be a list.")
    matches = [
        recommendation
        for recommendation in recommendations
        if isinstance(recommendation, dict)
        and recommendation.get("disposition") == "investigate"
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    combined = dict(matches[0])
    combined["summary"] = " ".join(
        str(recommendation.get("summary") or "").strip()
        for recommendation in matches
        if str(recommendation.get("summary") or "").strip()
    )
    combined["evidenceIds"] = list(
        dict.fromkeys(
            evidence_id
            for recommendation in matches
            for evidence_id in recommendation.get("evidenceIds", [])
            if isinstance(evidence_id, str)
        )
    )
    return combined


_QUARANTINE_RECONCILIATION_BODY_FORMAT_VERSION = 3


def _licensed_quarantine_claims(finding: dict[str, Any]) -> list[dict[str, object]]:
    kind = finding.get("kind")
    claimed = finding.get("claimedTestName")
    entries = finding.get("currentSource")
    issue_url = finding.get("issueUrl")
    if not isinstance(kind, str) or not isinstance(entries, list):
        raise ValueError("Quarantine finding must have a kind and source entries.")
    if not isinstance(issue_url, str) or not issue_url:
        raise ValueError("Quarantine finding must identify its issue URL.")

    source_claims: list[dict[str, object]] = []
    linked_issue_urls: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("testName"), str)
            or not entry["testName"]
            or not isinstance(entry.get("file"), str)
            or not entry["file"]
            or not isinstance(entry.get("line"), int)
            or isinstance(entry["line"], bool)
            or entry["line"] <= 0
            or not isinstance(entry.get("quarantineIssueUrls"), list)
            or any(
                not isinstance(url, str) or not url
                for url in entry["quarantineIssueUrls"]
            )
        ):
            raise ValueError(
                "Quarantine source claims require a test name, location, and "
                "validated quarantine issue links."
            )
        issue_urls = list(entry["quarantineIssueUrls"])
        linked_issue_urls.extend(issue_urls)
        source_claims.append(
            {
                "kind": "source-method-match",
                "testName": entry["testName"],
                "file": entry["file"],
                "line": entry["line"],
                "quarantineIssueUrls": issue_urls,
            }
        )

    if kind == "unresolved-test-identity":
        if claimed is not None or entries:
            raise ValueError(
                "Unresolved test identity cannot license a method-level source claim."
            )
        return [
            {"kind": "test-identity-unresolved"},
            {
                "kind": "no-quarantine-link-to-current-issue",
                "issueUrl": issue_url,
            },
        ]

    if not isinstance(claimed, str) or not claimed:
        raise ValueError(f"{kind} requires a resolved test method name.")
    if kind == "label-without-attribute":
        if not entries or linked_issue_urls:
            raise ValueError(
                "label-without-attribute requires a resolved source method "
                "without quarantine issue links."
            )
    elif kind == "quarantined-against-other-issue":
        other_links = [
            url
            for url in linked_issue_urls
            if url.rstrip("/").casefold() != issue_url.rstrip("/").casefold()
        ]
        if not entries or not other_links:
            raise ValueError(
                "quarantined-against-other-issue requires another quarantine "
                "issue link."
            )
    elif kind == "ambiguous-inspection":
        if len(entries) < 2:
            raise ValueError(
                "ambiguous-inspection requires multiple source matches."
            )
    elif kind == "attribute-name-drift":
        if not entries:
            raise ValueError("attribute-name-drift requires a current source match.")
        if not any(
            url.rstrip("/").casefold() == issue_url.rstrip("/").casefold()
            for url in linked_issue_urls
        ):
            raise ValueError(
                "attribute-name-drift requires a quarantine link to the current issue."
            )
    elif kind in {"ambiguous-absence", "removed-test-closure-review"}:
        if kind == "removed-test-closure-review" and entries:
            raise ValueError(
                "removed-test-closure-review cannot contain a source match."
            )
        source_claims.append(
            {"kind": "source-method-not-found", "testName": claimed}
        )
    else:
        raise ValueError(f"Unsupported quarantine reconciliation kind: {kind}")

    source_claims.append(
        {
            "kind": (
                "quarantine-link-to-current-issue"
                if kind == "attribute-name-drift"
                else "no-quarantine-link-to-current-issue"
            ),
            "issueUrl": issue_url,
        }
    )
    prior = finding.get("priorQuarantine")
    if isinstance(prior, dict):
        source_claims.append(
            {
                "kind": "prior-quarantine",
                "pullRequestUrl": prior.get("pullRequestUrl"),
                "recordedAt": prior.get("recordedAt"),
            }
        )
    return source_claims


def _quarantine_decision(finding: dict[str, Any]) -> str:
    kind = finding["kind"]
    if kind == "unresolved-test-identity":
        return (
            "Identify the test method this issue tracks, or remove the "
            "`quarantined-test` label if it does not track a test."
        )
    if kind == "label-without-attribute":
        return (
            "Confirm whether this test should be quarantined. Either quarantine "
            "it against this issue or remove the `quarantined-test` label."
        )
    if kind == "quarantined-against-other-issue":
        issue_urls = sorted(
            {
                str(url)
                for entry in finding["currentSource"]
                for url in entry["quarantineIssueUrls"]
            }
        )
        return (
            f"Review whether this issue duplicates {', '.join(issue_urls)}. "
            "Close the duplicate or repoint the existing attribute if this issue "
            "is the canonical tracker; do not add a second quarantine for the "
            "same method."
        )
    if kind == "attribute-name-drift":
        return (
            "Update the issue title and metadata to the current method name. "
            "The shepherd does not edit issue metadata."
        )
    if kind == "ambiguous-inspection":
        return (
            "Identify the current canonical test method and update the issue "
            "metadata or source attribute. The shepherd will not infer identity "
            "from ambiguous matches."
        )
    if kind == "removed-test-closure-review":
        return (
            "Confirm the test was deleted rather than renamed, then close this "
            "issue. The shepherd does not close issues on absence."
        )
    if kind == "ambiguous-absence":
        return (
            "Decide whether the test was renamed or removed. The shepherd will "
            "not close this issue on absence alone."
        )
    raise ValueError(f"Unsupported quarantine reconciliation kind: {kind}")


def _quarantine_source_lines(finding: dict[str, Any]) -> list[str]:
    entries = finding.get("currentSource")
    if not isinstance(entries, list):
        raise TypeError("Quarantine reconciliation currentSource must be a list.")
    if not entries:
        # An empty source list means absence only when the inspector had a test name
        # to search. Without one, source was not checked and only the issue link was.
        if finding.get("claimedTestName") is None:
            return [
                "- The collected reconciliation evidence did not resolve a test "
                "method name, so no source method was checked. Only the attribute "
                "link was verified: no `[QuarantinedTest]` attribute references "
                "this issue. The test may still be quarantined against another "
                "issue.",
            ]
        return ["- No matching method exists in the inspected source."]
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("Quarantine reconciliation source entry must be an object.")
        issue_urls = entry.get("quarantineIssueUrls")
        if not isinstance(issue_urls, list):
            raise TypeError("Quarantine reconciliation issue URLs must be a list.")
        attribute = (
            "no `[QuarantinedTest]` attribute"
            if not issue_urls
            else "`[QuarantinedTest]` links " + ", ".join(str(url) for url in issue_urls)
        )
        lines.append(
            f"- `{entry['testName']}` — `tests/{entry['file']}:{entry['line']}` — "
            f"{attribute}"
        )
    return lines


def _render_quarantine_reconciliation_body(
    issue_number: int,
    finding: dict[str, Any],
    source_revision: str,
) -> str:
    _licensed_quarantine_claims(finding)
    leads = {
        "unresolved-test-identity": (
            "The CI shepherd could not resolve the test method this issue tracks."
        ),
        "label-without-attribute": (
            "The CI shepherd could not confirm this issue's `quarantined-test` "
            "label against the inspected source."
        ),
        "quarantined-against-other-issue": (
            "The CI shepherd found this test quarantined against a different issue."
        ),
        "attribute-name-drift": (
            "The CI shepherd found stale test-name metadata on this issue."
        ),
        "removed-test-closure-review": (
            "The CI shepherd found no trace of this issue's quarantined test in "
            "the inspected source."
        ),
        "ambiguous-absence": (
            "The CI shepherd could not tell whether this issue's test was "
            "renamed or removed."
        ),
        "ambiguous-inspection": (
            "The CI shepherd found multiple source matches for this issue's "
            "test name."
        ),
    }
    kind = finding.get("kind")
    lead = leads.get(str(kind))
    if lead is None:
        raise ValueError(f"Unsupported quarantine reconciliation kind: {kind}")

    prior = finding.get("priorQuarantine")
    prior_lines: list[str] = []
    if isinstance(prior, dict):
        prior_lines = [
            (
                f"**Previously quarantined by:** {prior['pullRequestUrl']} "
                f"(recorded {prior['recordedAt']})"
            ),
            "",
        ]
    return "\n".join(
        [
            f"[automated] {lead}",
            "",
            f"**Current source evidence** (revision `{source_revision}`):",
            *_quarantine_source_lines(finding),
            "",
            *prior_lines,
            f"**Decision needed:** {_quarantine_decision(finding)}",
            "",
            (
                "The shepherd made no source, label, or issue-metadata change "
                "and will not close this issue on source absence."
            ),
            "",
            _status_markers(issue_number),
        ]
    )


def _quarantine_reconciliation_findings(
    document: object | None,
) -> tuple[dict[int, dict[str, Any]], str]:
    if document is None:
        return {}, ""
    if not isinstance(document, dict):
        raise TypeError("Quarantine reconciliation must be an object.")
    if document.get("schemaVersion") != 1:
        raise ValueError("Quarantine reconciliation schemaVersion must be 1.")
    findings = document.get("findings")
    if not isinstance(findings, list):
        raise TypeError("Quarantine reconciliation findings must be a list.")
    if not findings:
        return {}, ""
    source_revision = document.get("sourceRevision")
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    ):
        raise ValueError(
            "Quarantine reconciliation findings require a pinned 40-hex sourceRevision."
        )
    source_tree_digest = document.get("sourceTreeDigest")
    if (
        not isinstance(source_tree_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", source_tree_digest) is None
    ):
        raise ValueError(
            "Quarantine reconciliation findings require a pinned sourceTreeDigest."
        )
    by_issue: dict[int, dict[str, Any]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            raise TypeError("Quarantine reconciliation finding must be an object.")
        issue_number = finding.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("Quarantine reconciliation issueNumber must be positive.")
        if issue_number in by_issue:
            raise ValueError(
                f"Issue {issue_number} has multiple quarantine reconciliation findings."
            )
        by_issue[issue_number] = finding
    return by_issue, source_revision


def _verified_quarantine_issues(
    document: object | None,
) -> dict[int, dict[str, Any]]:
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise TypeError("Quarantine reconciliation must be an object.")
    verified = document.get("verifiedIssues", [])
    if not isinstance(verified, list):
        raise TypeError("Quarantine reconciliation verifiedIssues must be a list.")
    by_issue: dict[int, dict[str, Any]] = {}
    for entry in verified:
        if not isinstance(entry, dict):
            raise TypeError("Verified quarantine issue must be an object.")
        issue_number = entry.get("issueNumber")
        tests = entry.get("tests")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
            or not isinstance(tests, list)
            or not tests
        ):
            raise ValueError("Verified quarantine issue is malformed.")
        if issue_number in by_issue:
            raise ValueError(
                f"Issue {issue_number} has multiple verified quarantine records."
            )
        by_issue[issue_number] = entry
    return by_issue


def _machine_actionability(
    prepared_issue: Mapping[str, Any],
    evidence_ids: list[object],
    *,
    frozen_issue: Mapping[str, Any],
    category: str,
) -> Mapping[str, Any] | None:
    actionability = derive_machine_actionability(
        frozen_issue, category, prepared_issue.get("investigationResults", []),
    )
    if actionability is None:
        return None
    verified_evidence_ids = actionability.get("evidenceIds")
    if (
        not isinstance(verified_evidence_ids, list)
        or not verified_evidence_ids
        or not all(
            isinstance(evidence_id, str) and evidence_id
            for evidence_id in verified_evidence_ids
        )
        or not set(verified_evidence_ids).issubset(set(evidence_ids))
    ):
        return None
    return actionability


def _delegation_handoffs(
    snapshot: Mapping[str, Any],
) -> dict[int, list[dict[str, Any]]]:
    status = snapshot.get("delegationStatus")
    if not isinstance(status, Mapping):
        return {}
    records = status.get("records")
    if not isinstance(records, list):
        raise TypeError("Delegation status records must be a list.")
    by_issue: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("Delegation status record must be an object.")
        if record.get("requiresHuman") is not True:
            continue
        issue_number = record.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("Delegation handoff issueNumber must be positive.")
        by_issue.setdefault(issue_number, []).append(record)
    for handoffs in by_issue.values():
        handoffs.sort(key=lambda record: str(record.get("startedAt") or ""))
    return by_issue


def _render_delegation_handoff_body(
    issue_number: int,
    records: list[dict[str, Any]],
) -> str:
    lines = [
        "[automated] GitHub Copilot is no longer actively working on this issue, "
        "and the delegation needs human review.",
        "",
        "**Delegation status:**",
    ]
    latest_reminder = records[-1].get("handoffReminder") if records else None
    if isinstance(latest_reminder, Mapping):
        ordinal = latest_reminder.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            lines.extend(["", f"**Reminder:** {ordinal}"])
    for record in records:
        task_id = record.get("taskId") or "pending association"
        task_state = record.get("taskState") or record.get("lifecycle")
        pull_requests = record.get("pullRequests")
        rendered_pulls: list[str] = []
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                if not isinstance(pull_request, Mapping):
                    continue
                number = pull_request.get("number")
                identity = (
                    f"PR #{number}"
                    if isinstance(number, int) and not isinstance(number, bool)
                    else str(pull_request.get("globalId") or "unknown PR")
                )
                rendered_pulls.append(
                    f"{identity} ({pull_request.get('state')})"
                )
        lines.append(
            f"- Task `{task_id}`: {task_state}; "
            f"pull requests: {', '.join(rendered_pulls) or 'none'}"
        )
    lines.extend(
        [
            "",
            (
                "**Decision needed:** Review the task result and any pull request. "
                "Provide missing information, continue with a local investigation, "
                "or approve a new delegation. A closed pull request is not treated "
                "as a fix unless merge evidence is available."
            ),
            "",
            _status_markers(issue_number),
        ]
    )
    return "\n".join(lines)


def build_watch_proposals(
    snapshot: object,
    prepared: object,
    judgments: object,
    shepherd_author: str,
    *,
    excluded_issue_numbers: frozenset[int] = frozenset(),
) -> dict[str, object]:
    validate_poc_judgments(prepared, judgments)
    if not isinstance(snapshot, dict):
        raise TypeError("Snapshot must be an object.")
    if not isinstance(prepared, dict) or not isinstance(judgments, dict):
        raise TypeError("Prepared input and judgments must be objects.")
    if not shepherd_author.strip():
        raise ValueError("Shepherd author must be nonempty.")

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared["issues"]
        if isinstance(issue, dict)
    }
    proposals: list[dict[str, object]] = []
    unchanged: list[int] = []
    for issue in judgments["issues"]:
        issue_number = issue["issueNumber"]
        if issue_number in excluded_issue_numbers:
            continue
        recommendation = _selected_status_recommendation(issue)
        if recommendation is None or recommendation["disposition"] != "watch":
            continue

        key = f"issue:{issue_number}:status"
        body = _render_watch_body(issue_number, recommendation, snapshot)
        existing = _owned_status_comments(snapshot, issue_number, key)
        if len(existing) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple owned watch status comments."
            )

        existing_body = (
            str(existing[0].get("body") or "").strip()
            if existing
            else ""
        )
        if existing and comment_bodies_materially_equal(existing_body, body):
            unchanged.append(issue_number)
            continue

        proposal: dict[str, object] = {
            "actionId": (
                f"{prepared['snapshotId']}:issue:{issue_number}:watch-comment"
            ),
            "issueNumber": issue_number,
            "issueUrl": prepared_issues[issue_number]["issueUrl"],
            "operation": "edit-comment" if existing else "create-comment",
            "evidenceBasis": "issue-state",
            "idempotencyKey": key,
            "body": body,
            "evidenceIds": list(recommendation["evidenceIds"]),
            "expectedIssueState": "open",
        }
        if existing:
            proposal["commentId"] = existing[0]["id"]
        proposals.append(proposal)

    proposals.sort(key=lambda item: int(item["issueNumber"]))
    unchanged.sort()
    result = {
        "schemaVersion": 1,
        "repository": prepared["repository"],
        "snapshotId": prepared["snapshotId"],
        "shepherdAuthor": shepherd_author,
        "proposals": proposals,
        "unchangedIssueNumbers": unchanged,
    }
    _finalize_execution_metadata(result, snapshot)
    return result


def _selected_delegation_recommendation(
    issue: dict[str, Any],
) -> dict[str, Any] | None:
    recommendations = issue.get("recommendations")
    if not isinstance(recommendations, list):
        raise TypeError("Validated recommendations must be a list.")
    matches = [
        recommendation
        for recommendation in recommendations
        if isinstance(recommendation, dict)
        and recommendation.get("disposition") == "delegate-copilot"
    ]
    if len(matches) > 1:
        raise ValueError(
            f"Issue {issue['issueNumber']} has multiple delegation recommendations."
        )
    return matches[0] if matches else None


def _delegation_base_branch(prepared: Mapping[str, object]) -> str:
    policy = prepared.get("repositoryPolicy")
    quarantine_policy = (
        policy.get("quarantinePullRequest")
        if isinstance(policy, Mapping)
        else None
    )
    base_ref = (
        quarantine_policy.get("baseRef")
        if isinstance(quarantine_policy, Mapping)
        else None
    )
    if not isinstance(base_ref, str) or not base_ref:
        raise ValueError(
            "Prepared repository policy must provide a nonempty baseRef "
            "for Copilot delegation."
        )
    return base_ref


def _delegation_instructions(
    issue_number: int,
    handoff: Mapping[str, Any],
    verified_tests: object | None = None,
) -> str:
    verified_context = ""
    if isinstance(verified_tests, list) and verified_tests:
        rendered_tests = []
        for test in verified_tests:
            if not isinstance(test, Mapping):
                raise ValueError("Verified quarantine test must be an object.")
            test_name = test.get("testName")
            file = test.get("file")
            line = test.get("line")
            if (
                not isinstance(test_name, str)
                or not test_name
                or not isinstance(file, str)
                or not file
                or not isinstance(line, int)
                or isinstance(line, bool)
                or line <= 0
            ):
                raise ValueError(
                    "Verified quarantine test identity is incomplete."
                )
            rendered_tests.append(f"`{test_name}` at `tests/{file}:{line}`")
        verified_context = (
            " Source reconciliation confirmed the quarantined target"
            f"{'s' if len(rendered_tests) != 1 else ''}: "
            + ", ".join(rendered_tests)
            + ". Do not modify or remove the `[QuarantinedTest]` attribute; "
            "unquarantine is a separately authorized change."
        )
    validation = "\n".join(f"- {command}" for command in handoff["validation"])
    return (
        f"Investigate and fix issue #{issue_number}. Make the smallest complete "
        "change that addresses the reported failure, add focused regression "
        "coverage that would fail without the fix, and avoid unrelated changes."
        f"{verified_context} "
        f"\n\nProblem: {handoff['problem']}\n"
        f"Likely paths: {', '.join(handoff['likelyPaths'])}\n"
        f"Validation:\n{validation}\n\n"
        f"Open a draft pull request whose body includes `Fixes #{issue_number}`. "
        "If the issue cannot be fixed from the available evidence, keep the pull "
        "request in draft and clearly record the missing evidence or human "
        "decision needed."
    )


def build_action_proposals(
    snapshot: object,
    prepared: object,
    judgments: object,
    shepherd_author: str,
    *,
    agent_input: object | None = None,
    quarantine_reconciliation: object | None = None,
) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise TypeError("Snapshot must be an object.")
    open_issue_numbers = frozenset(
        int(issue_number)
        for issue_number in snapshot.get("openIssues", [])
    )
    delegated_issue_numbers = frozenset(
        int(issue_number)
        for issue_number in snapshot.get("delegatedIssues", [])
    )
    reconciliation_findings, reconciliation_revision = (
        _quarantine_reconciliation_findings(quarantine_reconciliation)
    )
    verified_quarantines = _verified_quarantine_issues(
        quarantine_reconciliation
    )
    delegation_handoffs = _delegation_handoffs(snapshot)
    reconciliation_issue_numbers = frozenset(reconciliation_findings)
    result = build_watch_proposals(
        snapshot,
        prepared,
        judgments,
        shepherd_author,
        excluded_issue_numbers=(
            reconciliation_issue_numbers | frozenset(delegation_handoffs)
        ),
    )
    if not isinstance(prepared, dict) or not isinstance(judgments, dict):
        raise TypeError("Prepared input and judgments must be objects.")
    judgments_by_issue = {
        issue.get("issueNumber"): issue
        for issue in judgments.get("issues", [])
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
    }
    action_clusters = _action_clusters(
        agent_input,
        snapshot_id=prepared.get("snapshotId"),
    )
    compact_issues = _compact_issues(
        agent_input,
        snapshot_id=prepared.get("snapshotId"),
    )
    frozen_issues = {
        item["issueNumber"]: item
        for item in prepare_assessment(snapshot, max_bundle_records=prepared.get("maxBundleRecords", 25))["issues"]
    } if any(
        recommendation.get("disposition") in {"review-close", "delegate-copilot"}
        for issue in judgments["issues"]
        for recommendation in issue["recommendations"]
    ) else {}

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared["issues"]
        if isinstance(issue, dict)
    }
    proposals = result["proposals"]
    if not isinstance(proposals, list):
        raise TypeError("Validated proposals must be a list.")
    blocked_recommendations: list[dict[str, object]] = []
    result["blockedRecommendations"] = blocked_recommendations

    for issue in judgments["issues"]:
        issue_number = issue["issueNumber"]
        status_recommendation = _selected_status_recommendation(issue)
        delegation_recommendation = _selected_delegation_recommendation(issue)
        if issue_number in reconciliation_findings:
            # Deterministic lifecycle evidence outranks an advisory model status
            # for the single canonical comment slot, but the displaced
            # recommendation stays visible instead of disappearing.
            if status_recommendation is not None:
                blocked_recommendations.append(
                    {
                        "issueNumber": issue_number,
                        "disposition": status_recommendation["disposition"],
                        "blockingReasons": [
                            "superseded-by-quarantine-source-reconciliation"
                        ],
                        "evidenceIds": list(status_recommendation["evidenceIds"]),
                    }
                )
            continue
        if issue_number in delegation_handoffs:
            continue
        prepared_issue = prepared_issues[issue_number]
        compact_issue = compact_issues.get(issue_number, {})
        action_cluster = action_clusters.get(issue_number)
        is_duplicate = (
            isinstance(action_cluster, dict)
            and action_cluster.get("role") == "superseded"
        )
        recovery = frozen_issues.get(issue_number, {}).get("recovery", {})
        has_recovery = (
            recovery.get("status") == "verified"
            and recovery == prepared_issue.get("recovery")
            and all(
                compact_issue.get("recovery", recovery).get(key) == recovery.get(key)
                for key in ("status", "complete", "evidenceIds")
            )
            and status_recommendation is not None
            and set(recovery["evidenceIds"]).issubset(status_recommendation["evidenceIds"])
        )
        closure_supersedes_delegation = (
            status_recommendation is not None
            and status_recommendation["disposition"] == "review-close"
            and (is_duplicate or has_recovery)
        )
        if delegation_recommendation is not None and closure_supersedes_delegation:
            blocked_recommendations.append(
                {
                    "issueNumber": issue_number,
                    "disposition": "delegate-copilot",
                    "blockingReasons": ["superseded-by-closure-review"],
                    "evidenceIds": list(delegation_recommendation["evidenceIds"]),
                }
            )
            delegation_recommendation = None
        actionability = (
            _machine_actionability(
                prepared_issue, list(delegation_recommendation["evidenceIds"]),
                frozen_issue=frozen_issues.get(issue_number, {}),
                category=issue["category"],
            ) if delegation_recommendation is not None else None
        )
        if delegation_recommendation is not None and actionability is None:
            blocked_recommendations.append(
                {
                    "issueNumber": issue_number,
                    "disposition": "delegate-copilot",
                    "blockingReasons": ["machine-actionability-not-verified"],
                    "evidenceIds": list(
                        delegation_recommendation["evidenceIds"]
                    ),
                }
            )
            delegation_recommendation = None
        if delegation_recommendation is not None:
            verified_quarantine = verified_quarantines.get(issue_number)
            delegation_status = snapshot.get("delegationStatus")
            episode_ordinals = (
                delegation_status.get("episodeOrdinals", {})
                if isinstance(delegation_status, Mapping)
                else {}
            )
            delegation_episode_ordinal = (
                episode_ordinals.get(str(issue_number), 1)
                if isinstance(episode_ordinals, Mapping)
                else 1
            )
            if (
                not isinstance(delegation_episode_ordinal, int)
                or isinstance(delegation_episode_ordinal, bool)
                or delegation_episode_ordinal <= 0
            ):
                raise ValueError("Delegation episode ordinal must be positive.")
            proposal: dict[str, object] = {
                "actionId": (
                    f"{prepared['snapshotId']}:issue:{issue_number}:"
                    "assign-copilot"
                ),
                "issueNumber": issue_number,
                "issueUrl": prepared_issues[issue_number]["issueUrl"],
                "operation": "assign-copilot",
                "evidenceBasis": (
                    "source-reconciliation"
                    if verified_quarantine is not None
                    else "ci-occurrence"
                ),
                "idempotencyKey": (
                    f"issue:{issue_number}:copilot-assignment:"
                    f"episode-{delegation_episode_ordinal}"
                ),
                "evidenceIds": list(delegation_recommendation["evidenceIds"]),
                "expectedIssueState": "open",
                "targetRepository": snapshot["repository"],
                "baseBranch": _delegation_base_branch(prepared),
                "customInstructions": _delegation_instructions(
                    issue_number,
                    actionability["fixHandoff"],
                    (
                        verified_quarantine.get("tests")
                        if verified_quarantine is not None
                        else None
                    ),
                ),
                "model": "",
            }
            if verified_quarantine is not None:
                if not isinstance(quarantine_reconciliation, dict):
                    raise TypeError("Quarantine reconciliation must be an object.")
                source_revision = quarantine_reconciliation.get("sourceRevision")
                source_tree_digest = quarantine_reconciliation.get(
                    "sourceTreeDigest"
                )
                inspector_tree_digest = quarantine_reconciliation.get(
                    "inspectorTreeDigest"
                )
                if (
                    not isinstance(source_revision, str)
                    or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
                    or not isinstance(source_tree_digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", source_tree_digest)
                    is None
                    or not isinstance(inspector_tree_digest, str)
                    or re.fullmatch(
                        r"sha256:[0-9a-f]{64}", inspector_tree_digest
                    )
                    is None
                ):
                    raise ValueError(
                        "Verified quarantine issues require pinned source evidence."
                    )
                proposal["sourceEvidenceFingerprint"] = {
                    "sourceRevision": source_revision,
                    "sourceTreeDigest": source_tree_digest,
                    "inspectorTreeDigest": inspector_tree_digest,
                    "findingDigest": "sha256:"
                    + hashlib.sha256(
                        stable_json(verified_quarantine).encode("utf-8")
                    ).hexdigest(),
                }
            proposals.append(proposal)
        if status_recommendation is None:
            investigation = _selected_investigation_recommendation(issue)
            if investigation is not None:
                key = f"issue:{issue_number}:status"
                existing = _owned_status_comments(snapshot, issue_number, key)
                if len(existing) > 1:
                    raise ValueError(
                        f"Issue {issue_number} has multiple owned status comments."
                    )
                if existing:
                    body = _render_retired_status_body(
                        issue_number,
                        investigation,
                        snapshot,
                    )
                    existing_body = str(existing[0].get("body") or "").strip()
                    if comment_bodies_materially_equal(existing_body, body):
                        unchanged = result["unchangedIssueNumbers"]
                        if (
                            isinstance(unchanged, list)
                            and issue_number not in unchanged
                        ):
                            unchanged.append(issue_number)
                    else:
                        proposals.append(
                            {
                                "actionId": (
                                    f"{prepared['snapshotId']}:issue:{issue_number}:"
                                    "retire-status-comment"
                                ),
                                "issueNumber": issue_number,
                                "issueUrl": prepared_issues[issue_number]["issueUrl"],
                                "operation": "edit-comment",
                                "evidenceBasis": "issue-state",
                                "commentId": existing[0]["id"],
                                "idempotencyKey": key,
                                "body": body,
                                "evidenceIds": list(investigation["evidenceIds"]),
                                "expectedIssueState": "open",
                            }
                        )
        if (
            status_recommendation is not None
            and status_recommendation["disposition"] == "ping-human"
        ):
            recommendation = status_recommendation
            key = f"issue:{issue_number}:status"
            body = _render_ping_human_body(
                issue_number,
                recommendation,
                snapshot,
            )
            existing = _owned_status_comments(snapshot, issue_number, key)
            if len(existing) > 1:
                raise ValueError(
                    f"Issue {issue_number} has multiple owned status comments."
                )
            existing_body = (
                str(existing[0].get("body") or "").strip()
                if existing
                else ""
            )
            if existing and comment_bodies_materially_equal(existing_body, body):
                unchanged = result["unchangedIssueNumbers"]
                if isinstance(unchanged, list) and issue_number not in unchanged:
                    unchanged.append(issue_number)
            else:
                proposal: dict[str, object] = {
                    "actionId": (
                        f"{prepared['snapshotId']}:issue:{issue_number}:"
                        "ping-human-comment"
                    ),
                    "issueNumber": issue_number,
                    "issueUrl": prepared_issues[issue_number]["issueUrl"],
                    "operation": (
                        "edit-comment" if existing else "create-comment"
                    ),
                    "evidenceBasis": "issue-state",
                    "idempotencyKey": key,
                    "body": body,
                    "evidenceIds": list(recommendation["evidenceIds"]),
                    "expectedIssueState": "open",
                }
                if existing:
                    proposal["commentId"] = existing[0]["id"]
                proposals.append(proposal)

        if (
            status_recommendation is None
            or status_recommendation["disposition"] != "review-close"
        ):
            continue

        if not closure_supersedes_delegation:
            blocked_recommendations.append(
                {
                    "issueNumber": issue_number,
                    "disposition": "review-close",
                    "blockingReasons": [
                        "missing-deterministic-resolution-evidence"
                    ],
                    "evidenceIds": list(status_recommendation["evidenceIds"]),
                }
            )
            continue

        recommendation = status_recommendation
        key = f"issue:{issue_number}:status"
        body = (
            _render_duplicate_close_body(
                issue_number,
                recommendation,
                action_cluster,
                snapshot,
            )
            if is_duplicate
            else _render_exact_recovery_body(
                issue_number,
                recommendation,
                recovery,
                snapshot,
            )
        )
        close_reason = "duplicate" if is_duplicate else "completed"
        existing = _owned_status_comments(snapshot, issue_number, key)
        if len(existing) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple owned review-close status comments."
            )

        comment_action_id = (
            f"{prepared['snapshotId']}:issue:{issue_number}:review-close-comment"
        )
        existing_body = (
            str(existing[0].get("body") or "").strip()
            if existing
            else ""
        )
        comment_proposed = not existing or not comment_bodies_materially_equal(
            existing_body,
            body,
        )
        if comment_proposed:
            comment: dict[str, object] = {
                "actionId": comment_action_id,
                "issueNumber": issue_number,
                "issueUrl": prepared_issue["issueUrl"],
                "operation": "edit-comment" if existing else "create-comment",
                "evidenceBasis": "ci-occurrence",
                "idempotencyKey": key,
                "body": body,
                "evidenceIds": list(recommendation["evidenceIds"]),
                "expectedIssueState": "open",
            }
            if existing:
                comment["commentId"] = existing[0]["id"]
            proposals.append(comment)

        close: dict[str, object] = {
            "actionId": (
                f"{prepared['snapshotId']}:issue:{issue_number}:review-close"
            ),
            "issueNumber": issue_number,
            "issueUrl": prepared_issue["issueUrl"],
            "operation": "close-issue",
            "evidenceBasis": "ci-occurrence",
            "closeReason": close_reason,
            "idempotencyKey": f"issue:{issue_number}:close:{close_reason}",
            "evidenceIds": list(recommendation["evidenceIds"]),
            "expectedIssueState": "open",
        }
        if comment_proposed:
            close["dependsOn"] = comment_action_id
        proposals.append(close)

    for issue_number, finding in sorted(reconciliation_findings.items()):
        prepared_issue = prepared_issues.get(issue_number)
        if not isinstance(prepared_issue, dict):
            continue
        key = f"issue:{issue_number}:status"
        finding_digest = "sha256:" + hashlib.sha256(
            stable_json(
                {
                    "bodyFormatVersion": (
                        _QUARANTINE_RECONCILIATION_BODY_FORMAT_VERSION
                    ),
                    "finding": finding,
                }
            ).encode("utf-8")
        ).hexdigest()
        licensed_claims = _licensed_quarantine_claims(finding)
        body = _render_quarantine_reconciliation_body(
            issue_number,
            finding,
            reconciliation_revision,
        )
        existing = _owned_status_comments(snapshot, issue_number, key)
        if len(existing) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple owned status comments."
            )
        existing_body = (
            str(existing[0].get("body") or "").strip() if existing else ""
        )
        if existing and _quarantine_comment_bodies_materially_equal(
            existing_body,
            body,
        ):
            unchanged = result["unchangedIssueNumbers"]
            if isinstance(unchanged, list) and issue_number not in unchanged:
                unchanged.append(issue_number)
            continue
        proposal = {
            "actionId": (
                f"{prepared['snapshotId']}:issue:{issue_number}:"
                "quarantine-reconciliation-comment"
            ),
            "issueNumber": issue_number,
            "issueUrl": prepared_issue["issueUrl"],
            "operation": "edit-comment" if existing else "create-comment",
            "evidenceBasis": "source-reconciliation",
            "idempotencyKey": key,
            "body": body,
            "licensedClaims": licensed_claims,
            "evidenceIds": [f"issue:{issue_number}"],
            "expectedIssueState": "open",
            "sourceEvidenceFingerprint": {
                "sourceRevision": reconciliation_revision,
                "sourceTreeDigest": quarantine_reconciliation["sourceTreeDigest"],
                "inspectorTreeDigest": quarantine_reconciliation[
                    "inspectorTreeDigest"
                ],
                "findingDigest": finding_digest,
            },
        }
        if existing:
            proposal["commentId"] = existing[0]["id"]
        proposals.append(proposal)

    repository = snapshot.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Snapshot repository must be nonempty.")
    for issue_number, records in sorted(delegation_handoffs.items()):
        if issue_number in reconciliation_issue_numbers:
            continue
        context = delegation_context(snapshot, issue_number)
        prepared_context = prepared_issues.get(issue_number, {}).get("delegationContext")
        if snapshot.get("delegationStatus", {}).get("status") != "complete":
            continue
        latest_record = records[-1]
        if issue_number not in open_issue_numbers | delegated_issue_numbers:
            continue
        if (
            issue_number in delegated_issue_numbers
            and latest_record.get("issueOpen") is not True
        ):
            continue
        issue_judgment = judgments_by_issue.get(issue_number)
        status_recommendation = (
            _selected_status_recommendation(issue_judgment)
            if isinstance(issue_judgment, dict)
            else None
        )
        if (
            status_recommendation is None
            or status_recommendation.get("disposition") != "ping-human"
        ):
            blocked_recommendations.append(
                {
                    "issueNumber": issue_number,
                    "disposition": "delegation-handoff",
                    "blockingReasons": ["validated-ping-human-required"],
                    "evidenceIds": [f"issue:{issue_number}"],
                }
            )
            continue
        if (
            context is None or context != prepared_context
            or context.get("decisionRequired") is not True
            or (
                issue_number in compact_issues
                and compact_issues[issue_number].get("delegationContext") != context
            )
        ):
            continue
        reminder = latest_record.get("handoffReminder")
        if isinstance(reminder, Mapping) and reminder.get("state") != "pending":
            continue
        if isinstance(reminder, Mapping):
            next_wakeup = reminder.get("nextWakeup")
            if not isinstance(next_wakeup, Mapping):
                raise ValueError("Pending handoff reminder is missing nextWakeup.")
            if parse_aware_iso8601(
                next_wakeup.get("evaluateAt"),
                "handoffReminder.nextWakeup.evaluateAt",
            ) > parse_aware_iso8601(
                snapshot.get("collectedAt"),
                "snapshot.collectedAt",
            ):
                continue
        reminder_identity = reminder_action_identity(records[-1])
        key = f"issue:{issue_number}:status"
        body = _render_delegation_handoff_body(issue_number, records)
        existing = _owned_status_comments(snapshot, issue_number, key)
        if len(existing) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple owned status comments."
            )
        existing_body = (
            str(existing[0].get("body") or "").strip() if existing else ""
        )
        if existing and comment_bodies_materially_equal(existing_body, body):
            unchanged = result["unchangedIssueNumbers"]
            if isinstance(unchanged, list) and issue_number not in unchanged:
                unchanged.append(issue_number)
            continue
        action_suffix = "delegation-handoff-comment"
        if reminder_identity is not None:
            episode_id, ordinal = reminder_identity
            action_suffix = (
                f"ping-human-comment:{episode_id}:reminder-{ordinal}"
            )
        proposal = {
            "actionId": (
                f"{prepared['snapshotId']}:issue:{issue_number}:"
                f"{action_suffix}"
            ),
            "issueNumber": issue_number,
            "issueUrl": (
                f"https://github.com/{repository}/issues/{issue_number}"
            ),
            "operation": "edit-comment" if existing else "create-comment",
            "evidenceBasis": "delegation-state",
            "idempotencyKey": key,
            "body": body,
            "evidenceIds": list(status_recommendation["evidenceIds"]),
            "expectedIssueState": "open",
        }
        if existing:
            proposal["commentId"] = existing[0]["id"]
        proposals.append(proposal)

    operation_order = {
        "create-comment": 0,
        "edit-comment": 0,
        "close-issue": 1,
    }
    proposals.sort(
        key=lambda item: (
            int(item["issueNumber"]),
            operation_order.get(str(item["operation"]), 2),
        )
    )
    blocked_recommendations.sort(key=lambda item: int(item["issueNumber"]))
    _finalize_execution_metadata(result, snapshot)
    return result
