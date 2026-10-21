"""Fork-only experiment connecting real Aspire test runs to bounded agent phases."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


REPOSITORY = "radical/aspire"
CANDIDATE = "flaky-real-candidate-20261021"
MEMORY = "memory/flaky-real-20261021"
EXPERIMENT = "real-test-20261021"
METHOD = "ExecutableExitsImmediately"
CLASS = "Aspire.Hosting.Tests.ExecutableResourceFailureLoggingTests"
ROOT = Path("/tmp/gh-aw/real-test-evidence")


def api(endpoint):
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def checkpoint():
    stored = api(f"contents/checkpoint.json?ref={MEMORY.replace('/', '%2F')}")
    state = json.loads(base64.b64decode(stored["content"]))
    if state["version"] != 1 or state["experiment"] != EXPERIMENT:
        raise ValueError("Unrecognized investigation checkpoint")
    return state


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, indent=2) + "\n")


def validate_evidence(directory, iterations):
    observations = []
    for iteration in range(1, iterations + 1):
        folder = directory / f"iteration-{iteration}"
        log = folder / "output.log"
        if not log.is_file() or log.stat().st_size == 0:
            raise ValueError("Missing iteration output")
        files = list(folder.rglob("*.trx"))
        if len(files) != 1:
            raise ValueError(f"Expected exactly one TRX per iteration; found {len(files)}")
        tree = ET.parse(files[0])
        definitions = tree.findall(".//{*}TestDefinitions/{*}UnitTest")
        if len(definitions) != 1:
            raise ValueError("Selection did not contain exactly one defined test")
        method = definitions[0].find("{*}TestMethod")
        if method is None or method.get("name") != METHOD or method.get("className") != CLASS:
            raise ValueError("TRX identifies a different test")
        results = tree.findall(".//{*}Results/{*}UnitTestResult")
        if (
            len(results) != 1
            or results[0].get("testId") != definitions[0].get("id")
            or results[0].get("outcome") not in {"Passed", "Failed"}
        ):
            raise ValueError("Target did not actually execute")
        counters = tree.find(".//{*}ResultSummary/{*}Counters")
        failed = int(results[0].get("outcome") == "Failed")
        if counters is None or any(
            int(counters.get(key, "-1")) != expected
            for key, expected in {
                "total": 1, "executed": 1, "passed": 1 - failed, "failed": failed,
            }.items()
        ):
            raise ValueError("TRX counters do not match the executed target")
        error = results[0].find(".//{*}ErrorInfo/{*}Message")
        observations.append({
            "iteration": iteration, "executed": 1, "failed": failed,
            "outcome": results[0].get("outcome"),
            "failure": error.text if error is not None else None,
            "trx": str(files[0].relative_to(directory)),
            "log_bytes": log.stat().st_size,
        })
    actual = {p.name for p in directory.glob("iteration-*") if p.is_dir()}
    expected = {f"iteration-{i}" for i in range(1, iterations + 1)}
    if actual != expected:
        raise ValueError("Artifact has unexpected or missing iterations")
    return observations


def worker_receipt(worker_id, state):
    if not worker_id.isdecimal():
        raise ValueError("Invalid worker run ID")
    worker = api(f"actions/runs/{worker_id}")
    # run-name is emitted as: Real test proof round 1 [39521922381].
    match = re.fullmatch(r"Real test proof round ([12]) \[([0-9]+)\]", worker["display_title"])
    if (
        match is None
        or worker["path"] != ".github/workflows/reproduce-flaky-tests.yml"
        or worker["event"] != "workflow_dispatch"
        or worker["head_repository"]["full_name"] != REPOSITORY
        or worker["head_branch"] != CANDIDATE
        or worker["head_sha"] != state["candidate_sha"]
        or worker["run_attempt"] != 1
    ):
        raise ValueError("Worker does not match the authorized candidate")
    round_number, parent_id = int(match[1]), match[2]
    if (
        state["phase"] != "awaiting-reproduction"
        or state["round"] != round_number
        or str(state["parent_run_id"]) != parent_id
        or state["iterations"] != round_number
        or len(state["rounds"]) != round_number - 1
    ):
        raise ValueError("Checkpoint does not authorize this worker")
    return worker, parent_id


def wait_completed(run_id, fail):
    result = api(f"actions/runs/{run_id}")
    if result["status"] != "completed":
        command = ["gh", "run", "watch", run_id, "--repo", REPOSITORY, "--interval", "15"]
        if fail:
            command.append("--exit-status")
        subprocess.run(command, check=True, timeout=480, stdout=subprocess.DEVNULL)
        result = api(f"actions/runs/{run_id}")
    if result["status"] != "completed" or (fail and result["conclusion"] != "success"):
        raise ValueError("Required parent run did not complete successfully")
    return result


def resume(worker_id):
    if not worker_id.isdecimal():
        raise ValueError("Invalid worker run ID")
    worker = api(f"actions/runs/{worker_id}")
    match = re.fullmatch(r"Real test proof round ([12]) \[([0-9]+)\]", worker["display_title"])
    if match is None or worker["path"] != ".github/workflows/reproduce-flaky-tests.yml":
        raise ValueError("Unexpected worker notification")
    parent_id = match[2]
    wait_completed(worker_id, False)
    parent = api(f"actions/runs/{parent_id}")
    if (
        parent["path"] != ".github/workflows/flaky-real-agent.lock.yml"
        or parent["head_repository"]["full_name"] != REPOSITORY
        or parent["event"] != "workflow_dispatch"
    ):
        raise ValueError("Unexpected parent agent")
    wait_completed(parent_id, True)
    # Safe outputs can start CI before repo-memory is pushed. Read state again
    # only after the full parent, including its memory push, has completed.
    state = checkpoint()
    if state["phase"] == "orchestration-proven":
        print("Investigation already terminal; no dispatch")
        return
    worker_receipt(worker_id, state)
    subprocess.run([
        "gh", "workflow", "run", "flaky-real-agent.lock.yml",
        "--repo", REPOSITORY, "--ref", "main",
        "-f", "phase=resume", "-f", f"worker_run_id={worker_id}",
    ], check=True)
    print(json.dumps({"worker_run_id": worker_id, "parent_run_id": parent_id}))


def prepare(worker_id):
    ROOT.mkdir(parents=True)
    issue = subprocess.run([
        "gh", "api", "repos/microsoft/aspire/issues/16189",
    ], capture_output=True, text=True, check=True)
    issue = json.loads(issue.stdout)
    if issue["state"] != "open" or "pull_request" in issue or METHOD not in issue["title"]:
        raise ValueError("Issue no longer matches this quarantined-test experiment")
    write("issue.json", issue)
    candidate = api(f"git/ref/heads/{CANDIDATE}")["object"]["sha"]
    source = api(
        "contents/tests/Aspire.Hosting.Tests/ResourceFailureLoggingTests.cs"
        f"?ref={candidate}"
    )
    (ROOT / "test-source.cs").write_bytes(base64.b64decode(source["content"]))
    write("candidate.json", {"sha": candidate, "ref": CANDIDATE, "experiment": EXPERIMENT})
    if not worker_id:
        return
    state = checkpoint()
    worker, parent_id = worker_receipt(worker_id, state)
    if candidate != state["candidate_sha"]:
        raise ValueError("Candidate ref moved since investigation started")
    if worker["status"] != "completed" or worker["conclusion"] not in {"success", "failure"}:
        raise ValueError("Worker is not completed")
    artifact = ROOT / "artifact"
    subprocess.run([
        "gh", "run", "download", worker_id, "--repo", REPOSITORY,
        "--name", "real-test-evidence-windows-latest-1", "--dir", str(artifact),
    ], check=True)
    observations = validate_evidence(artifact, state["iterations"])
    jobs = api(f"actions/runs/{worker_id}/jobs?per_page=100")["jobs"]
    job = next(j for j in jobs if j["name"] == "windows-latest #1")
    failed = sum(item["failed"] for item in observations)
    if job["conclusion"] != ("failure" if failed else "success"):
        raise ValueError("Job outcome does not agree with actual test results")
    log = subprocess.run([
        "gh", "api", "--allow-escape-sequences",
        f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs",
    ], check=True, capture_output=True).stdout
    if not log:
        raise ValueError("Job log is empty")
    (ROOT / "job.log").write_bytes(log)
    write("receipt.json", {
        "run_id": worker_id, "run_attempt": worker["run_attempt"],
        "candidate_sha": candidate, "round": state["round"],
        "parent_run_id": parent_id, "test": f"{CLASS}.{METHOD}",
        "executed": len(observations), "failed": failed,
        "iterations": observations, "job_id": job["id"], "job_log_bytes": len(log),
        "artifact_files": len([p for p in artifact.rglob("*") if p.is_file()]),
        "collector": "trusted-actions-preprocessing",
    })


if __name__ == "__main__":
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise SystemExit("This experiment is restricted to radical/aspire")
    operation = sys.argv[1]
    if operation == "prepare":
        prepare(os.environ.get("WORKER_RUN_ID", ""))
    elif operation == "resume":
        resume(os.environ["WORKER_RUN_ID"])
    else:
        raise SystemExit("Unknown operation")
