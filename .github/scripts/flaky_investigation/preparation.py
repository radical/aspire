"""Observed preparation inputs, distinct from model-sandbox prerequisites."""

import json
import platform
import re
import shutil
import subprocess

from contracts import REPOSITORY, require
from reporting import diagnostic


def prepare_inputs(github, source, issue, comments, output):
    settings = json.loads((source / "global.json").read_text(encoding="utf-8"))
    expected = settings["sdk"]["version"]
    environment = {
        "scope": "pre-agent preparation host, not the model sandbox; recheck inside the model Bash tool",
        "os": platform.system() + " " + platform.machine(), "expected_sdk": expected,
        "sdk_source": "global.json", "restore_attempted": False,
        "restore_feasibility": "Not established here. Probe the sandbox, then try bounded repository restore "
                               "within its existing network policy; do not assume Docker is required.",
    }
    executable = shutil.which("dotnet")
    if executable:
        try:
            result = subprocess.run([executable, "--list-sdks"], cwd=source,
                                    capture_output=True, text=True, timeout=15, check=False)
            environment.update(command=[executable, "--list-sdks"], exit_code=result.returncode,
                               output=diagnostic(result.stdout + result.stderr),
                               exact_sdk_available=result.returncode == 0 and any(
                                   line.split()[0] == expected for line in result.stdout.splitlines() if line.split()))
        except (OSError, subprocess.SubprocessError) as error:
            environment.update(command=[executable, "--list-sdks"], error=diagnostic(error),
                               exact_sdk_available=False)
    else:
        environment.update(error="dotnet was not found on the preparation host PATH", exact_sdk_available=False)
    issue_text = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
    sources = []
    for path in sorted((source / "tests").rglob("*.cs")):
        content = path.read_text(encoding="utf-8-sig")
        methods = re.findall(r"\bpublic\s+(?:(?:async|static|override|virtual)\s+)*(?:Task|ValueTask|void)\s+(\w+)\s*\(",
                             content)
        if issue["html_url"] in content or any(
                re.search(r"\b" + re.escape(method) + r"\b", issue_text) for method in methods):
            sources.append({"path": path.relative_to(source).as_posix(), "content": content[:64000],
                            "truncated": len(content) > 64000})
            if len(sources) == 5:
                break
    records = []
    combined = issue_text + "\n" + "\n".join(comment.get("body") or "" for comment in comments)
    links = sorted(set(re.findall(
        r"https://github\.com/" + re.escape(REPOSITORY) + r"/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)",
        combined)))[:2]
    for run_id, job_id in links:
        record = {"run_id": int(run_id), "job_id": int(job_id),
                  "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/job/{job_id}"}
        try:
            job = github.api(f"actions/jobs/{job_id}", timeout=20)
            require(job.get("run_id") == int(run_id), "Historical job does not belong to its linked run")
            logs = github.api(f"actions/jobs/{job_id}/logs", binary=True, timeout=30)
            require(isinstance(logs, bytes), "Historical logs are not original bytes")
            relative = f"historical/job-{job_id}.log"
            destination = output / relative
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(logs[:1000000])
            record.update(path=relative, bytes_retained=min(len(logs), 1000000),
                          truncated=len(logs) > 1000000, conclusion=job.get("conclusion"))
        except (ValueError, KeyError, subprocess.SubprocessError) as error:
            record["unavailable"] = diagnostic(error)
        records.append(record)
    result = {
        "environment": environment, "candidate_test_sources": sources,
        "source_discovery": "Literal issue/method matches only, not a validated selection. "
                            "Read complete source, project setup and referenced fixtures before deciding prerequisites.",
        "historical_jobs": records,
        "history_limit": "At most two exact same-repository job links are fetched. Run-only links and other "
                         "historical jobs still require targeted inspection by the agent.",
    }
    (output / "preparation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
