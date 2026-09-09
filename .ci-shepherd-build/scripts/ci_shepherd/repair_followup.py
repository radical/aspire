from __future__ import annotations

"""Read-only post-repair evidence, separate from task/PR attempt disposition."""

import copy
import re
from typing import Any, Mapping

from .eligibility import delegation_replacement_ready
from .observations import build_repair_evidence
from .signals import extract_issue_signals
from .timeutils import parse_aware_iso8601


def build_repair_followup(
    snapshot: Mapping[str, Any],
    observations: Mapping[str, Any],
    issue: Mapping[str, Any],
    workflow_health: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    records = [
        record for record in snapshot.get("delegationStatus", {}).get("records", [])
        if record.get("repository") == snapshot["repository"]
        and record.get("issueNumber") == issue["issueNumber"]
    ]
    if not records:
        return None
    records.sort(key=lambda record: (
        parse_aware_iso8601(record["startedAt"], "delegation.startedAt"), record["actionId"],
    ))
    latest = records[-1]
    attempt_status = (
        "awaiting-post-fix-success" if latest.get("attemptOutcome") == "merged" else {
            "running": "work-in-progress", "awaiting_pull_request": "work-in-progress",
            "handoff_required": "human-handoff", "closed_unmerged": "human-handoff",
            "retired": "human-handoff",
        }.get(latest.get("lifecycle"), "unknown")
    )
    if any(
        record.get("taskObservation") == "available"
        and record.get("taskState") in {"queued", "in_progress"}
        for record in records
    ):
        attempt_status = "work-in-progress"
    result = {
        "issueNumber": issue["issueNumber"],
        "status": attempt_status,
        "requiresNewDecision": latest.get("requiresNewDecision") is True,
        "attempts": [
            {
                "actionId": record["actionId"], "taskId": record.get("taskId"),
                "taskState": record.get("taskState"), "lifecycle": record.get("lifecycle"),
                "attemptOutcome": record.get("attemptOutcome"),
                "pullRequests": copy.deepcopy(record.get("pullRequests", [])),
            }
            for record in records
        ],
    }
    if attempt_status != "awaiting-post-fix-success":
        return result
    if workflow_health is None:
        result.update(status="unknown", reason="Affected workflow/job subject is unavailable.")
        return result
    subject = _repair_subject(snapshot, observations, issue, latest, workflow_health)
    if subject is None:
        result.update(status="unknown", reason="The original affected job is missing or ambiguous.")
        return result
    result["subject"] = subject
    merged = [
        pull for pull in latest.get("pullRequests", [])
        if pull.get("state") == "merged" or pull.get("lastKnownState") == "merged"
    ]
    if not merged or any(
        pull.get("state") not in {"closed", "merged"}
        and not (pull.get("state") == "unknown" and pull.get("lastKnownState") == "merged")
        for pull in latest.get("pullRequests", [])
    ):
        result.update(status="unknown", reason="Merged fix identity or associated pull request disposition is unresolved.")
        return result
    if any(not pull.get("mergedAt") or not _commit_sha(pull.get("mergeCommitSha")) for pull in merged):
        result.update(status="unknown", reason="Verified merge timestamp or commit identity is unavailable.")
        return result
    merged_times = [parse_aware_iso8601(pull["mergedAt"], "mergedAt") for pull in merged]
    merged_at = max(merged_times)
    if (
        min(merged_times) < parse_aware_iso8601(latest["startedAt"], "delegation.startedAt")
        or merged_at > parse_aware_iso8601(snapshot["collectedAt"], "collectedAt")
    ):
        result.update(status="unknown", reason="Merge timestamps fall outside this observed delegation.")
        return result
    missing = []
    for coverage in observations.get("coverage", []):
        execution = _execution(snapshot, coverage, subject)
        if (
            execution is not None
            and coverage.get("independentRecoveryEligible") is True
            and execution["conclusion"] == "success"
            and execution["startedAt"] >= merged_at
        ):
            containment = [
                _fix_containment(snapshot, pull["mergeCommitSha"], execution["headSha"])
                for pull in merged
            ]
            if any(contains is False for contains, _ in containment):
                continue
            if any(contains is None for contains, _ in containment):
                missing.extend(source for contains, source in containment if contains is None)
                continue
            result.update(status="verified", verification={
                key: execution[key] for key in ("runId", "attempt", "jobId", "headSha", "evidenceIds")
            })
            result["verification"]["observedAt"] = coverage["observedAt"]
            result["verification"]["commitComparisons"] = copy.deepcopy([
                source for _, source in containment if source is not None
            ])
            break
    failures = {}
    unverified_failures = {}
    missing_failure_proof = []
    for occurrence in observations.get("occurrences", []):
        execution = _execution(snapshot, occurrence, subject)
        if (
            execution is not None and execution["conclusion"] in {"failure", "timed_out"}
            and execution["startedAt"] >= merged_at
        ):
            failure = {
                key: execution[key] for key in ("runId", "attempt", "jobId", "headSha", "evidenceIds")
            }
            failure.update(
                observedAt=occurrence["observedAt"], sameRootCause="unknown",
                sourceIssueNumber=occurrence["issueNumber"],
            )
            containment = [
                _fix_containment(snapshot, pull["mergeCommitSha"], execution["headSha"])
                for pull in merged
            ]
            if any(contains is False for contains, _ in containment):
                continue
            if any(contains is None for contains, _ in containment):
                missing_failure_proof.extend(source for contains, source in containment if contains is None)
                unverified_failures[(execution["runId"], execution["attempt"], execution["jobId"])] = failure
                continue
            failures[(execution["runId"], execution["attempt"], execution["jobId"])] = failure
    if failures:
        # Matching execution subjects establish recurrence, not root-cause identity
        # or permission to create another assignment, even if a later run passes.
        result.update(
            status="reassessment-required",
            reason="The affected job failed after the repair merged; root cause needs reassessment.",
            laterFailures=sorted(failures.values(), key=lambda item: (
                parse_aware_iso8601(item["observedAt"], "observedAt"), item["runId"], item["attempt"],
            )),
        )
    elif unverified_failures:
        result.update(
            status="unknown",
            reason="A later failed job is not yet proven to contain the merged fix.",
            unverifiedFailures=list(unverified_failures.values()),
            missingEvidence=list({source["url"]: source for source in missing_failure_proof}.values()),
        )
    elif result["status"] != "verified" and missing:
        result.update(
            status="unknown", reason="Successful job is not yet proven to contain the merged fix.",
            missingEvidence=list({source["url"]: source for source in missing}.values()),
        )
    return result


def _occurrence_subject(
    snapshot: Mapping[str, Any], occurrence: Mapping[str, Any], default_branch: str,
) -> dict[str, Any] | None:
    run = snapshot["evidence"].get(f"run:{occurrence['runId']}", {}).get("payload", {})
    subject = {
        "defaultBranch": default_branch, "workflowId": run.get("workflowId"),
        "workflowPath": run.get("workflowPath"), "event": run.get("event"),
        "workflow": occurrence.get("workflow"), "job": occurrence.get("jobName"),
        "lane": occurrence.get("lane"), "os": occurrence.get("os"),
        "laneId": occurrence.get("laneId"),
        "testName": occurrence.get("testName"),
    }
    if type(subject["workflowId"]) is not int or not all(
        subject[key] for key in ("workflow", "workflowPath", "event", "job", "lane", "os")
    ):
        return None
    return subject


def build_related_workflow_repairs(
    snapshot: Mapping[str, Any], observations: Mapping[str, Any], issue: Mapping[str, Any],
    health: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if health is None:
        return _related_observed_repairs(snapshot, observations, issue)
    signatures: dict[tuple[Any, ...], set[str]] = {}

    def signature(subject: Mapping[str, Any], occurrence: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            subject["defaultBranch"], subject["workflowId"], subject["workflowPath"], subject["event"],
            subject["job"], subject["lane"], subject["laneId"],
            subject["os"], subject["testName"], occurrence["fingerprintId"],
        )

    for occurrence in observations.get("occurrences", []):
        if occurrence.get("issueNumber") != issue["issueNumber"]:
            continue
        subject = _occurrence_subject(snapshot, occurrence, health["defaultBranch"])
        if subject is None:
            continue
        execution = _execution(snapshot, occurrence, subject)
        if execution is not None and execution["conclusion"] in {"failure", "timed_out"}:
            signatures.setdefault(signature(subject, occurrence), set()).update(occurrence["evidenceIds"])
    if not signatures:
        return []
    by_issue: dict[int, list[Mapping[str, Any]]] = {}
    for record in snapshot.get("delegationStatus", {}).get("records", []):
        if record.get("repository") == snapshot["repository"] and record.get("issueNumber") != issue["issueNumber"]:
            by_issue.setdefault(record["issueNumber"], []).append(record)
    related = []
    for number, records in sorted(by_issue.items()):
        proof = set()
        for record in records:
            started_at = parse_aware_iso8601(record["startedAt"], "delegation.startedAt")
            for occurrence in observations.get("occurrences", []):
                if occurrence.get("issueNumber") != number:
                    continue
                subject = _occurrence_subject(snapshot, occurrence, health["defaultBranch"])
                if subject is None:
                    continue
                execution = _execution(snapshot, occurrence, subject)
                if (
                    execution is not None and execution["conclusion"] in {"failure", "timed_out"}
                    and execution["completedAt"] <= started_at
                    and signature(subject, occurrence) in signatures
                ):
                    proof.update(occurrence["evidenceIds"])
                    proof.update(signatures[signature(subject, occurrence)])
        if proof:
            related.append({
                "issueNumber": number,
                "issueUrl": f"https://github.com/{snapshot['repository']}/issues/{number}",
                "replacementReady": delegation_replacement_ready(records),
                "sameRootCause": "unknown",
                "records": copy.deepcopy(sorted(records, key=lambda record: (record["startedAt"], record["actionId"]))),
                "evidenceIds": sorted(proof),
            })
    return related


def _related_observed_repairs(
    snapshot: Mapping[str, Any], observations: Mapping[str, Any], issue: Mapping[str, Any],
) -> list[dict[str, Any]]:
    repair = issue.get("repairEvidence", {})
    if not repair.get("subjectKey"):
        return []
    by_issue: dict[int, list[Mapping[str, Any]]] = {}
    for record in snapshot.get("delegationStatus", {}).get("records", []):
        if record.get("repository") == snapshot["repository"] and record["issueNumber"] != issue["issueNumber"]:
            by_issue.setdefault(record["issueNumber"], []).append(record)
    related = []
    for number, records in sorted(by_issue.items()):
        owned = build_repair_evidence(snapshot, observations, number)
        if (
            owned.get("subjectKey") != repair["subjectKey"] or not owned.get("lastFailureAt")
            or not any(
                parse_aware_iso8601(owned["lastFailureAt"], "lastFailureAt")
                <= parse_aware_iso8601(record["startedAt"], "startedAt") for record in records
            )
        ):
            continue
        related.append({
            "issueNumber": number, "issueUrl": f"https://github.com/{snapshot['repository']}/issues/{number}",
            "replacementReady": delegation_replacement_ready(records), "sameRootCause": "unknown",
            "records": copy.deepcopy(records),
            "evidenceIds": sorted(set(repair["evidenceIds"]) | set(owned["evidenceIds"])),
        })
    return related


def build_upstream_repairs(
    snapshot: Mapping[str, Any], observations: Mapping[str, Any], issue: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """An explicit artifact-source execution can identify an existing cause owner."""
    repository = snapshot["repository"]
    evidence = snapshot["evidence"]
    related: dict[int, dict[str, Any]] = {}
    questions: set[str] = set()
    for occurrence in observations.get("occurrences", []):
        if occurrence["issueNumber"] != issue["issueNumber"]:
            continue
        for evidence_id in occurrence["evidenceIds"]:
            record = evidence.get(evidence_id, {})
            payload = record.get("payload", {})
            text = payload.get("excerpt", "")
            if (
                record.get("kind") != "workflow-log" or record.get("availability") != "available"
                or payload.get("truncated") is True or not isinstance(text, str)
                or re.search(r"(?i)\b(?:failed|unable)\b[^\n]{0,120}\bdownload\b[^\n]{0,120}\bartifact", text) is None
            ):
                continue
            # Bind URLs on the failure line itself, for example:
            # "Unable to download artifact packages from https://github.com/o/r/actions/runs/80".
            # A diagnostic elsewhere mentioning an unrelated run is not a source.
            producer_text = "\n".join(
                match["url"] for line in text.splitlines()
                for match in re.finditer(
                    r"(?i)\b(?:failed|unable)\b.{0,120}\bdownload\b.{0,120}\bartifact\b"
                    r".{0,120}\bfrom\s+(?P<url>https://github\.com/[^/\s]+/[^/\s]+/actions/runs/[1-9][0-9]*)",
                    line,
                )
            )
            if not producer_text:
                continue
            signals = extract_issue_signals(
                issue["issueNumber"], evidence_id, str(record.get("url", "")), producer_text, repository,
            )
            for reference in signals.references:
                if (
                    reference.get("targetType") != "workflow-run"
                    or reference.get("targetRepository") != repository
                    or reference["runId"] == occurrence["runId"]
                ):
                    continue
                producer_id = f"run:{reference['runId']}"
                producer = evidence.get(producer_id, {})
                run = producer.get("payload", {})
                owners = [
                    owner for owner in snapshot.get("delegationStatus", {}).get("records", [])
                    if owner.get("repository") == repository and owner.get("issueNumber") != issue["issueNumber"]
                    and any(ref.get("sourceIssueNumber") == owner["issueNumber"] for ref in run.get("referencedBy", []))
                    and (
                        owner.get("taskObservation") == "available" and owner.get("taskState") in {"queued", "in_progress"}
                        or any(pull.get("state") in {"open", "unknown"} for pull in owner.get("pullRequests", []))
                    )
                ]
                if (
                    producer.get("availability") != "available"
                    or run.get("targetRepository") != repository or run.get("status") != "completed"
                    or run.get("conclusion") != "failure" or type(run.get("workflowId")) is not int
                    or not run.get("headSha") or not run.get("workflowPath") or not owners
                ):
                    questions.add("Verify whether the explicitly linked artifact producer failed and already has a repair owner.")
                    continue
                for owner in owners:
                    number = owner["issueNumber"]
                    if not run.get("updatedAt") or parse_aware_iso8601(run["updatedAt"], "producer.updatedAt") > parse_aware_iso8601(owner["startedAt"], "startedAt"):
                        questions.add("Verify that the producer failure predates the linked repair attempt.")
                        continue
                    related[number] = {
                        "issueNumber": number, "issueUrl": f"https://github.com/{repository}/issues/{number}",
                        "replacementReady": False, "sameRootCause": "upstream-artifact-producer",
                        "records": [copy.deepcopy(owner)], "evidenceIds": sorted({evidence_id, producer_id}),
                    }
    return list(related.values()), sorted(questions)


def _repair_subject(
    snapshot: Mapping[str, Any], observations: Mapping[str, Any], issue: Mapping[str, Any],
    record: Mapping[str, Any], health: Mapping[str, Any],
) -> dict[str, Any] | None:
    subjects = {}
    started_at = parse_aware_iso8601(record["startedAt"], "delegation.startedAt")
    # Current issue comments can introduce another failing job after assignment.
    # Bind the repair to pre-assignment execution evidence, not the latest subject.
    for occurrence in observations.get("occurrences", []):
        if occurrence.get("issueNumber") != issue["issueNumber"]:
            continue
        subject = _occurrence_subject(snapshot, occurrence, health["defaultBranch"])
        if subject is None:
            continue
        execution = _execution(snapshot, occurrence, subject)
        if (
            execution is not None and execution["completedAt"] <= started_at
            and execution["conclusion"] in {"failure", "timed_out"}
        ):
            subjects[tuple(subject.values())] = subject
    return next(iter(subjects.values())) if len(subjects) == 1 else None


def _fix_containment(
    snapshot: Mapping[str, Any], base_sha: str, head_sha: str,
) -> tuple[bool | None, Mapping[str, Any] | None]:
    if base_sha == head_sha:
        return True, None
    url = f"https://api.github.com/repos/{snapshot['repository']}/compare/{base_sha}...{head_sha}"
    missing = {"url": url, "baseSha": base_sha, "headSha": head_sha, "availability": "unknown"}
    matches = [
        comparison for comparison in snapshot.get("commitComparisons", [])
        if comparison.get("repository") == snapshot["repository"]
        and comparison.get("baseSha") == base_sha and comparison.get("headSha") == head_sha
    ]
    if len(matches) != 1:
        return None, missing
    comparison = matches[0]
    if comparison.get("availability") == "unavailable":
        missing["availability"] = "unavailable"
    if (
        comparison.get("availability") != "available"
        or comparison.get("url") != url or comparison.get("baseCommitSha") != base_sha
        or not _commit_sha(comparison.get("mergeBaseSha"))
        or type(comparison.get("behindBy")) is not int or comparison["behindBy"] < 0
    ):
        return None, missing
    if comparison["behindBy"] > 0 and (
        comparison.get("status") == "behind" and comparison["mergeBaseSha"] == head_sha
        or comparison.get("status") == "diverged" and comparison["mergeBaseSha"] not in {base_sha, head_sha}
    ):
        return False, comparison
    if (
        comparison.get("status") == "ahead" and comparison["behindBy"] == 0
        and comparison["mergeBaseSha"] == base_sha
    ):
        return True, comparison
    return None, missing


def _execution(
    snapshot: Mapping[str, Any], sample: Mapping[str, Any], subject: Mapping[str, Any],
) -> dict[str, Any] | None:
    if any(sample.get(key) != subject.get(target) for key, target in (
        ("workflow", "workflow"), ("jobName", "job"), ("lane", "lane"), ("os", "os"), ("laneId", "laneId"),
    )) or (
        subject.get("testName") is not None and sample.get("testName") != subject["testName"]
    ) or sample.get("scopeConflict"):
        return None
    run_id, attempt, job_id = (sample.get(key) for key in ("runId", "attempt", "jobId"))
    run_key = f"run:{run_id}"
    job_key = f"run:{run_id}:attempt:{attempt}:job:{job_id}"
    run_record = snapshot["evidence"].get(run_key, {})
    job_record = snapshot["evidence"].get(job_key, {})
    if (
        run_record.get("availability") != "available" or run_record.get("kind") != "workflow-run"
        or job_record.get("availability") != "available" or job_record.get("kind") != "workflow-job"
        or not {run_key, job_key}.issubset(sample.get("evidenceIds", []))
    ):
        return None
    run, job = run_record["payload"], job_record["payload"]
    if (
        run.get("targetRepository") != snapshot["repository"]
        or job.get("targetRepository") != snapshot["repository"]
        or run.get("runId") != run_id or job.get("runId") != run_id
        or job.get("jobId") != job_id or job.get("attempt") != attempt
        or run.get("workflowId") != subject["workflowId"]
        or run.get("workflowPath") != subject["workflowPath"] or run.get("event") != subject["event"]
        or job.get("name") != subject["job"]
        or not run.get("event") or run["event"] in {"pull_request", "pull_request_target", "merge_group"}
        or (run.get("headBranch") or run.get("branch")) != subject["defaultBranch"]
        or run.get("status") != "completed" or job.get("status") != "completed"
        or not _commit_sha(run.get("headSha")) or sample.get("headSha") != run["headSha"]
        or not job.get("startedAt") or not job.get("completedAt")
    ):
        return None
    started_at = parse_aware_iso8601(job["startedAt"], "job.startedAt")
    completed_at = parse_aware_iso8601(job["completedAt"], "job.completedAt")
    if not started_at <= completed_at <= parse_aware_iso8601(snapshot["collectedAt"], "collectedAt"):
        return None
    return {
        "runId": run_id, "attempt": attempt, "jobId": job_id, "headSha": run["headSha"],
        "startedAt": started_at, "completedAt": completed_at, "conclusion": job.get("conclusion"),
        "evidenceIds": sorted(sample["evidenceIds"]),
    }


def _commit_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None
