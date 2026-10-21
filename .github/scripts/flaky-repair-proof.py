"""Fork-only repair experiment; candidate code never receives a write credential."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location(
    "real_proof", Path(__file__).with_name("flaky-real-proof.py")
)
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)

REPOSITORY = real.REPOSITORY
BASE = "aa56e005faa2ee6326f3c777f6d30dd43fece929"
MEMORY = "memory/flaky-repair-20261021"
EXPERIMENT = "repair-20261021"
SOURCE = "tests/Aspire.Hosting.Tests/ResourceFailureLoggingTests.cs"
AGENT = ".github/workflows/flaky-repair-agent.lock.yml"
PUBLISHER = ".github/workflows/flaky-repair-publish.yml"
WORKER = ".github/workflows/reproduce-flaky-tests.yml"
ROOT = Path("/tmp/gh-aw/repair-evidence")
ITERATIONS = 25
COMMIT_MESSAGE = """test: synchronize immediate-exit assertions with log capture

Expected stdout or stderr can be missing when assertions race resource
log capture. Synchronize the test with the output it asserts rather than
weakening the assertions. Keep the test quarantined for monitoring.

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
"""


def api(endpoint, body=None):
    command = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
    if body is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(body) if body is not None else None,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


def write(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / name).write_text(json.dumps(value, indent=2) + "\n")


def source_at(sha):
    document = api(f"contents/{SOURCE}?ref={sha}")
    if document["type"] != "file":
        raise ValueError("Target must be an ordinary source file")
    return base64.b64decode(document["content"]).decode()


def check_state(state):
    if state.get("version") != 1 or state.get("experiment") != EXPERIMENT:
        raise ValueError("Unrecognized repair checkpoint")
    return state


def checkpoint():
    document = api(f"contents/checkpoint.json?ref={MEMORY.replace('/', '%2F')}")
    return check_state(json.loads(base64.b64decode(document["content"])))


def run_identity(run_id, path):
    if not isinstance(run_id, str) or not run_id.isdecimal():
        raise ValueError("Invalid run ID")
    run = api(f"actions/runs/{run_id}")
    if (
        run["path"] != path or run["event"] != "workflow_dispatch"
        or run["head_repository"]["full_name"] != REPOSITORY
        or run["head_branch"] != "main" or run["run_attempt"] != 1
    ):
        raise ValueError("Run is not a trusted first-attempt main workflow")
    return run


def download(run_id, name, directory):
    subprocess.run([
        "gh", "run", "download", run_id, "--repo", REPOSITORY,
        "--name", name, "--dir", str(directory),
    ], check=True)


def memory_at(run_id):
    run_identity(run_id, AGENT)
    real.wait_completed(run_id, True)
    directory = ROOT / f"memory-{run_id}"
    download(run_id, "repo-memory-default", directory)
    state = check_state(json.loads((directory / "checkpoint.json").read_text()))
    return directory, state


def worker_identity(worker_id):
    worker = run_identity(worker_id, WORKER)
    # Trusted run-name: Repair reproduce [39531803470] <40-character source SHA>.
    match = re.fullmatch(
        r"Repair (reproduce|verify) \[([0-9]+)\] ([0-9a-f]{40})",
        worker["display_title"],
    )
    if match is None:
        raise ValueError("Unexpected repair worker notification")
    return worker, match[1], match[2], match[3]


def collect_worker(worker_id):
    worker, phase, parent_id, candidate = worker_identity(worker_id)
    real.wait_completed(worker_id, False)
    receipt_dir = ROOT / f"checkout-{worker_id}"
    download(worker_id, "repair-worker-receipt", receipt_dir)
    checkout = json.loads((receipt_dir / "candidate-receipt.json").read_text(encoding="utf-8-sig"))
    expected = {
        "candidate_sha": candidate, "workflow_sha": worker["head_sha"],
        "phase": phase, "controller_run_id": parent_id,
        "run_id": worker_id, "run_attempt": "1",
    }
    if checkout != expected:
        raise ValueError("Actual checkout does not match authorized source and workflow")
    jobs = api(f"actions/runs/{worker_id}/jobs?per_page=100")["jobs"]
    job = next(j for j in jobs if j["name"] == "windows-latest #1")
    log = subprocess.run([
        "gh", "api", "--allow-escape-sequences",
        f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs",
    ], capture_output=True, check=True).stdout
    if not log:
        raise ValueError("Worker job log is empty")
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "job.log").write_bytes(log)
    artifacts = api(f"actions/runs/{worker_id}/artifacts?per_page=100")["artifacts"]
    artifact = ROOT / f"worker-{worker_id}"
    receipt = {
        "run_id": worker_id, "phase": phase, "parent_run_id": parent_id,
        "candidate_sha": candidate, "workflow_sha": worker["head_sha"],
        "test": f"{real.CLASS}.{real.METHOD}", "job_id": job["id"],
        "job_conclusion": job["conclusion"], "job_log": str(ROOT / "job.log"),
    }
    name = "real-test-evidence-windows-latest-1"
    if not any(a["name"] == name and not a["expired"] for a in artifacts):
        receipt["evidence_error"] = "Test artifact absent; inspect job.log for build or runner failure"
        return receipt
    download(worker_id, name, artifact)
    try:
        observations = real.validate_evidence(artifact, ITERATIONS)
        failed = sum(item["failed"] for item in observations)
        if job["conclusion"] != ("failure" if failed else "success"):
            raise ValueError("Job outcome disagrees with executed target results")
    except (ValueError, ET.ParseError) as error:
        # Invalid/incomplete results are supplied as an explicit blocker, never
        # converted into a passing observation or hidden from the resumed agent.
        receipt["evidence_error"] = str(error)
        receipt["artifact_directory"] = str(artifact)
        return receipt
    receipt.update(
        executed=len(observations), failed=failed, iterations=observations,
        artifact_directory=str(artifact),
    )
    return receipt


def publication_at(run_id):
    run = run_identity(run_id, PUBLISHER)
    real.wait_completed(run_id, True)
    directory = ROOT / f"publication-{run_id}"
    download(run_id, "repair-publication", directory)
    publication = json.loads((directory / "publication.json").read_text())
    match = re.fullmatch(r"Publish repair \[([0-9]+)\]", run["display_title"])
    if (
        match is None or publication["agent_run_id"] != match[1]
        or publication["publisher_run_id"] != run_id
        or publication["base_sha"] != BASE
    ):
        raise ValueError("Publication does not match its trusted publisher")
    commit = api(f"git/commits/{publication['candidate_sha']}")
    if [parent["sha"] for parent in commit["parents"]] != [BASE]:
        raise ValueError("Published candidate has an unexpected parent")
    comparison = api(f"compare/{BASE}...{publication['candidate_sha']}")
    if [(f["filename"], f["status"]) for f in comparison["files"]] != [(SOURCE, "modified")]:
        raise ValueError("Published candidate changed unauthorized paths")
    return publication


def validate_candidate(payload, original):
    if payload.get("base_sha") != BASE:
        raise ValueError("Candidate has a stale or unauthorized base")
    content = payload.get("test_source")
    if not isinstance(content, str) or len(content.encode()) > 20000 or content == original:
        raise ValueError("Candidate must contain a bounded, changed source file")
    marker = "    public async Task ExecutableExitsImmediately()\n    {"
    prefix_end = original.index(marker) + len(marker)
    suffix_start = original.index("\n    [Fact]", prefix_end)
    if not content.startswith(original[:prefix_end]) or not content.endswith(original[suffix_start:]):
        raise ValueError("Candidate changes outside the authorized target method")
    for assertion in (
        'Assert.Contains(logLines, x => x.EndsWith("Hello from Stdout"));',
        'Assert.Contains(logLines, x => x.EndsWith("Hello from Stderr"));',
    ):
        if assertion not in content:
            raise ValueError("Candidate must retain both output assertions")
    diagnosis = payload.get("diagnosis")
    if not isinstance(diagnosis, str) or not 100 <= len(diagnosis) <= 12000:
        raise ValueError("Candidate requires a substantive evidence-backed diagnosis")
    return content


def publish(agent_id):
    directory, state = memory_at(agent_id)
    if state["phase"] != "candidate-ready" or str(state["agent_run_id"]) != agent_id:
        raise ValueError("Agent did not prepare an authorized candidate")
    payload = json.loads((directory / "candidate.json").read_text())
    content = validate_candidate(payload, source_at(BASE))
    baseline = collect_worker(str(payload["baseline_run_id"]))
    if (
        baseline["phase"] != "reproduce" or baseline["candidate_sha"] != BASE
        or baseline.get("executed") != ITERATIONS or baseline.get("failed", 0) < 1
        or not any("Assert.Contains" in (i["failure"] or "") for i in baseline["iterations"])
    ):
        raise ValueError("No actual matching baseline assertion failure authorizes repair publication")
    base = api(f"git/commits/{BASE}")
    blob = api("git/blobs", {"content": content, "encoding": "utf-8"})
    tree = api("git/trees", {
        "base_tree": base["tree"]["sha"],
        "tree": [{"path": SOURCE, "mode": "100644", "type": "blob", "sha": blob["sha"]}],
    })
    commit = api("git/commits", {
        "message": COMMIT_MESSAGE, "tree": tree["sha"], "parents": [BASE],
    })
    branch = f"flaky-repair-candidate-20261021-{agent_id}"
    # Creation only: a duplicate or conflicting ref fails instead of rewriting it.
    api("git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    published = api(f"git/ref/heads/{branch}")["object"]["sha"]
    if published != commit["sha"] or published == BASE:
        raise ValueError("Published source identity was not the changed commit")
    publication = {
        "base_sha": BASE, "candidate_sha": published, "branch": branch,
        "agent_run_id": agent_id, "publisher_run_id": os.environ["GITHUB_RUN_ID"],
        "baseline_run_id": baseline["run_id"], "source_path": SOURCE,
        "diagnosis": payload["diagnosis"],
    }
    write("publication.json", publication)
    api("actions/workflows/reproduce-flaky-tests.yml/dispatches", {
        "ref": "main", "inputs": {
            "controller_run_id": os.environ["GITHUB_RUN_ID"],
            "repair_phase": "verify", "candidate_sha": published,
            "repair_iterations": str(ITERATIONS),
        },
    })
    print(json.dumps({"published": published, "branch": branch}))


def context(worker_id):
    _, phase, parent_id, candidate = worker_identity(worker_id)
    if phase == "reproduce":
        _, state = memory_at(parent_id)
        if (
            state["phase"] != "awaiting-reproduction"
            or str(state["agent_run_id"]) != parent_id or candidate != BASE
        ):
            raise ValueError("Baseline is not authorized by its immutable parent checkpoint")
    else:
        publication = publication_at(parent_id)
        if candidate != publication["candidate_sha"] or candidate == BASE:
            raise ValueError("Verification did not use the actually published changed commit")
        write("publication.json", publication)
    return phase, candidate


def prepare(worker_id):
    ROOT.mkdir(parents=True)
    issue = subprocess.run([
        "gh", "api", "repos/microsoft/aspire/issues/16189",
    ], capture_output=True, text=True, check=True)
    issue = json.loads(issue.stdout)
    if issue["state"] != "open" or "pull_request" in issue or real.METHOD not in issue["title"]:
        raise ValueError("Issue does not match the repair experiment")
    write("issue.json", issue)
    candidate = BASE
    if worker_id:
        _, candidate = context(worker_id)
        write("receipt.json", collect_worker(worker_id))
    (ROOT / "test-source.cs").write_text(source_at(candidate))
    write("candidate-identity.json", {"sha": candidate, "base_sha": BASE})


def resume(worker_id):
    worker_identity(worker_id)
    real.wait_completed(worker_id, False)
    phase, _ = context(worker_id)
    state = checkpoint()
    if state["phase"] in {"repair-verified", "verification-failed", "blocked"}:
        print("Investigation is terminal; no dispatch")
        return
    api("actions/workflows/flaky-repair-agent.lock.yml/dispatches", {
        "ref": "main", "inputs": {"phase": "resume", "worker_run_id": worker_id},
    })
    print(json.dumps({"resumed": worker_id, "phase": phase}))


if __name__ == "__main__":
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise SystemExit("This experiment is restricted to radical/aspire")
    operation = sys.argv[1]
    if operation == "prepare":
        prepare(os.environ.get("WORKER_RUN_ID", ""))
    elif operation == "publish":
        publish(os.environ["AGENT_RUN_ID"])
    elif operation == "resume":
        resume(os.environ["WORKER_RUN_ID"])
    else:
        raise SystemExit("Unknown repair operation")
