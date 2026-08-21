from __future__ import annotations

from typing import Any

from ci_shepherd.poc import validate_poc_judgments


def _status_markers(issue_number: int, disposition: str) -> str:
    return (
        "<!-- ci-shepherd:role=status -->\n"
        f"<!-- ci-shepherd:idempotency-key=issue:{issue_number}:{disposition} -->"
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
            _status_markers(issue_number, "watch"),
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
            _status_markers(issue_number, "review-close"),
        ]
    )


def _owned_status_comments(
    snapshot: dict[str, object],
    issue_number: int,
    idempotency_key: str,
) -> list[dict[str, object]]:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")

    matches: list[dict[str, object]] = []
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
        if (
            isinstance(status, dict)
            and status.get("owned") is True
            and status.get("idempotencyKey") == idempotency_key
        ):
            matches.append(payload)
    return matches


def build_watch_proposals(
    snapshot: object,
    prepared: object,
    judgments: object,
    shepherd_author: str,
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
        watch_recommendations = [
            recommendation
            for recommendation in issue["recommendations"]
            if recommendation["disposition"] == "watch"
        ]
        if len(watch_recommendations) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple watch recommendations."
            )
        if not watch_recommendations:
            continue

        recommendation = watch_recommendations[0]
        key = f"issue:{issue_number}:watch"
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
        if existing and existing_body == body.strip():
            unchanged.append(issue_number)
            continue

        proposal: dict[str, object] = {
            "actionId": (
                f"{prepared['snapshotId']}:issue:{issue_number}:watch-comment"
            ),
            "issueNumber": issue_number,
            "issueUrl": prepared_issues[issue_number]["issueUrl"],
            "operation": "edit-comment" if existing else "create-comment",
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
    return {
        "schemaVersion": 1,
        "repository": prepared["repository"],
        "snapshotId": prepared["snapshotId"],
        "shepherdAuthor": shepherd_author,
        "proposals": proposals,
        "unchangedIssueNumbers": unchanged,
    }


def build_action_proposals(
    snapshot: object,
    prepared: object,
    judgments: object,
    shepherd_author: str,
) -> dict[str, object]:
    result = build_watch_proposals(
        snapshot,
        prepared,
        judgments,
        shepherd_author,
    )
    if not isinstance(snapshot, dict):
        raise TypeError("Snapshot must be an object.")
    if not isinstance(prepared, dict) or not isinstance(judgments, dict):
        raise TypeError("Prepared input and judgments must be objects.")

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared["issues"]
        if isinstance(issue, dict)
    }
    proposals = result["proposals"]
    if not isinstance(proposals, list):
        raise TypeError("Validated proposals must be a list.")

    for issue in judgments["issues"]:
        issue_number = issue["issueNumber"]
        close_recommendations = [
            recommendation
            for recommendation in issue["recommendations"]
            if recommendation["disposition"] == "review-close"
        ]
        if len(close_recommendations) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple review-close recommendations."
            )
        if not close_recommendations:
            continue

        prepared_issue = prepared_issues[issue_number]
        if (
            prepared_issue.get("candidateState") != "resolved"
            or prepared_issue.get("candidateAction") != "recommend-close"
            or not prepared_issue.get("resolutionEvidence")
        ):
            raise ValueError(
                f"Issue {issue_number} review-close requires deterministic "
                "resolution evidence."
            )

        recommendation = close_recommendations[0]
        key = f"issue:{issue_number}:review-close"
        body = _render_close_body(
            issue_number,
            recommendation,
            prepared_issue,
            snapshot,
        )
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
        comment_proposed = not existing or existing_body != body.strip()
        if comment_proposed:
            comment: dict[str, object] = {
                "actionId": comment_action_id,
                "issueNumber": issue_number,
                "issueUrl": prepared_issue["issueUrl"],
                "operation": "edit-comment" if existing else "create-comment",
                "idempotencyKey": key,
                "body": body,
                "evidenceIds": list(recommendation["evidenceIds"]),
                "expectedIssueState": "open",
                "requiresSeparateApproval": True,
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
            "closeReason": "completed",
            "requiresSeparateApproval": True,
            "idempotencyKey": f"issue:{issue_number}:close:completed",
            "evidenceIds": list(recommendation["evidenceIds"]),
            "expectedIssueState": "open",
        }
        if comment_proposed:
            close["dependsOn"] = comment_action_id
        proposals.append(close)

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
    return result
