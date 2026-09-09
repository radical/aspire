from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from .observations import matches_issue_job_scope
from .timeutils import parse_aware_iso8601


RECENT_WORKFLOW_FAILURE_DAYS = 14
TRANSIENT_INCIDENT_RETENTION_DAYS = 30
_PR_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})


def build_workflow_health(
    snapshot: Mapping[str, Any],
    observations: Mapping[str, Any],
    issue: Mapping[str, Any],
) -> dict[str, Any] | None:
    # New discovery activates this policy. Reinterpreting legacy frozen inputs
    # would change the meaning of already-reviewed or authorized decisions.
    discovery = snapshot.get("workflowDiscovery")
    if not isinstance(discovery, Mapping) or observations.get("error"):
        return None
    default_branch = discovery.get("defaultBranch")
    if "testMaintenance" in issue:
        return None
    if discovery.get("defaultBranchVerified") is not True or not isinstance(default_branch, str) or not default_branch:
        if not any(
            occurrence["issueNumber"] == issue["issueNumber"]
            and snapshot["evidence"].get(f"run:{occurrence['runId']}", {}).get("payload", {}).get("event") not in _PR_EVENTS
            for occurrence in observations["occurrences"]
        ):
            return None
        return {
            "defaultBranch": None, "current": False, "category": "unknown", "route": "watch",
            "closureAllowed": False, "coverageComplete": False,
            "coverageGaps": [{"code": "default-branch-unverified"}],
            "samples": [], "sampleRunIds": [], "recurrent": False, "evidenceIds": [],
        }
    collected_at = parse_aware_iso8601(snapshot["collectedAt"], "collectedAt")
    evidence = snapshot["evidence"]
    failures = []
    for occurrence in observations["occurrences"]:
        if (
            occurrence["issueNumber"] != issue["issueNumber"]
            or occurrence.get("scopeConflict")
            or not matches_issue_job_scope(snapshot, occurrence, issue["issueNumber"])
        ):
            continue
        run = evidence.get(f"run:{occurrence['runId']}", {}).get("payload", {})
        if (
            run.get("targetRepository") != snapshot["repository"]
            or run.get("event") in _PR_EVENTS
            or not run.get("event")
            or (run.get("headBranch") or run.get("branch")) != default_branch
            or not run.get("headSha")
            or type(run.get("workflowId")) is not int
            or not all(occurrence.get(key) for key in ("jobName", "lane", "os", "observedAt"))
        ):
            continue
        observed_at = parse_aware_iso8601(occurrence["observedAt"], "observedAt")
        if observed_at <= collected_at:
            failures.append(occurrence)
    if not failures:
        return None
    groups: dict[tuple[object, ...], list[Mapping[str, Any]]] = {}
    for failure in failures:
        key = (
            evidence[f"run:{failure['runId']}"]["payload"]["workflowId"],
            evidence[f"run:{failure['runId']}"]["payload"].get("workflowPath"),
            evidence[f"run:{failure['runId']}"]["payload"].get("event"),
            failure["fingerprintId"], failure["jobName"], failure["lane"], failure["os"], failure.get("laneId"),
        )
        groups.setdefault(key, []).append(failure)
    subjects = [
        _build_subject_health(snapshot, observations, issue, default_branch, matching)
        for matching in groups.values()
    ]
    # A newer isolated incident must not hide another workflow needing repair.
    # Closing a shared tracker still requires every subject to satisfy its policy.
    health = max(subjects, key=lambda subject: (
        subject["route"] == "delegate-copilot",
        subject["current"],
        not subject["closureAllowed"],
        parse_aware_iso8601(subject["lastFailureAt"], "lastFailureAt"),
    ))
    health["closureAllowed"] = all(subject["closureAllowed"] for subject in subjects)
    return health


