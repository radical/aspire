"""Stage primary evidence for the bounded, read-only PR validation trial."""

import base64
import json
import os
from pathlib import Path
import subprocess

ROOT = Path("/tmp/gh-aw/pr21428-validation")
ROOT.mkdir(parents=True, exist_ok=True)
context = json.loads(Path(".github/scripts/pr21428-validation-context.json").read_text())
if os.environ["STATE_REVISION"] != context["state_revision"]:
    raise ValueError("Dispatch revision does not match the committed trial context")


def api(endpoint, accept=None):
    command = ["gh", "api", "--allow-escape-sequences", endpoint]
    if accept:
        command.extend(["-H", f"Accept: {accept}"])
    return subprocess.run(command, capture_output=True, check=True, timeout=90).stdout


repo = "repos/microsoft/aspire"
pr = json.loads(api(f"{repo}/pulls/21428"))
if pr["head"]["sha"] != context["after"]["source_parent"]:
    raise ValueError("PR head changed; do not claim current-head verification")
(ROOT / "pr.json").write_text(json.dumps(pr, indent=2))
(ROOT / "pr.diff").write_bytes(api(f"{repo}/pulls/21428", "application/vnd.github.diff"))
source_path = "tests/Aspire.Hosting.RemoteHost.Tests/IntegrationHostLauncherTests.cs"
for case in ("before", "after"):
    pinned = context[case]
    ref = json.loads(api(f"{repo}/git/ref/heads/{pinned['branch']}"))
    if ref["object"]["sha"] != pinned["sha"]:
        raise ValueError(f"{case} validation branch moved")
    for label, path in (("source.cs", source_path),
                        ("workflow.yml", ".github/workflows/reproduce-flaky-tests.yml")):
        blob = json.loads(api(f"{repo}/contents/{path}?ref={pinned['sha']}"))
        (ROOT / f"{case}-{label}").write_bytes(base64.b64decode(blob["content"]))
for owned in context["runs"]:
    run = json.loads(api(f"{repo}/actions/runs/{owned['id']}"))
    expected = context[owned["case"]]
    if not (run["head_sha"] == expected["sha"] and run["head_branch"] == expected["branch"]
            and run["event"] == "workflow_dispatch" and run["status"] == "completed"
            and run["workflow_id"] == 238676131 and run["run_attempt"] == 1):
        raise ValueError("Foreign, incomplete, rerun or unpinned test evidence")
    directory = ROOT / str(owned["id"])
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(run, indent=2))
    jobs = json.loads(api(f"{repo}/actions/runs/{owned['id']}/jobs?per_page=100"))
    (directory / "jobs.json").write_text(json.dumps(jobs, indent=2))
    for job in jobs["jobs"]:
        if job["name"].startswith("windows-latest #"):
            (directory / f"job-{job['id']}.log").write_bytes(
                api(f"{repo}/actions/jobs/{job['id']}/logs"))
    artifacts = api(f"{repo}/actions/runs/{owned['id']}/artifacts?per_page=100")
    (directory / "artifacts.json").write_bytes(artifacts)
(ROOT / "context.json").write_text(json.dumps(context, indent=2))
