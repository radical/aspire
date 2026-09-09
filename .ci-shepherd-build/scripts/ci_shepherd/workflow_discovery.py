"""Bounded observations, not authorization or a workflow-health verdict."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import urlencode

from .github import GitHubApiError, GitHubTextResponse


BOUNDS = {
    "lookbackDays": 7,
    "recentPages": 2,
    "recentPageSize": 25,
    "workflowWindows": 8,
    "runsPerWindow": 5,
    "historyPages": 2,
    "historyPageSize": 5,
    "historyLookbackDays": 90,
    "jobPages": 8,
    "jobPageSize": 50,
    "jobs": 2_500,
    "requests": 128,
    "responseBytes": 512_000,
    "totalResponseBodyBytes": 8_000_000,
    "sourceRuns": 8,
    "sourceRequests": 40,
    "issueAssociations": 40,
    "logs": 12,
    "logBytes": 200_000,
}
_PR_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    document: dict
    logs: dict[str, dict]


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


class _Reader:
    def __init__(self, client: Any, document: dict) -> None:
        self.client = client
        self.document = document

    def gap(self, code: str, context: dict, message: str, endpoint: str = "") -> None:
        self.document["gaps"].append({
            "kind": "coverage", **context, "code": code, "message": message, "endpoint": endpoint,
        })

    def text(self, endpoint: str, context: dict, max_bytes: int) -> GitHubTextResponse | None:
        usage = self.document["usage"]
        remaining = BOUNDS["totalResponseBodyBytes"] - usage["responseBodyBytes"]
        if usage["requests"] >= BOUNDS["requests"] or remaining <= 0:
            self.gap("request-or-byte-budget", context, "Discovery request or response-body budget exhausted.", endpoint)
            return None
        if not callable(getattr(self.client, "get_text", None)):
            self.gap("bounded-client-unavailable", context, "Discovery requires bounded get_text support.", endpoint)
            return None
        usage["requests"] += 1
        try:
            response = self.client.get_text(endpoint, max_bytes=min(max_bytes, BOUNDS["responseBytes"], remaining))
            usage["responseBodyBytes"] += len(response.text.encode("utf-8"))
        except (GitHubApiError, UnicodeError) as exc:
            self.gap("read-failed", context, str(exc), endpoint)
            return None
        if response.status != 200:
            self.gap("unexpected-http-status", context, f"Expected HTTP 200, received {response.status}.", endpoint)
            return None
        return response

    def get(self, endpoint: str, context: dict) -> dict | None:
        response = self.text(endpoint, context, BOUNDS["responseBytes"])
        if response is None:
            return None
        if response.truncated:
            self.gap("response-truncated", context, "Response exceeded its byte limit.", endpoint)
            return None
        try:
            result = json.loads(response.text)
        except ValueError as exc:
            self.gap("invalid-json", context, str(exc), endpoint)
            return None
        if not isinstance(result, dict):
            self.gap("invalid-response", context, "Expected a JSON object.", endpoint)
            return None
        return result

    def page(self, endpoint: str, key: str, context: dict, max_items: int) -> tuple[list, int] | None:
        response = self.get(endpoint, context)
        if response is None:
            return None
        rows, total = response.get(key), response.get("total_count")
        if (
            not isinstance(rows, list) or not isinstance(total, int)
            or isinstance(total, bool) or total < len(rows) or len(rows) > max_items
        ):
            self.gap("invalid-page", context, "Missing or inconsistent items/total_count.", endpoint)
            return None
        return rows, total


def _collect_jobs(reader: _Reader, item: dict, repository: str, normalize_job: Callable, context: dict) -> None:
    document = reader.document
    context = {**context, "attempt": item["attempt"]}
    gap_start = len(document["gaps"])
    seen_jobs = 0
    valid_jobs = True
    job_ids = set()
    lanes = set()
    # Attempts are executions of the same run, never independent recurrence samples.
    # https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt
    page_size = min(BOUNDS["jobPageSize"], BOUNDS["jobs"] - document["usage"]["jobs"])
    for page in range(1, BOUNDS["jobPages"] + 1):
        # Changing per_page between pages changes the offset and duplicates jobs.
        if page_size <= 0 or BOUNDS["jobs"] - document["usage"]["jobs"] < page_size:
            reader.gap("job-budget", context, "Global job observation budget exhausted.")
            break
        endpoint = f"/repos/{repository}/actions/runs/{item['runId']}/attempts/{item['attempt']}/jobs?" + urlencode({
            "per_page": page_size, "page": page,
        })
        result = reader.page(endpoint, "jobs", context, page_size)
        if result is None:
            break
        rows, total = result
        seen_jobs += len(rows)
        for raw_job in rows:
            document["usage"]["jobs"] += 1
            if (
                not isinstance(raw_job, dict) or not _positive(raw_job.get("id"))
                or not _positive(raw_job.get("run_id"))
                or raw_job.get("run_id") != item["runId"]
                or not _positive(raw_job.get("run_attempt", item["attempt"]))
                or raw_job.get("run_attempt", item["attempt"]) != item["attempt"]
                or raw_job.get("head_sha") != item["headSha"]
                or raw_job.get("head_branch", item["branch"]) != item["branch"]
            ):
                valid_jobs = False
                reader.gap("job-identity-mismatch", context, "Job does not identify the requested run, attempt and head.")
                continue
            expected_url = f"https://github.com/{repository}/actions/runs/{item['runId']}/job/{raw_job['id']}"
            if (
                not isinstance(raw_job.get("name"), str) or not raw_job["name"]
                or not isinstance(raw_job.get("status"), str) or not raw_job["status"]
                or not isinstance(raw_job.get("steps", []), list)
                or raw_job.get("html_url") not in (None, "") and (
                    not isinstance(raw_job["html_url"], str) or raw_job["html_url"].casefold() != expected_url.casefold()
                )
            ):
                valid_jobs = False
                reader.gap("job-metadata-unverified", context, "Job name, state, steps or URL is malformed.")
                continue
            if raw_job["id"] in job_ids:
                valid_jobs = False
                reader.gap("duplicate-job", context, "Repeated job ID across the job inventory.")
                continue
            job_ids.add(raw_job["id"])
            normalized = normalize_job(raw_job, default_attempt=item["attempt"], target_repository=repository)
            if normalized is not None:
                normalized["url"] = expected_url
                if normalized["conclusion"] == "success":
                    started = _timestamp(normalized["startedAt"])
                    completed = _timestamp(normalized["completedAt"])
                    if normalized["status"] != "completed" or started is None or completed is None or completed < started:
                        valid_jobs = False
                        reader.gap("job-execution-unverified", context, "Success lacks a completed, timestamped job execution.")
                labels = raw_job.get("labels")
                if not isinstance(labels, list) or any(not isinstance(label, str) or not label for label in labels):
                    valid_jobs = False
                    reader.gap("job-lane-unverified", context, "Runner labels are missing or malformed.")
                    continue
                identity = {
                    "repository": repository.casefold(), "workflowId": item["workflowId"],
                    "workflowPath": item["workflowPath"], "branch": item["branch"],
                    "event": item["event"], "jobName": normalized["name"], "runnerLabels": sorted(set(labels)),
                }
                normalized.update({
                    "laneIdentity": identity,
                    "laneId": json.dumps(identity, sort_keys=True, separators=(",", ":")),
                    "lane": normalized["name"], "runnerLabels": identity["runnerLabels"],
                })
                if normalized["laneId"] in lanes:
                    valid_jobs = False
                    reader.gap("ambiguous-job-lane", context, "Multiple jobs have the same verified lane identity.")
                lanes.add(normalized["laneId"])
                item["jobs"].append(normalized)
        if seen_jobs >= total:
            item["jobsComplete"] = valid_jobs
            break
    if not item["jobsComplete"]:
        reader.gap("job-inventory-incomplete", context, "Job pages were bounded or incomplete.")
    item["gaps"].extend(document["gaps"][gap_start:])


def discover_workflows(
    client: Any, repository: str, now: datetime, *, normalize_job: Callable[..., dict | None],
    source_requests: list[dict],
    extract_log_facts: Callable[[str, str], list[dict]],
    existing_evidence: dict,
) -> DiscoveryResult:
    document = {
        "schemaVersion": 1, "repository": repository,
        "collectedAt": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "defaultBranch": None, "defaultBranchVerified": False, "status": "unavailable",
        "repositoryId": None,
        "bounds": dict(BOUNDS), "usage": {"requests": 0, "responseBodyBytes": 0, "jobs": 0, "sourceRuns": 0, "logs": 0},
        "recentScanComplete": False, "workflows": [], "runs": [], "issueAssociations": [],
        "sourceRuns": [], "diagnostics": [], "gaps": [], "excludedRuns": [],
    }
    reader = _Reader(client, document)
    root = f"/repos/{repository}"
    repo = reader.get(root, {"scope": "repository"})
    if repo is None:
        return DiscoveryResult(document, {})
    if (
        not _positive(repo.get("id")) or str(repo.get("full_name", "")).casefold() != repository.casefold()
        or type(repo.get("fork")) is not bool or not isinstance(repo.get("default_branch"), str)
        or not repo["default_branch"].strip()
    ):
        reader.gap("repository-unverified", {"scope": "repository"}, "Repository identity/default branch is unverified.", root)
        return DiscoveryResult(document, {})
    branch = document["defaultBranch"] = repo["default_branch"]
    document["repositoryId"] = repo["id"]
    document["defaultBranchVerified"] = True

    def admit(
        raw: object, context: dict, created_range: tuple[datetime, datetime] | None = None,
    ) -> dict | None:
        if not isinstance(raw, dict):
            reader.gap("invalid-run", context, "Run is not an object.")
            return None
        reason = ""
        created = _timestamp(raw.get("created_at"))
        if created_range is not None and created is not None and not created_range[0] <= created <= created_range[1]:
            reason = "outside-query-window"
        elif not isinstance(raw.get("event"), str) or not raw["event"]:
            reason = "incomplete-run-identity"
        elif raw["event"] in _PR_EVENTS:
            reason = "pr-event"
        elif raw.get("pull_requests") != []:
            reason = "pr-associated-or-unknown"
        elif raw.get("head_branch") != branch:
            reason = "non-default-or-unknown-branch"
        elif any(
            not isinstance(raw.get(field), dict)
            or not _positive(raw[field].get("id"))
            or raw[field].get("id") != repo["id"]
            or str(raw[field].get("full_name", "")).casefold() != repository.casefold()
            or raw[field].get("fork") is not repo["fork"]
            for field in ("repository", "head_repository")
        ):
            reason = "foreign-fork-or-unknown-repository"
        elif (
            not all(_positive(raw.get(field)) for field in ("id", "workflow_id", "run_attempt", "run_number"))
            or not isinstance(raw.get("path"), str) or not raw["path"]
            or _timestamp(raw.get("created_at")) is None
            or not isinstance(raw.get("head_sha"), str) or re.fullmatch(r"[0-9a-f]{40}", raw["head_sha"]) is None
            or not isinstance(raw.get("status"), str) or not raw["status"]
            or raw["status"] == "completed" and (not isinstance(raw.get("conclusion"), str) or not raw["conclusion"])
        ):
            reason = "incomplete-run-identity"
        if reason:
            excluded = {
                "runId": raw["id"] if _positive(raw.get("id")) else None,
                "workflowId": raw["workflow_id"] if _positive(raw.get("workflow_id")) else None,
                "event": raw["event"] if isinstance(raw.get("event"), str) else None,
                "branch": raw["head_branch"] if isinstance(raw.get("head_branch"), str) else None,
                "reason": reason,
            }
            document["excludedRuns"].append(excluded)
            if (
                not isinstance(raw.get("head_branch"), str) or not raw["head_branch"]
                or not isinstance(raw.get("pull_requests"), list)
                or any(
                    not isinstance(raw.get(field), dict)
                    or not _positive(raw[field].get("id"))
                    or not isinstance(raw[field].get("full_name"), str) or not raw[field]["full_name"]
                    or type(raw[field].get("fork")) is not bool
                    for field in ("repository", "head_repository")
                )
                or reason in {"incomplete-run-identity", "outside-query-window"}
            ):
                reader.gap(
                    "unverified-run-scope",
                    {**{key: excluded[key] for key in ("workflowId", "event", "runId")}, **context},
                    reason,
                )
            return None
        return {
            "runId": raw["id"], "workflowId": raw["workflow_id"], "workflowPath": raw["path"],
            "workflow": raw.get("name"), "runNumber": raw["run_number"], "attempt": raw["run_attempt"],
            "event": raw["event"], "branch": branch, "headSha": raw["head_sha"],
            "targetRepository": repository, "status": raw.get("status"), "conclusion": raw.get("conclusion"),
            "repositoryId": repo["id"], "headRepositoryId": repo["id"],
            "headRepository": repository, "headRepositoryFork": repo["fork"],
            "createdAt": raw["created_at"], "updatedAt": raw.get("updated_at"),
            "runStartedAt": raw.get("run_started_at"), "subjectPullRequests": [],
            "jobs": [], "jobsComplete": False, "gaps": [],
        }

    recent = []
    seen = 0
    cutoff = (now - timedelta(days=BOUNDS["lookbackDays"])).astimezone(UTC).isoformat().replace("+00:00", "Z")
    # Do not send exclude_pull_requests: it removes PR metadata, not PR-triggered runs.
    # https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
    for page in range(1, BOUNDS["recentPages"] + 1):
        endpoint = root + "/actions/runs?" + urlencode({
            "branch": branch, "created": f"{cutoff}..{document['collectedAt']}",
            "per_page": BOUNDS["recentPageSize"], "page": page,
        })
        result = reader.page(endpoint, "workflow_runs", {"scope": "repository"}, BOUNDS["recentPageSize"])
        if result is None:
            break
        rows, total = result
        seen += len(rows)
        recent.extend(item for row in rows if (item := admit(
            row, {"scope": "repository"}, (now - timedelta(days=BOUNDS["lookbackDays"]), now),
        )) is not None)
        if seen >= total:
            document["recentScanComplete"] = True
            break
    if not document["recentScanComplete"]:
        reader.gap("recent-window-incomplete", {"scope": "repository"}, "Recent run inventory is bounded or incomplete.")

    keys = {}
    for item in sorted(recent, key=lambda row: (row["conclusion"] == "failure", row["createdAt"]), reverse=True):
        keys.setdefault((item["workflowId"], item["workflowPath"], item["event"]), item)
    recent_keys = set(keys)
    anchors = {}
    source_requests = sorted(source_requests, key=lambda request: (
        request["testTracker"], request["sourceRunId"] not in {item["runId"] for item in recent},
        -request["sourceRunId"], request["issueNumber"],
    ))
    if len(source_requests) > BOUNDS["sourceRequests"]:
        reader.gap("source-request-budget", {"scope": "repository"}, "Additional issue associations were not probed.")
    source_requests = source_requests[:BOUNDS["sourceRequests"]]
    for request in source_requests:
        key = (request["sourceRunId"], request["sourceAttempt"])
        if key in anchors:
            continue
        context = {"scope": "issue", "issueNumber": request["issueNumber"], "runId": key[0]}
        if document["usage"]["sourceRuns"] >= BOUNDS["sourceRuns"]:
            reader.gap("source-run-budget", context, "Additional source runs were not verified.")
            continue
        document["usage"]["sourceRuns"] += 1
        endpoint = root + f"/actions/runs/{key[0]}"
        if key[1] is not None:
            endpoint += f"/attempts/{key[1]}"
        raw = reader.get(endpoint, context)
        anchor = admit(raw, context) if raw is not None else None
        if anchor is not None and (
            anchor["runId"] != key[0] or key[1] is not None and anchor["attempt"] != key[1]
        ):
            reader.gap("source-identity-mismatch", context, "Source response does not match the requested run/attempt.", endpoint)
            anchor = None
        anchors[key] = anchor
        if anchor is not None:
            keys.setdefault((anchor["workflowId"], anchor["workflowPath"], anchor["event"]), anchor)
    for workflow_id, path, event in list(keys)[BOUNDS["workflowWindows"]:]:
        reader.gap(
            "workflow-window-budget", {"scope": "workflow", "workflowId": workflow_id, "event": event},
            f"Window for {path} was not collected.",
        )
    for workflow_id, path, event in list(keys)[:BOUNDS["workflowWindows"]]:
        context = {"scope": "workflow", "workflowId": workflow_id, "event": event}
        selected = {}
        previous_run_number = None
        seen = 0
        exhausted = False
        page = 1
        history_start = cutoff
        history_end = document["collectedAt"]
        for _ in range(BOUNDS["historyPages"]):
            endpoint = root + f"/actions/workflows/{workflow_id}/runs?" + urlencode({
                "branch": branch, "event": event, "status": "completed",
                # Unbounded filtered listings can hit GitHub's 1,000-result search
                # limit and omit newer runs. Start with the same recent date range,
                # then spend the remaining request on older, infrequent executions.
                "created": f"{history_start}..{history_end}",
                "per_page": BOUNDS["historyPageSize"], "page": page,
            })
            result = reader.page(endpoint, "workflow_runs", context, BOUNDS["historyPageSize"])
            if result is None:
                break
            rows, total = result
            if total >= 1_000:
                reader.gap("github-search-limit", context, "Filtered history may exceed GitHub's search limit.", endpoint)
            seen += len(rows)
            for raw in rows:
                item = admit(raw, context, (
                    datetime.fromisoformat(history_start.replace("Z", "+00:00")),
                    datetime.fromisoformat(history_end.replace("Z", "+00:00")),
                ))
                if item is not None and (
                    item["workflowId"], item["workflowPath"], item["event"], item["status"]
                ) == (workflow_id, path, event, "completed"):
                    if previous_run_number is not None and item["runNumber"] > previous_run_number:
                        reader.gap("history-order-unverified", context, "History is not ordered newest independent run first.")
                    previous_run_number = item["runNumber"]
                    previous = selected.get(item["runId"])
                    if previous is None or item["attempt"] > previous["attempt"]:
                        selected[item["runId"]] = item
            exhausted = seen >= total
            if len(selected) >= BOUNDS["runsPerWindow"]:
                break
            if exhausted:
                if history_start != cutoff:
                    break
                history_start = (
                    now - timedelta(days=BOUNDS["historyLookbackDays"])
                ).astimezone(UTC).isoformat().replace("+00:00", "Z")
                history_end = cutoff
                seen = 0
                page = 1
                exhausted = False
            else:
                page += 1
        complete = exhausted or len(selected) >= BOUNDS["runsPerWindow"]
        if not complete:
            reader.gap("history-window-incomplete", context, "Fewer than five independent runs and history was not exhausted.")
        seed = keys[(workflow_id, path, event)]
        if (workflow_id, path, event) in recent_keys and seed["status"] == "completed" and (
            seed["runId"] not in selected or seed["attempt"] > selected[seed["runId"]]["attempt"]
        ):
            selected[seed["runId"]] = seed
            reader.gap("recent-history-disagreement", context, "Preserved the recent observation missing from history.")
        runs = sorted(selected.values(), key=lambda row: row["runNumber"], reverse=True)[:BOUNDS["runsPerWindow"]]
        for item in runs:
            job_context = {**context, "scope": "run", "runId": item["runId"]}
            _collect_jobs(reader, item, repository, normalize_job, job_context)
        gaps = [
            gap for gap in document["gaps"]
            if gap.get("workflowId") == workflow_id and gap.get("event") == event
            and gap.get("kind") == "coverage" and gap.get("scope") in {"repository", "workflow", "run"}
        ]
        document["workflows"].append({
            "workflowId": workflow_id, "workflowPath": path, "event": event,
            "runIds": [item["runId"] for item in runs],
            "windowComplete": complete and all(item["jobsComplete"] for item in runs) and not gaps, "gaps": gaps,
        })
        document["runs"].extend(runs)
    sampled = {item["runId"]: item for item in document["runs"]}
    for item in sorted(recent, key=lambda row: row["attempt"]):
        existing = sampled.get(item["runId"])
        if existing is not None and existing["attempt"] >= item["attempt"]:
            continue
        sampled[item["runId"]] = item
        context = {"scope": "run", "runId": item["runId"], "workflowId": item["workflowId"], "event": item["event"]}
        if existing is None:
            reader.gap("outside-job-window", context, "Recent run retained without jobs outside selected comparable windows.")
        else:
            reader.gap("newer-attempt-observed", context, "Recent inventory observed a newer attempt than the history window.")
            for window in document["workflows"]:
                if item["runId"] in window["runIds"]:
                    window["windowComplete"] = False
                    window["gaps"].append(document["gaps"][-1])
                    if item["status"] != "completed":
                        window["runIds"].remove(item["runId"])
        item["gaps"].append(document["gaps"][-1])
    document["runs"] = sorted(sampled.values(), key=lambda row: (_timestamp(row["createdAt"]), row["runId"]), reverse=True)
    observed = {(item["runId"], item["attempt"]): item for item in document["runs"]}
    for key, anchor in anchors.items():
        if anchor is None:
            continue
        identity = (anchor["runId"], anchor["attempt"])
        if identity in observed and not any(
            gap["code"] in {"outside-job-window", "newer-attempt-observed"} for gap in observed[identity]["gaps"]
        ):
            anchors[key] = observed[identity]
        else:
            _collect_jobs(
                reader, anchor, repository, normalize_job,
                {"scope": "source-run", "runId": anchor["runId"], "attempt": anchor["attempt"]},
            )
        if anchors[key] not in document["sourceRuns"]:
            document["sourceRuns"].append(anchors[key])
    complete_run_ids = {
        run_id for window in document["workflows"] if window["windowComplete"] for run_id in window["runIds"]
    }
    logs = _collect_diagnostics(reader, repository, extract_log_facts, existing_evidence)
    for request in source_requests:
        anchor = anchors.get((request["sourceRunId"], request["sourceAttempt"]))
        context = {"scope": "issue", "issueNumber": request["issueNumber"], "runId": request["sourceRunId"]}
        if anchor is None or not anchor["jobsComplete"]:
            reader.gap("source-job-coverage-incomplete", context, "The source job could not be verified.")
            continue
        candidates = [
            job for job in anchor["jobs"]
            if (
                job["name"] in request["jobNames"] if request["jobNames"]
                else job["status"] == "completed" and job["conclusion"] in {"failure", "timed_out"}
            )
        ]
        if (
            not candidates
            or not request["jobNames"] and len(candidates) != 1
            or len({job["name"] for job in candidates}) != len(candidates)
        ):
            reader.gap("source-job-ambiguous", context, "No unique affected source job was identified.")
            continue
        for source_job in candidates:
            if len(document["issueAssociations"]) >= BOUNDS["issueAssociations"]:
                reader.gap("association-budget", context, "Additional exact issue associations were not retained.")
                break
            evidence_ids = {
                f"run:{anchor['runId']}",
                f"run:{anchor['runId']}:attempt:{source_job['attempt']}:job:{source_job['jobId']}",
            }
            # Missing neighboring runs cannot erase a verified current failure.
            # Positive coverage still requires the complete comparable window.
            for observed_run in document["runs"]:
                for observed_job in observed_run["jobs"]:
                    if observed_job["laneId"] == source_job["laneId"] and (
                        observed_run["runId"] in complete_run_ids
                        or observed_run["jobsComplete"] and observed_job["status"] == "completed"
                        and observed_job["conclusion"] in {"failure", "timed_out"}
                    ):
                        evidence_ids.update({
                            f"run:{observed_run['runId']}",
                            f"run:{observed_run['runId']}:attempt:{observed_job['attempt']}:job:{observed_job['jobId']}",
                        })
                        evidence_ids.update(observed_job.get("logEvidenceIds", []))
            evidence_ids.update(source_job.get("logEvidenceIds", []))
            document["issueAssociations"].append({
                "issueNumber": request["issueNumber"], "sourceEvidenceId": request["sourceEvidenceId"],
                "sourceRunId": anchor["runId"], "sourceAttempt": source_job["attempt"], "sourceJobId": source_job["jobId"],
                "laneId": source_job["laneId"], "testTracker": request["testTracker"],
                "scope": "job-only", "evidenceIds": sorted(evidence_ids) if not request["testTracker"] else [],
            })
    document["status"] = "partial" if document["gaps"] else "complete"
    return DiscoveryResult(document, logs)


def _collect_diagnostics(reader: _Reader, repository: str, extract_facts: Callable, existing_evidence: dict) -> dict:
    document = reader.document
    logs = {}
    seen = set()
    for item in sorted([*document["runs"], *document["sourceRuns"]], key=lambda row: row["createdAt"], reverse=True):
        for job in item["jobs"]:
            if job["status"] != "completed" or job["conclusion"] not in {"failure", "timed_out"}:
                continue
            evidence_id = f"run:{item['runId']}:attempt:{job['attempt']}:job:{job['jobId']}:log"
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            context = {
                "kind": "diagnostic", "scope": "job", "workflowId": item["workflowId"],
                "event": item["event"], "runId": item["runId"], "jobId": job["jobId"],
            }
            job["diagnosticsComplete"] = False
            if document["usage"]["logs"] >= BOUNDS["logs"]:
                reader.gap("log-budget", context, "Additional failed-job logs were not inspected.")
                continue
            document["usage"]["logs"] += 1
            cached = existing_evidence.get(evidence_id, {})
            payload = cached.get("payload", {})
            from_cache = (
                cached.get("kind") == "workflow-log" and cached.get("availability") == "available"
                and payload.get("runId") == item["runId"] and payload.get("attempt") == job["attempt"]
                and payload.get("jobId") == job["jobId"] and payload.get("targetRepository") == repository
                and payload.get("truncated") is False and isinstance(payload.get("excerpt"), str)
                and len(payload["excerpt"].encode("utf-8")) <= BOUNDS["logBytes"]
            )
            if not from_cache:
                endpoint = f"/repos/{repository}/actions/jobs/{job['jobId']}/logs"
                response = reader.text(endpoint, context, BOUNDS["logBytes"])
                if response is None:
                    continue
                payload = {
                    "evidenceId": evidence_id, "runId": item["runId"], "attempt": job["attempt"],
                    "jobId": job["jobId"], "targetRepository": repository, "excerpt": response.text,
                    "facts": extract_facts(response.text, evidence_id), "truncated": response.truncated,
                    "status": response.status, "referencedBy": [],
                }
            logs[evidence_id] = payload
            job["logEvidenceIds"] = [evidence_id]
            job["diagnosticsComplete"] = not payload["truncated"]
            document["diagnostics"].append({
                "evidenceId": evidence_id, "runId": item["runId"], "attempt": job["attempt"], "jobId": job["jobId"],
                "complete": not payload["truncated"], "fromCache": from_cache,
            })
            if payload["truncated"]:
                reader.gap("log-truncated", context, "Failed-job diagnostics exceed the retained byte prefix.")
    return logs


def validate_workflow_discovery(value: object, repository: str, evidence: Mapping) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(f"workflowDiscovery: {message}")

    require(isinstance(value, Mapping), "expected an object.")
    require(type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1, "unsupported schemaVersion.")
    require(value.get("repository") == repository, "repository does not match the snapshot.")
    require(_timestamp(value.get("collectedAt")) is not None, "collectedAt must include a timezone.")
    require(type(value.get("defaultBranchVerified")) is bool, "defaultBranchVerified must be boolean.")
    verified = value["defaultBranchVerified"]
    require(
        isinstance(value.get("defaultBranch"), str) and bool(value["defaultBranch"]) and _positive(value.get("repositoryId"))
        if verified else value.get("defaultBranch") is None and value.get("repositoryId") is None,
        "default branch/repository identity must be verified or unknown.",
    )
    require(type(value.get("recentScanComplete")) is bool, "recentScanComplete must be boolean.")
    bounds, usage = value.get("bounds"), value.get("usage")
    require(isinstance(bounds, Mapping) and set(bounds) == set(BOUNDS), "missing bounds.")
    require(all(_positive(bounds[key]) and bounds[key] <= limit for key, limit in BOUNDS.items()), "invalid bounds.")
    require(isinstance(usage, Mapping), "missing usage.")
    for key, bound in (
        ("requests", "requests"), ("responseBodyBytes", "totalResponseBodyBytes"),
        ("jobs", "jobs"), ("sourceRuns", "sourceRuns"), ("logs", "logs"),
    ):
        require(type(usage.get(key)) is int and 0 <= usage[key] <= bounds[bound], f"invalid {key} usage.")
    for key in ("runs", "sourceRuns", "workflows", "issueAssociations", "gaps", "excludedRuns", "diagnostics"):
        require(isinstance(value.get(key), list), f"{key} must be an array.")
    require(
        value.get("status") == ("partial" if value["gaps"] else "complete") if verified
        else value.get("status") == "unavailable" and bool(value["gaps"]),
        "status contradicts verification/gaps.",
    )
    require(len(value["workflows"]) <= bounds["workflowWindows"], "too many workflow windows.")
    require(len(value["runs"]) <= (
        bounds["workflowWindows"] * bounds["runsPerWindow"] + bounds["recentPages"] * bounds["recentPageSize"]
    ), "too many sampled/recent runs.")
    require(len(value["sourceRuns"]) <= bounds["sourceRuns"], "too many source runs.")
    require(len(value["issueAssociations"]) <= bounds["issueAssociations"], "too many issue associations.")
    require(len(value["diagnostics"]) <= bounds["logs"], "too many diagnostics.")
    if not verified:
        require(not any(value[key] for key in ("runs", "sourceRuns", "workflows", "issueAssociations", "diagnostics")),
                "unverified repositories cannot contain admitted observations.")
    runs = {}
    source_runs = {}
    jobs = {}
    for collection, lookup in (("runs", runs), ("sourceRuns", source_runs)):
        for item in value[collection]:
            require(isinstance(item, Mapping), "run must be an object.")
            require(all(_positive(item.get(key)) for key in ("runId", "workflowId", "runNumber", "attempt")), "invalid run IDs.")
            require(
                item.get("targetRepository") == repository and item.get("headRepository") == repository
                and item.get("repositoryId") == value["repositoryId"] and item.get("headRepositoryId") == value["repositoryId"]
                and type(item.get("headRepositoryFork")) is bool and item.get("subjectPullRequests") == []
                and item.get("branch") == value["defaultBranch"]
                and isinstance(item.get("event"), str) and bool(item["event"]) and item["event"] not in _PR_EVENTS,
                "run scope does not match the verified default branch.",
            )
            require(isinstance(item.get("workflowPath"), str) and bool(item["workflowPath"]), "missing workflow path.")
            require(_timestamp(item.get("createdAt")) is not None, "invalid run creation timestamp.")
            require(isinstance(item.get("headSha"), str) and re.fullmatch(r"[0-9a-f]{40}", item["headSha"]) is not None,
                    "invalid run head.")
            require(isinstance(item.get("status"), str) and bool(item["status"]), "missing run status.")
            require(type(item.get("jobsComplete")) is bool and isinstance(item.get("jobs"), list), "invalid job coverage.")
            key = item["runId"] if collection == "runs" else (item["runId"], item["attempt"])
            require(key not in lookup, "duplicate independent run/source attempt.")
            lookup[key] = item
            local_jobs = set()
            lanes = set()
            for job in item["jobs"]:
                require(isinstance(job, Mapping) and _positive(job.get("jobId")), "invalid job.")
                require(job.get("runId") == item["runId"] and job.get("attempt") == item["attempt"], "job/run identity mismatch.")
                require(job.get("targetRepository") == repository, "foreign job.")
                require(isinstance(job.get("name"), str) and bool(job["name"]), "missing job name.")
                labels = job.get("runnerLabels")
                require(isinstance(labels, list) and all(isinstance(label, str) and label for label in labels), "invalid runner labels.")
                expected_identity = {
                    "repository": repository.casefold(), "workflowId": item["workflowId"],
                    "workflowPath": item["workflowPath"], "branch": item["branch"], "event": item["event"],
                    "jobName": job["name"], "runnerLabels": sorted(set(labels)),
                }
                require(
                    job.get("laneIdentity") == expected_identity
                    and job.get("laneId") == json.dumps(expected_identity, sort_keys=True, separators=(",", ":")),
                    "job lane is not bound to its verified workflow.",
                )
                require(job["jobId"] not in local_jobs, "duplicate job ID.")
                if item["jobsComplete"]:
                    require(job["laneId"] not in lanes, "complete jobs contain ambiguous lanes.")
                    if job.get("conclusion") == "success":
                        started = _timestamp(job.get("startedAt"))
                        completed = _timestamp(job.get("completedAt"))
                        require(job.get("status") == "completed" and started is not None and completed is not None
                                and completed >= started, "success lacks verified execution timestamps.")
                local_jobs.add(job["jobId"])
                lanes.add(job["laneId"])
                jobs[(item["runId"], item["attempt"], job["jobId"])] = job
    require(len(jobs) <= usage["jobs"], "job usage understates retained observations.")
    windows = set()
    complete_runs = set()
    for window in value["workflows"]:
        require(isinstance(window, Mapping), "invalid workflow window.")
        key = (window.get("workflowId"), window.get("workflowPath"), window.get("event"))
        require(_positive(key[0]) and isinstance(key[1], str) and isinstance(key[2], str)
                and bool(key[2]) and key[2] not in _PR_EVENTS, "invalid workflow identity.")
        require(key not in windows, "duplicate workflow window.")
        windows.add(key)
        ids = window.get("runIds")
        require(isinstance(ids, list) and all(_positive(number) for number in ids), "invalid window run IDs.")
        require(len(ids) == len(set(ids)) and len(ids) <= bounds["runsPerWindow"], "invalid independent run count.")
        require(type(window.get("windowComplete")) is bool and isinstance(window.get("gaps"), list), "invalid window coverage.")
        for run_id in ids:
            require(run_id in runs, "window names an unobserved run.")
            item = runs[run_id]
            require((item["workflowId"], item["workflowPath"], item["event"]) == key and item.get("status") == "completed",
                    "window mixes non-comparable runs.")
            if window["windowComplete"]:
                require(item["jobsComplete"] and not window["gaps"], "complete window has missing job coverage.")
                complete_runs.add(run_id)
    for association in value["issueAssociations"]:
        require(isinstance(association, Mapping), "invalid issue association.")
        require(all(_positive(association.get(key)) for key in ("issueNumber", "sourceRunId", "sourceAttempt", "sourceJobId")),
                "association IDs must be positive integers.")
        source_id = association.get("sourceEvidenceId")
        require(
            isinstance(source_id, str)
            and re.fullmatch(rf"issue:{association['issueNumber']}(?::comment:[1-9]\d*)?", source_id) is not None
            and source_id in evidence, "association has no exact issue source.",
        )
        anchor = source_runs.get((association["sourceRunId"], association["sourceAttempt"]))
        require(anchor is not None and anchor["jobsComplete"], "association lacks verified source-job coverage.")
        source_job = jobs.get((association["sourceRunId"], association["sourceAttempt"], association["sourceJobId"]))
        require(source_job is not None and association.get("laneId") == source_job["laneId"], "association has a different source lane.")
        require(type(association.get("testTracker")) is bool and association.get("scope") == "job-only", "invalid association scope.")
        issue_payload = evidence.get(f"issue:{association['issueNumber']}", {}).get("payload", {})
        source_payload = evidence[source_id].get("payload", {})
        test_tracker = bool(
            set(issue_payload.get("labels", [])) & {"quarantined-test", "test-failure", "failing-test"}
            or any(fact.get("field") == "testName" for fact in [
                *issue_payload.get("facts", []), *source_payload.get("facts", []),
            ])
        )
        require(association["testTracker"] == test_tracker, "association changes the issue's test-tracker scope.")
        expected_ids = {f"run:{anchor['runId']}", f"run:{anchor['runId']}:attempt:{source_job['attempt']}:job:{source_job['jobId']}"}
        expected_ids.update(source_job.get("logEvidenceIds", []))
        for run_id, observed_run in runs.items():
            for job in observed_run["jobs"]:
                if job["laneId"] == association["laneId"] and (
                    run_id in complete_runs
                    or observed_run["jobsComplete"] and job["status"] == "completed"
                    and job["conclusion"] in {"failure", "timed_out"}
                ):
                    expected_ids.update({f"run:{run_id}", f"run:{run_id}:attempt:{job['attempt']}:job:{job['jobId']}"})
                    expected_ids.update(job.get("logEvidenceIds", []))
        require(association.get("evidenceIds") == ([] if association["testTracker"] else sorted(expected_ids)),
                "association evidence does not match its verified, exact lane observations.")
        require(all(evidence_id in evidence for evidence_id in association["evidenceIds"]), "association evidence is absent.")
        for job in jobs.values():
            evidence_id = f"run:{job['runId']}:attempt:{job['attempt']}:job:{job['jobId']}"
            if evidence_id not in association["evidenceIds"]:
                continue
            record = evidence[evidence_id]
            require(record.get("kind") == "workflow-job", "associated job evidence has the wrong kind.")
            require(all(record["payload"].get(key) == job.get(key) for key in (
                "runId", "attempt", "jobId", "targetRepository", "name", "status", "conclusion",
                "startedAt", "completedAt", "laneId", "laneIdentity", "runnerLabels",
            )), "associated evidence disagrees with its observed job.")
    for diagnostic in value["diagnostics"]:
        require(isinstance(diagnostic, Mapping), "invalid diagnostic.")
        require(isinstance(diagnostic.get("evidenceId"), str), "invalid diagnostic evidence ID.")
        record = evidence.get(diagnostic.get("evidenceId"), {})
        require(record.get("kind") == "workflow-log", "diagnostic evidence is absent.")
        payload = record["payload"]
        require(all(payload.get(key) == diagnostic.get(key) for key in ("runId", "attempt", "jobId")), "diagnostic identity mismatch.")
        require(type(diagnostic.get("complete")) is bool and diagnostic["complete"] is (not payload["truncated"]),
                "diagnostic completeness contradicts its retained log.")