def _build_subject_health(
    snapshot: Mapping[str, Any],
    observations: Mapping[str, Any],
    issue: Mapping[str, Any],
    default_branch: str,
    matching: list[Mapping[str, Any]],
) -> dict[str, Any]:
    collected_at = parse_aware_iso8601(snapshot["collectedAt"], "collectedAt")
    cutoff = collected_at - timedelta(days=RECENT_WORKFLOW_FAILURE_DAYS)
    evidence = snapshot["evidence"]
    latest = max(matching, key=lambda item: parse_aware_iso8601(item["observedAt"], "observedAt"))
    source_run = evidence[f"run:{latest['runId']}"]["payload"]
    workflow_id = source_run["workflowId"]
    discovery = snapshot["workflowDiscovery"]
    windows = [
        window for window in discovery.get("workflows", [])
        if (window["workflowId"], window["workflowPath"], window["event"])
        == (workflow_id, source_run.get("workflowPath"), source_run.get("event"))
    ]
    window = windows[0] if len(windows) == 1 else {}
    coverage_complete = (
        discovery.get("defaultBranchVerified") is True and window.get("windowComplete") is True
        and not window.get("gaps")
    )
    window_run_ids = window.get("runIds", [])
    run_catalog = {
        record["payload"]["runId"]: record["payload"]
        for record in evidence.values()
        if record.get("kind") == "workflow-run" and record.get("availability") == "available"
    }
    # A complete run can omit the affected job. It is still a sample (unknown),
    # not permission to make failures on either side appear consecutive.
    run_catalog.update({run["runId"]: run for run in discovery.get("runs", [])})
    runs = [
        run for run in run_catalog.values()
        if run.get("workflowId") == workflow_id
        and run.get("workflowPath") == source_run.get("workflowPath")
        and run.get("event") == source_run.get("event")
        and run.get("runId") in window_run_ids
        and run.get("targetRepository") == snapshot["repository"]
        and run.get("event") not in _PR_EVENTS and run.get("event")
        and (run.get("headBranch") or run.get("branch")) == default_branch
        and run.get("status") == "completed"
        and parse_aware_iso8601(run["createdAt"], "run.createdAt") <= collected_at
    ]
    runs.sort(key=lambda run: (
        parse_aware_iso8601(run["createdAt"], "run.createdAt"), run["runId"],
    ), reverse=True)
    samples = []
    for run in runs[:5]:
        failed = any(failure["runId"] == run["runId"] for failure in matching)
        passed = any(
            coverage["runId"] == run["runId"]
            and coverage.get("independentRecoveryEligible") is True
            and all(coverage.get(key) == latest.get(key) for key in ("jobName", "lane", "os", "testName", "laneId"))
            for coverage in observations["coverage"]
        )
        samples.append({
            "runId": run["runId"],
            "outcome": "failure" if failed else "success" if passed else "unknown",
        })
    outcomes = [sample["outcome"] for sample in samples]
    recurrent = coverage_complete and (outcomes[:2] == ["failure", "failure"] or outcomes.count("failure") >= 3)
    current = (
        parse_aware_iso8601(latest["observedAt"], "lastFailureAt") >= cutoff
        and (
            bool(samples) and outcomes[0] == "failure"
            or recurrent and ("infra-transient" in latest["allowedCauses"] or latest.get("testName"))
            or not coverage_complete
        )
    )
    current = bool(current)
    category = (
        "blocking-build" if "toolchain-build-break" in latest["allowedCauses"]
        else "transient-infrastructure" if "infra-transient" in latest["allowedCauses"]
        else "flaky-test" if latest.get("testName")
        else "product-or-tooling" if latest["fingerprintId"].startswith("diagnostic:")
        else "unknown"
    )
    route = (
        "delegate-copilot"
        if current and (category == "blocking-build" or recurrent and category != "unknown")
        else "investigate" if current and category == "unknown"
        else "watch"
    )
    closure_allowed = (
        coverage_complete
        and issue.get("recovery", {}).get("status") == "verified"
        and not current
        and bool(issue["recovery"].get("subjects"))
        and all(
            subject.get("coverage") is not None
            and subject["coverage"].get("independentRecoveryEligible") is True
            and subject["coverage"]["runId"] != subject["occurrence"]["runId"]
            and subject["coverage"].get("verifiedScope", {}).get("event") not in _PR_EVENTS
            and subject["coverage"].get("verifiedScope", {}).get("ref") == default_branch
            and type(evidence[f"run:{subject['occurrence']['runId']}"]["payload"].get("workflowId")) is int
            and evidence[f"run:{subject['coverage']['runId']}"]["payload"].get("workflowId")
            == evidence[f"run:{subject['occurrence']['runId']}"]["payload"]["workflowId"]
            and all(
                evidence[f"run:{subject['coverage']['runId']}"]["payload"].get(key)
                == evidence[f"run:{subject['occurrence']['runId']}"]["payload"].get(key)
                for key in ("event", "workflowPath")
            )
            and any(
                candidate.get("windowComplete") is True
                and subject["coverage"]["runId"] in candidate["runIds"]
                and (
                    candidate["workflowId"], candidate["workflowPath"], candidate["event"]
                ) == tuple(
                    evidence[f"run:{subject['coverage']['runId']}"]["payload"].get(key)
                    for key in ("workflowId", "workflowPath", "event")
                )
                for candidate in discovery.get("workflows", [])
            )
            for subject in issue["recovery"]["subjects"]
        )
        and (
            issue.get("delegationContext") is None
            or issue.get("repairFollowup", {}).get("status") == "verified"
            and not any(
                record.get("taskObservation") == "available"
                and record.get("taskState") in {"queued", "in_progress"}
                or any(pull.get("state") in {"open", "unknown"} for pull in record.get("pullRequests", []))
                for record in issue["delegationContext"].get("records", [])
            )
        )
        and (
            category != "transient-infrastructure"
            or collected_at - parse_aware_iso8601(latest["observedAt"], "lastFailureAt")
            >= timedelta(days=TRANSIENT_INCIDENT_RETENTION_DAYS)
        )
    )
    proof_samples = [sample for sample in samples if sample["outcome"] == "failure"]
    if route == "delegate-copilot":
        # Keep complete citations for the threshold's witnesses, rather than
        # dropping required diagnostics to fit every redundant occurrence.
        proof_samples = (
            samples[:2] if outcomes[:2] == ["failure", "failure"]
            else proof_samples[:3] if recurrent
            else proof_samples[:1]
        )
    proof_run_ids = {sample["runId"] for sample in proof_samples}
    if not proof_run_ids:
        proof_run_ids.add(latest["runId"])
    # Retried failures still represent one independent execution. Preserve one
    # whole, most-recent matching occurrence instead of accumulating its retries.
    proof_failures = [
        max(
            (failure for failure in matching if failure["runId"] == run_id),
            key=lambda failure: (
                parse_aware_iso8601(failure["observedAt"], "observedAt"),
                failure.get("attempt") or 0,
            ),
        )
        for run_id in proof_run_ids
    ]
    return {
        "defaultBranch": default_branch,
        "workflowId": workflow_id,
        "workflowPath": source_run.get("workflowPath"),
        "event": source_run.get("event"),
        "coverageComplete": coverage_complete,
        "coverageGaps": list(window.get("gaps", [])) if window else [{"code": "workflow-window-unavailable"}],
        "workflow": latest["workflow"],
        "job": latest["jobName"],
        "lane": latest["lane"],
        "laneId": latest.get("laneId"),
        "os": latest["os"],
        "failureFingerprint": latest["fingerprintId"],
        "current": current,
        "category": category,
        "route": route,
        "closureAllowed": closure_allowed,
        "lastFailureAt": latest["observedAt"],
        "recurrent": recurrent,
        "sampleRunIds": [sample["runId"] for sample in samples],
        "samples": samples,
        "evidenceIds": sorted({
            f"issue:{issue['issueNumber']}",
            *(evidence_id for failure in proof_failures
              for evidence_id in failure["evidenceIds"]),
        }),
    }
