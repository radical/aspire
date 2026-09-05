from __future__ import annotations

import copy
from datetime import datetime, timedelta

from ci_shepherd.lifecycle import prepare_assessment


def with_exact_coverage(value: dict) -> dict:
    """Add collected job executions to older run-only recovery fixtures."""
    value = copy.deepcopy(value)
    records = value["evidence"]
    for number in value["openIssues"]:
        issue = records[f"issue:{number}"]["payload"]
        rows = issue["ledger"]["rows"]
        for row in rows:
            row["job"] = "Build (ubuntu-latest)"
        reference = [{"sourceIssueNumber": number, "sourceEvidenceId": f"issue:{number}"}]
        for row in rows:
            run_id = row["sourceRun"]
            if not any(r["kind"] == "workflow-run" and r["payload"].get("runId") == run_id
                       for r in records.values()):
                records[f"run:{run_id}"] = {
                    "kind": "workflow-run", "availability": "available",
                    "url": f"https://github.com/{value['repository']}/actions/runs/{run_id}",
                    "payload": {"runId": run_id, "conclusion": "failure",
                                "createdAt": row["date"] + "T00:00:00Z"},
                }
        for key, record in list(records.items()):
            if record["kind"] != "workflow-run":
                continue
            payload = record["payload"]
            run_id = payload.setdefault("runId", int(key.split(":")[1]))
            if key != f"run:{run_id}":
                records[f"run:{run_id}"] = records.pop(key)
            payload.setdefault("status", "completed")
            payload.setdefault("headSha", "a" * 40)
            payload.setdefault("targetRepository", value["repository"])
            payload.setdefault("workflow", "CI")
            payload.setdefault("branch", "main" if rows[0].get("pullRequest") is None else "feature")
            payload.setdefault("event", "push" if payload["branch"] == "main" else "pull_request")
            payload.setdefault("subjectPullRequests", [{
                "number": rows[0].get("pullRequest", 1),
                "headSha": payload["headSha"], "baseRepository": value["repository"],
            }])
            payload.setdefault("referencedBy", reference)
            when = payload.get("runStartedAt") or payload.get("createdAt")
            payload.setdefault("createdAt", when)
            job_id = run_id + 1000
            job_key = f"run:{run_id}:attempt:1:job:{job_id}"
            records[job_key] = {
                "kind": "workflow-job", "availability": "available", "url": record["url"],
                "payload": {
                    "runId": run_id, "attempt": 1, "jobId": job_id,
                    "name": "Build (ubuntu-latest)", "status": "completed",
                    "conclusion": payload["conclusion"], "completedAt": when,
                    "referencedBy": reference,
                    "errorMessage": "src/Program.cs(1): error CS1002: ; expected"
                    if payload["conclusion"] == "failure" else "",
                },
            }
            test_name = next(
                (fact.get("normalized") or fact.get("raw") for fact in issue.get("facts", [])
                 if fact.get("field") == "testName"),
                None,
            )
            if test_name:
                records[f"{job_key}:log"] = {
                    "kind": "workflow-log", "availability": "available", "url": record["url"],
                    "payload": {
                        "runId": run_id, "attempt": 1, "jobId": job_id, "referencedBy": reference,
                        "excerpt": f"{'Passed' if payload['conclusion'] == 'success' else 'Failed'} {test_name} [42 ms]",
                    },
                }
    return value


def with_prepared_recovery(prepared: dict) -> dict:
    """Upgrade explicit positive compact fixtures through factual preparation."""
    prepared = copy.deepcopy(prepared)
    for issue in prepared["issues"]:
        number = issue["issueNumber"]
        records = {record["id"]: {key: val for key, val in record.items() if key != "id"}
                   for record in issue["evidenceBundle"]
                   if not record["kind"].startswith("workflow-") or record["kind"] == "workflow-run"}
        payload = records[f"issue:{number}"]["payload"]
        test_name = issue.get("identity", {}).get("tier2TestName")
        payload.update({"number": number, "producer": issue["producer"], "ledger": copy.deepcopy(issue["ledger"]),
                        "title": issue["title"], "facts": [{"field": "testName", "normalized": test_name, "raw": test_name}] if test_name else [],
                        "url": f"https://github.com/{prepared['repository']}/issues/{number}"})
        for record in records.values():
            record.setdefault("url", payload["url"])
            record["payload"]["referencedBy"] = [{"sourceIssueNumber": number}]
            if record["kind"] == "workflow-run":
                record["payload"].setdefault("createdAt", prepared["sourceCollectedAt"][:10] + "T23:59:00Z")
        for row in payload["ledger"]["rows"]:
            if any(r["kind"] == "workflow-run" and r["payload"].get("runId") == row["sourceRun"]
                   and r["payload"].get("conclusion") == "success" for r in records.values()):
                row["sourceRun"] += 1
        instants = [
            datetime.fromisoformat(record["payload"]["createdAt"].replace("Z", "+00:00"))
            for record in records.values()
            if record["kind"] == "workflow-run" and record["payload"].get("createdAt")
        ]
        collected = prepared["sourceCollectedAt"]
        if instants:
            collected = (max(instants) + timedelta(days=1)).isoformat()
        raw = with_exact_coverage({
            "repository": prepared["repository"], "collectedAt": collected,
            "openIssues": [number], "evidence": records, "collectionErrors": [],
        })
        upgraded = prepare_assessment(raw)["issues"][0]
        issue["recovery"] = upgraded["recovery"]
        issue["evidenceBundle"] = upgraded["evidenceBundle"]
    return prepared
