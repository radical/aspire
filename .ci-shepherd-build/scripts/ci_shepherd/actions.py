from __future__ import annotations

from typing import Any

from ci_shepherd.poc import validate_poc_judgments


def _status_markers(issue_number: int) -> str:
    return (
        "<!-- ci-shepherd:role=status -->\n"
        f"<!-- ci-shepherd:idempotency-key=issue:{issue_number}:status -->"
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
            "requiresSeparateApproval": True,
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
    *,
    agent_input: object | None = None,
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
    action_clusters = _action_clusters(
        agent_input,
        snapshot_id=prepared.get("snapshotId"),
    )
    compact_issues = _compact_issues(
        agent_input,
        snapshot_id=prepared.get("snapshotId"),
    )

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
        status_recommendation = _selected_status_recommendation(issue)
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
                    if existing_body == body.strip():
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
                                "commentId": existing[0]["id"],
                                "idempotencyKey": key,
                                "body": body,
                                "evidenceIds": list(investigation["evidenceIds"]),
                                "expectedIssueState": "open",
                                "requiresSeparateApproval": True,
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
            if existing and existing_body == body.strip():
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
                    "idempotencyKey": key,
                    "body": body,
                    "evidenceIds": list(recommendation["evidenceIds"]),
                    "expectedIssueState": "open",
                    "requiresSeparateApproval": True,
                }
                if existing:
                    proposal["commentId"] = existing[0]["id"]
                proposals.append(proposal)

        if (
            status_recommendation is None
            or status_recommendation["disposition"] != "review-close"
        ):
            continue

        prepared_issue = prepared_issues[issue_number]
        compact_issue = compact_issues.get(issue_number, {})
        action_cluster = action_clusters.get(issue_number)
        is_duplicate = (
            isinstance(action_cluster, dict)
            and action_cluster.get("role") == "superseded"
        )
        has_recovery = (
            prepared_issue.get("candidateState") == "resolved"
            and prepared_issue.get("candidateAction") == "recommend-close"
            and bool(prepared_issue.get("resolutionEvidence"))
        )
        recovered_run_evidence_id = compact_issue.get("recoveredRunEvidenceId")
        has_run_recovery = (
            isinstance(recovered_run_evidence_id, str)
            and bool(recovered_run_evidence_id)
        )
        if not is_duplicate and not has_recovery and not has_run_recovery:
            raise ValueError(
                f"Issue {issue_number} review-close requires deterministic "
                "resolution evidence."
            )

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
            else _render_recovered_run_close_body(
                issue_number,
                recommendation,
                recovered_run_evidence_id,
                snapshot,
            )
            if has_run_recovery and not has_recovery
            else _render_close_body(
                issue_number,
                recommendation,
                prepared_issue,
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
            "closeReason": close_reason,
            "requiresSeparateApproval": True,
            "idempotencyKey": f"issue:{issue_number}:close:{close_reason}",
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
