"""Authenticate and stage original hosted evidence before the final model phase."""

import base64
from collections import defaultdict
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile

from pr21428_native_evidence import read_target


ROOT = Path(os.environ.get("PR21428_EVIDENCE_ROOT", "/tmp/gh-aw/pr21428-validation"))
ROOT.mkdir(parents=True, exist_ok=True)
context = json.loads(Path(".github/scripts/pr21428-native-validation-context.json").read_text())
if os.environ["STATE_REVISION"] != context["state_revision"]:
    raise ValueError("Dispatch revision differs from committed evidence context")
REPO = "microsoft/aspire"
FAMILY = "windows-2025-vs2026"


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def api(endpoint, raw=False):
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "api", "--allow-escape-sequences", f"repos/{REPO}/{endpoint}"],
            capture_output=True, timeout=120)
        if result.returncode == 0:
            return result.stdout if raw else json.loads(result.stdout)
        error = result.stderr.decode(errors="replace")
        if attempt < 2 and re.search(r"HTTP (502|503|504)", error):
            time.sleep(5)
            continue
        raise RuntimeError(f"Read-only GitHub request {endpoint}: {error}")


pr = api("pulls/21428")
if pr["state"] != "open" or pr["head"]["sha"] != context["sources"]["after"]:
    raise ValueError("PR no longer open at the admitted head")
save(ROOT / "pr.json", pr)
source_path = "tests/Aspire.Hosting.RemoteHost.Tests/IntegrationHostLauncherTests.cs"
for case, source in context["sources"].items():
    publication = context["publications"][case]
    commit = api(f"git/commits/{publication['sha']}")
    if [parent["sha"] for parent in commit["parents"]] != [source]:
        raise ValueError("Validation commit is not a direct child of admitted test source")
    if api(f"git/ref/heads/{publication['branch']}")["object"]["sha"] != publication["sha"]:
        raise ValueError("Validation branch moved")
    comparison = api(f"compare/{source}...{publication['sha']}")
    if [entry["filename"] for entry in comparison["files"]] != [
            ".github/workflows/reproduce-flaky-tests.yml"]:
        raise ValueError("Validation commit changes test or product source")
    for label, path, sha in (
            ("source.cs", source_path, source),
            ("workflow.yml", ".github/workflows/reproduce-flaky-tests.yml", publication["sha"])):
        blob = api(f"contents/{path}?ref={sha}")
        (ROOT / f"{case}-{label}").write_bytes(base64.b64decode(blob["content"]))
if (ROOT / "before-workflow.yml").read_bytes() != (ROOT / "after-workflow.yml").read_bytes():
    raise ValueError("Before and after workflow configurations differ")

counts = {case: {"executions": 0, "passed": 0, "original_timeouts": 0,
                 "different_target_failures": 0, "other_test_failures": 0}
          for case in ("before", "after")}
images = defaultdict(lambda: {case: {key: 0 for key in counts[case]}
                             for case in ("before", "after")})
observations = []
runner_ids = set()
for owned in context["runs"]:
    run = api(f"actions/runs/{owned['run_id']}")
    case = owned["case"]
    publication = context["publications"][case]
    expected = {"head_sha": publication["sha"], "head_branch": publication["branch"],
                "event": "workflow_dispatch", "workflow_id": 238676131,
                "run_attempt": 1, "status": "completed"}
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError("Foreign, stale, incomplete or rerun evidence")
    directory = ROOT / str(run["id"])
    directory.mkdir()
    save(directory / "run.json", run)
    jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")
    save(directory / "jobs.json", jobs)
    windows = [job for job in jobs["jobs"] if job["name"].startswith(f"{FAMILY} #")]
    if len(windows) != 10 or any(job["conclusion"] not in {"success", "failure"} for job in windows):
        raise ValueError("Missing or interrupted hosted Windows jobs")
    artifacts = api(f"actions/runs/{run['id']}/artifacts?per_page=100")
    save(directory / "artifacts.json", artifacts)
    by_name = {artifact["name"]: artifact for artifact in artifacts["artifacts"]}
    for job in windows:
        if job["labels"] != [FAMILY] or job["runner_group_name"] != "GitHub Actions":
            raise ValueError("Wrong hosted runner family or group")
        if job["runner_id"] in runner_ids:
            raise ValueError("Duplicate or reused runner identity")
        runner_ids.add(job["runner_id"])
        index = int(job["name"].rsplit("#", 1)[1])
        artifact = by_name[f"iteration-results-{FAMILY}-{index}"]
        if artifact["expired"] or artifact["workflow_run"]["id"] != run["id"]:
            raise ValueError("Expired or foreign TRX artifact")
        archive = api(f"actions/artifacts/{artifact['id']}/zip", raw=True)
        (directory / f"artifact-{artifact['id']}.zip").write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            reports = [name for name in zipped.namelist() if name.endswith(".trx")]
            if len(reports) != 1 or Path(reports[0]).parent.name != "iteration-1":
                raise ValueError("Expected one retained first-execution TRX per fresh runner")
            report = directory / f"runner-{index}.trx"
            report.write_bytes(zipped.read(reports[0]))
        row = read_target(report, case)
        log = api(f"actions/jobs/{job['id']}/logs", raw=True).decode()
        (directory / f"job-{job['id']}.log").write_text(log)
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log)
        block = re.search(r"##\[group\]Runner Image\r?\n(.*?)##\[endgroup\]", clean, re.S)
        if block is None:
            raise ValueError("Missing original runner-image setup group")
        image = re.search(r"Image: ([^\r\n]+)", block[1]).group(1).strip()
        version = re.search(r"Version: ([^\r\n]+)", block[1]).group(1).strip()
        if image != FAMILY:
            raise ValueError("Actual runner image family differs from admitted family")
        row.update(case=case, run_id=run["id"], runner=index, runner_id=job["runner_id"],
                   image=image, image_version=version, original_trx=str(report))
        observations.append(row)
        for aggregate in (counts[case], images[version][case]):
            aggregate["executions"] += 1
            aggregate["passed"] += row["target_outcome"] == "Passed"
            aggregate["original_timeouts"] += row["original_timeout"]
            aggregate["different_target_failures"] += (
                row["target_outcome"] == "Failed" and not row["original_timeout"])
            aggregate["other_test_failures"] += row["other_failures"]
if counts != context["expected_counts"] or dict(images) != context["expected_images"]:
    raise ValueError("Fresh authenticated counts/images differ from admitted collected evidence")
save(ROOT / "context.json", context)
save(ROOT / "target-observations.json", observations)
save(ROOT / "authenticated-results.json", {
    "counts": counts, "images": dict(images), "unique_fresh_runners": len(runner_ids),
    "sources": context["sources"], "test_dispatches_agent_directed": False,
})
save(ROOT / "expected-handoff.json", {
    "version": 1, "pr": 21428, "state_revision": context["state_revision"],
    "action": "finish", "outcome": context["expected_outcome"],
    "sources": context["sources"], "baseline_reproduced": counts["before"]["original_timeouts"] > 0,
    "observed_suite_iterations": {case: counts[case]["executions"] for case in counts},
    "target_results": counts, "image_results": dict(images), "agent_directed_test_dispatches": 0,
})
print(json.dumps({"staged_original_targets": len(observations),
                  "unique_fresh_runners": len(runner_ids), "counts": counts}))
