"""Fork-only issue lifecycle. Candidate jobs are read-only; state uses Git CAS."""

import base64
from datetime import datetime, timezone
import difflib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


REPOSITORY = "radical/aspire"
CONTROLLER = ".github/workflows/flaky-issue-controller.yml"
AGENT = ".github/workflows/flaky-issue-agent.lock.yml"
WORKER = ".github/workflows/flaky-issue-worker.yml"
ROOT = Path("/tmp/gh-aw/issue-proof")
LABEL = "flaky-agent-proof"
TERMINAL = {"verified", "not-reproduced", "blocked", "verification-failed", "cancelled"}
PROBE = "        // Retry-path probe: preserve the failing baseline behavior."
TRAILER = "\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>\n"
CATALOG = {
    "openai-controlled": {
        "base": "aa080c329e164aad343b6b7390d5665f47499db0",
        "base_branch": "flaky-openai-controlled-baseline-20261021",
        "source": "tests/Aspire.Hosting.OpenAI.Tests/OpenAIFunctionalTests.cs",
        "project": "tests/Aspire.Hosting.OpenAI.Tests/Aspire.Hosting.OpenAI.Tests.csproj",
        "class": "Aspire.Hosting.OpenAI.Tests.OpenAIFunctionalTests",
        "method": "DependentResourceWaitsForOpenAIModelResourceWithHealthCheckToBeHealthy",
        "induced_failure": True,
    },
    "hosting-json": {
        "base": "8bb44fe03358922580990578b2040d7e2b00fa1b",
        "source": "tests/Aspire.Hosting.Tests/JsonFlattenerTests.cs",
        "project": "tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj",
        "class": "Aspire.Hosting.Tests.JsonFlattenerTests",
        "method": "FlattenJsonObject_HandlesEmptyObject",
        "induced_failure": False,
    },
}
os.environ["REPAIR_TARGET"] = "openai-controlled"
spec = importlib.util.spec_from_file_location(
    "issue_repair", Path(__file__).with_name("flaky-repair-proof.py")
)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def api(endpoint, body=None, method=None):
    command = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
    if body is not None:
        command += ["--method", method or "POST", "--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(body) if body is not None else None,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


def optional(endpoint):
    try:
        return api(endpoint)
    except subprocess.CalledProcessError as error:
        if "HTTP 404" not in error.stderr:
            raise
        return None


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,12}", value):
        raise ValueError("Expected a positive issue/run number")
    return value


def bounded(value, maximum, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Integer must be between {minimum} and {maximum}")
    return value


def safe_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 8000:
        raise ValueError("A bounded nonempty explanation is required")
    if re.search(r"gh[pousr]_[A-Za-z0-9]{15}|github_pat_|[?&](?:sig|signature|token)=", value):
        raise ValueError("Refusing credential-shaped publication content")
    return value


def configuration(issue):
    if (
        issue.get("state") != "open" or "pull_request" in issue
        or issue["user"]["login"] != "radical"
        or LABEL not in {label["name"] for label in issue["labels"]}
    ):
        raise ValueError("Issue is not an enrolled, owner-authored investigation")
    body = issue.get("body") or ""
    # Owner-reviewed issue contract, for example:
    # ```json
    # {"version":1,"target":"hosting-json", ... bounded execution policy ...}
    # ```
    matches = re.findall(r"```json\n(.*?)\n```", body, re.DOTALL)
    if len(matches) != 1 or len(body) > 12000:
        raise ValueError("Issue requires exactly one bounded JSON contract")
    value = json.loads(matches[0])
    if not isinstance(value, dict) or set(value) != {
        "version", "target", "max_rounds", "max_fix_attempts", "max_executions",
        "retry_probe", "resume_delay_seconds", "cancel_probe",
    } or type(value["version"]) is not int or value["version"] != 1 or not isinstance(value["target"], str) or value["target"] not in CATALOG:
        raise ValueError("Unknown issue contract or non-allowlisted target")
    for key, maximum in (("max_rounds", 3), ("max_fix_attempts", 3), ("max_executions", 20)):
        bounded(value[key], maximum)
    bounded(value["resume_delay_seconds"], 120, 0)
    if type(value["retry_probe"]) is not bool or type(value["cancel_probe"]) is not bool:
        raise ValueError("Probe flags must be booleans")
    if value["retry_probe"] and value["target"] != "openai-controlled":
        raise ValueError("Retry probe requires the frozen controlled baseline")
    return value


def configure(issue):
    config = configuration(issue)
    policy = CATALOG[config["target"]]
    repair.BASE = policy["base"]
    repair.SOURCE = policy["source"]
    repair.AGENT = AGENT
    repair.WORKER = WORKER
    repair.RUNNER_OS = "ubuntu-latest"
    repair.ROOT = ROOT
    repair.real.CLASS = policy["class"]
    repair.real.METHOD = policy["method"]
    repair.worker_identity = worker_identity
    return config, policy


def branch(issue_id):
    return f"memory/flaky-issue-controller-{issue_id}"


def load(issue_id, revision=None):
    if revision is None:
        reference = optional(f"git/ref/heads/{branch(issue_id)}")
        if reference is None:
            return None, None
        revision = reference["object"]["sha"]
    document = api(f"contents/state.json?ref={revision}")
    state = json.loads(base64.b64decode(document["content"]))
    if state.get("version") != 1 or str(state.get("issue_number")) != issue_id:
        raise ValueError("Checkpoint belongs to another issue or schema")
    return state, revision


def save(state, previous):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    blob = api("git/blobs", {"content": json.dumps(state, indent=2), "encoding": "utf-8"})
    tree = api("git/trees", {"tree": [
        {"path": "state.json", "mode": "100644", "type": "blob", "sha": blob["sha"]},
    ]})
    commit = api("git/commits", {
        "message": f"ci: checkpoint issue {state['issue_number']} ({state['phase']}){TRAILER}",
        "tree": tree["sha"], "parents": [previous] if previous else [],
    })
    ref = branch(str(state["issue_number"]))
    if previous:
        # A concurrent writer creates a sibling commit, so force:false rejects it.
        api(f"git/refs/heads/{ref}", {"sha": commit["sha"], "force": False}, "PATCH")
    else:
        api("git/refs", {"ref": f"refs/heads/{ref}", "sha": commit["sha"]})
    return commit["sha"]


def write(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / name).write_text(json.dumps(value, indent=2) + "\n")


def dispatch(path, inputs):
    # gh CLI v2.102.0's workflow/run implementation documents this REST field.
    # Capture the server's run ID instead of guessing from the most recent run.
    result = api(f"actions/workflows/{Path(path).name}/dispatches", {
        "ref": "main", "inputs": inputs, "return_run_details": True,
    })
    if not result or not isinstance(result.get("workflow_run_id"), int):
        raise ValueError("Dispatch lacks a run-ID receipt; do not blindly dispatch again")
    return str(result["workflow_run_id"])


def run_identity(run_id, path):
    run = repair.run_identity(number(str(run_id)), path)
    if run["actor"]["login"] not in {"radical", "github-actions[bot]"}:
        raise ValueError("Unexpected workflow initiator")
    return run


def worker_identity(run_id):
    run = run_identity(run_id, WORKER)
    state, _ = load(number(os.environ["ISSUE_NUMBER"]))
    request = state["worker"]
    expected = (
        f"Issue worker #{state['issue_number']} {request['phase']} "
        f"[{request['parent_run_id']}] {request['candidate_sha']}"
    )
    if run["display_title"] != expected or str(request["run_id"]) != str(run_id):
        raise ValueError("Worker identity does not match the owned dispatch")
    return run, request["phase"], request["parent_run_id"], request["candidate_sha"]


def agent_dispatch(state, revision):
    if len(state["agents"]) >= 10:
        raise ValueError("Investigation exceeded the ten-agent budget")
    state["phase"] = "dispatching-agent"
    intent = save(state, revision)
    agent_id = dispatch(AGENT, {
        "issue_number": str(state["issue_number"]), "state_revision": intent,
    })
    state.update(phase="awaiting-agent", agent_run_id=agent_id, agent_revision=intent)
    state["agents"].append(agent_id)
    revision = save(state, intent)
    # A failed model may never emit a safe-output callback. The trusted job
    # watches its owned agent, not CI, so failure still produces finite findings.
    # A successful callback queued behind this job becomes a harmless duplicate.
    repair.real.wait_completed(agent_id, True)
    handle_agent(state, revision, agent_id)
    return revision


def worker_request(state, phase, candidate, iterations, runners):
    bounded(iterations, 3)
    bounded(runners, 3)
    executions = 0 if phase == "cancel-probe" else iterations * runners
    if state["requested_executions"] + executions > state["config"]["max_executions"]:
        raise ValueError("Requested tests exceed the remaining issue execution budget")
    return {
        "phase": phase, "candidate_sha": candidate,
        "parent_run_id": os.environ["GITHUB_RUN_ID"],
        "configuration": {
            "iterations_per_runner": iterations, "runners": runners, "quarantine_project": False,
        },
        "reserved_executions": executions,
    }


def worker_dispatch(state, revision, request):
    state["phase"] = "dispatching-worker"
    state["worker"] = request
    state["requested_executions"] += request["reserved_executions"]
    intent = save(state, revision)
    worker_id = dispatch(WORKER, {
        "issue_number": str(state["issue_number"]), "phase": request["phase"],
        "candidate_sha": request["candidate_sha"], "parent_run_id": request["parent_run_id"],
        "iterations": str(request["configuration"]["iterations_per_runner"]),
        "runners": str(request["configuration"]["runners"]),
    })
    state["worker"]["run_id"] = worker_id
    state["phase"] = "awaiting-worker"
    revision = save(state, intent)
    if request["phase"] == "cancel-probe":
        # This probe owns a disposable sleeping job, never somebody else's CI.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            jobs = api(f"actions/runs/{worker_id}/jobs?per_page=100")["jobs"]
            if any(j["name"] == "cancel_probe" and j["status"] == "in_progress" for j in jobs):
                break
            time.sleep(5)
        else:
            raise ValueError("Owned cancellation probe did not start")
        state["stage"] = "awaiting-cancellation"
        agent_dispatch(state, revision)


def candidate_source(state):
    sha = state.get("candidate", {}).get("candidate_sha", repair.BASE)
    document = api(f"contents/{repair.SOURCE}?ref={sha}")
    return sha, document["sha"], base64.b64decode(document["content"]).decode()


def prepare(issue_id, revision):
    issue = api(f"issues/{issue_id}")
    configure(issue)
    snapshot, _ = load(issue_id, revision)
    live, live_revision = load(issue_id)
    if snapshot is None or live is None or snapshot["phase"] != "dispatching-agent":
        raise ValueError("Agent requires an immutable dispatch intent")
    if live.get("agent_revision", live_revision) != revision or live["phase"] in TERMINAL:
        raise ValueError("Agent dispatch intent is stale")
    if snapshot["issue_body"] != issue["body"]:
        raise ValueError("Issue selection changed after enrollment")
    sha, blob, source = candidate_source(snapshot)
    context = {
        **snapshot, "state_revision": revision, "actual_source_sha": sha,
        "actual_source_blob": blob, "test": f"{repair.real.CLASS}.{repair.real.METHOD}",
        "remaining_executions": snapshot["config"]["max_executions"] - snapshot["requested_executions"],
    }
    if snapshot.get("worker") and snapshot["stage"] in {"baseline", "verification"}:
        worker_id = snapshot["worker"]["run_id"]
        context["receipt"] = repair.collect_worker(worker_id, snapshot["worker"]["configuration"])
        write("receipt.json", context["receipt"])
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "test-source.cs").write_text(source)
    original = repair.source_at(repair.BASE)
    (ROOT / "candidate.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(True), source.splitlines(True), fromfile="baseline", tofile="candidate",
    )))
    write("context.json", context)


def proposal_for(state, agent_id):
    run = run_identity(agent_id, AGENT)
    expected = f"Issue investigator #{state['issue_number']} [{state['agent_revision']}]"
    if run["display_title"] != expected or state["agent_run_id"] != agent_id:
        raise ValueError("Agent callback does not own this checkpoint")
    repair.real.wait_completed(agent_id, True)
    directory = ROOT / f"agent-{agent_id}"
    repair.download(agent_id, "repo-memory-default", directory)
    path = directory / "proposal.json"
    if not path.is_file():
        raise ValueError("Agent completed without a proposal artifact")
    if path.stat().st_size > 50000:
        raise ValueError("Agent proposal is oversized")
    proposal = json.loads(path.read_text())
    validate_proposal(state, proposal)
    return proposal


def validate_proposal(state, proposal):
    if (
        type(proposal.get("version")) is not int or proposal.get("version") != 1
        or proposal.get("issue_number") != state["issue_number"]
        or proposal.get("state_revision") != state["agent_revision"]
        or proposal.get("action") not in {"reproduce", "publish", "finish", "cancel-probe", "cancel"}
    ):
        raise ValueError("Proposal identity, revision or action is invalid")
    safe_text(proposal.get("explanation"))
    paths = proposal.get("evidence_read")
    if not isinstance(paths, list) or not paths or len(paths) > 40 or any(
        not isinstance(path, str) or not 1 <= len(path) <= 500 for path in paths
    ):
        raise ValueError("Proposal must describe actual inspected evidence")


def publish(state, revision, proposal):
    if state["config"]["target"] != "openai-controlled":
        raise ValueError("Passing smoke target is not authorized for repair publication")
    attempt = len(state["candidates"]) + 1
    if attempt > state["config"]["max_fix_attempts"]:
        raise ValueError("Candidate attempt budget exhausted")
    baseline = next((item for item in state["workers"] if (
        item["run_id"] == str(proposal.get("baseline_run_id"))
        and item["phase"] == "reproduce" and item.get("failed", 0) > 0
        and any(repair.matching_failure(x.get("failure") or "") for x in item["iterations"])
    )), None)
    if baseline is None:
        raise ValueError("Publication requires actual matching failed baseline evidence")
    original = repair.source_at(repair.BASE)
    source = repair.validate_candidate(proposal, original)
    marker = f"    public async Task {repair.real.METHOD}()\n    {{"
    is_probe = state["config"]["retry_probe"] and attempt == 1
    if is_probe and source != original.replace(marker, marker + "\n" + PROBE, 1):
        raise ValueError("First retry probe must preserve the exact failing baseline behavior")
    if not is_probe and PROBE in source:
        raise ValueError("Final repair must not retain the ineffective probe marker")
    if state["config"]["retry_probe"] and attempt > 1 and not state.get("failed_probe_observed"):
        raise ValueError("No actual failed candidate probe was inspected")
    # Reserve the full verification budget before creating any candidate ref.
    request = worker_request(state, "verify", "pending", 1 if is_probe else 3, 1 if is_probe else 3)
    blob = api("git/blobs", {"content": source, "encoding": "utf-8"})
    base = api(f"git/commits/{repair.BASE}")
    tree = api("git/trees", {"base_tree": base["tree"]["sha"], "tree": [
        {"path": repair.SOURCE, "mode": "100644", "type": "blob", "sha": blob["sha"]},
    ]})
    subject = (
        "test: exercise an intentionally ineffective repair candidate"
        if is_probe else "test: isolate OpenAI model readiness from external status"
    )
    message = subject + "\n\n" + (
        "Fork-only retry-path probe. Preserve the controlled failing baseline;\n"
        "this is deliberately not a repair.\n"
        if is_probe else
        "The inherited external status check can remain Degraded after the\n"
        "controlled readiness check is Healthy. Isolate the test contract\n"
        "without weakening its waits. This is an induced-failure proof only.\n"
        "Keep the quarantine marker and fixed outage fixture unchanged.\n"
    ) + TRAILER
    commit = api("git/commits", {"message": message, "tree": tree["sha"], "parents": [repair.BASE]})
    name = f"flaky-issue-{state['issue_number']}-candidate-{attempt}"
    api("git/refs", {"ref": f"refs/heads/{name}", "sha": commit["sha"]})
    candidate = {
        "attempt": attempt, "candidate_sha": commit["sha"], "source_blob": blob["sha"],
        "branch": name, "is_probe": is_probe, "diagnosis": proposal["diagnosis"],
    }
    state["candidate"] = candidate
    state["candidates"].append(candidate)
    request["candidate_sha"] = commit["sha"]
    worker_dispatch(state, revision, request)


def complete_evidence(receipt):
    expected = receipt["iterations_per_runner"] * receipt["runners"]
    return (
        not receipt.get("evidence_error")
        and receipt.get("executed") == expected
        and len(receipt.get("iterations", [])) == expected
        and not receipt.get("other_failed", 0)
    )


def require_verified(state, proposal):
    candidate = state.get("candidate")
    receipt = state["workers"][-1] if state["workers"] else {}
    if (
        not candidate or candidate["is_probe"]
        or receipt.get("phase") != "verify"
        or receipt.get("candidate_sha") != candidate["candidate_sha"]
        or not complete_evidence(receipt)
        or receipt["executed"] != 9 or receipt["failed"] != 0
        or proposal.get("reviewed_source_blob") != candidate["source_blob"]
    ):
        raise ValueError("PR requires complete actual nine-pass evidence and published-source review")
    sha, blob, source = candidate_source(state)
    if sha != candidate["candidate_sha"] or blob != candidate["source_blob"]:
        raise ValueError("Published candidate source changed")
    repair.validate_candidate({
        "base_sha": repair.BASE, "test_source": source, "diagnosis": candidate["diagnosis"],
    }, repair.source_at(repair.BASE))
    comparison = api(f"compare/{repair.BASE}...{sha}")
    if [(f["filename"], f["status"]) for f in comparison["files"]] != [(repair.SOURCE, "modified")]:
        raise ValueError("PR candidate includes unauthorized changes")


def pull_request(state):
    candidate = state["candidate"]
    policy = CATALOG[state["config"]["target"]]
    base = api(f"git/ref/heads/{policy['base_branch']}")["object"]["sha"]
    if base != policy["base"]:
        raise ValueError("Controlled proof PR base moved")
    title = "[automated] test: isolate model readiness from external status (proof only)"
    description = (
        "**Fork-only induced-failure proof; do not merge into upstream.**\n\n"
        "The model-readiness test times out when its inherited external status\n"
        "check remains Degraded after the controlled check becomes Healthy.\n"
        "Keep the parent builder and remove its external health annotation so\n"
        "the test measures the intended readiness contract.\n\n"
        "The fixed outage helper, original waits and quarantine remain unchanged.\n"
        "The PR targets the frozen fixture branch, not upstream main.\n\n"
        "### Validation\n\n"
        f"Exact published source: `{candidate['candidate_sha']}`.\n\n"
        "Three Linux workers ran three iterations each: **9 passed, 0 failed**.\n"
        "The selected command on each runner was:\n\n"
        "```bash\n./dotnet.sh test --project " + policy["project"] + " \\\n"
        "  --no-restore --no-build -- --timeout 6m \\\n"
        "  --results-directory <iteration-directory> \\\n"
        "  --report-trx --report-trx-filename real-proof.trx \\\n"
        "  --filter-method " + policy["class"] + "." + policy["method"] + " \\\n"
        "  --filter-not-trait outerloop=true\n```\n\n"
        f"Verification: https://github.com/{REPOSITORY}/actions/runs/{state['workers'][-1]['run_id']}\n\n"
        "This does not establish natural flake elimination or unquarantine.\n"
        "Human author walkthrough and production security review are pending.\n"
    )
    template = Path(__file__).resolve().parents[1] / "pull_request_template.md"
    body = template.read_text().replace(
        "Please include a summary of the changes and the related issue. Please also include relevant motivation and context. List any dependencies that are required for this change.",
        description,
    ).replace("Fixes # (issue)", f"Refs {REPOSITORY}#{state['issue_number']}")
    body = body.replace("[ ] No. Follow-up changes expected.", "[x] No. Follow-up changes expected.")
    # Existing template questions are answered in their original order.
    sections = body.split("- Are you including unit tests")
    sections[1] = sections[1].replace("  - [ ] Yes\n", "  - [x] Yes\n", 1)
    body = "- Are you including unit tests".join(sections)
    sections = body.split("- Did you add public API?")
    sections[1] = sections[1].replace("  - [ ] No\n- Does", "  - [x] No\n- Does", 1)
    body = "- Did you add public API?".join(sections)
    position = body.rfind("  - [ ] No")
    body = body[:position] + body[position:].replace("  - [ ] No", "  - [x] No", 1)
    write("pull-request-preview.json", {"title": title, "body": body, "draft": True})
    print("DRAFT PR PREVIEW\n" + title + "\n" + body, flush=True)
    existing = api(
        f"pulls?state=open&head=radical%3A{candidate['branch']}&base={policy['base_branch']}"
    )
    if existing:
        if len(existing) != 1 or existing[0]["head"]["sha"] != candidate["candidate_sha"]:
            raise ValueError("Conflicting existing PR")
        return existing[0]["html_url"]
    return api("pulls", {
        "title": title, "body": body, "head": candidate["branch"],
        "base": policy["base_branch"], "draft": True, "maintainer_can_modify": False,
    })["html_url"]


def terminal_comment(state):
    marker = f"<!-- flaky-issue-result:{state['issue_number']} -->"
    lines = [
        f"[automated] **Investigation finished: {state['phase']}.**", "",
        safe_text(state["explanation"]), "",
        f"Target: `{repair.real.CLASS}.{repair.real.METHOD}`.",
        f"Actual test executions: {sum(w.get('executed', 0) for w in state['workers'])}; "
        f"failed: {sum(w.get('failed', 0) for w in state['workers'])}.",
        f"Reserved execution budget: {state['requested_executions']}/{state['config']['max_executions']}; "
        f"candidate attempts: {len(state['candidates'])}/{state['config']['max_fix_attempts']}.",
        "", "### Evidence",
    ]
    for worker in state["workers"]:
        lines.append(
            f"- {worker['phase']}: https://github.com/{REPOSITORY}/actions/runs/{worker['run_id']} "
            f"(executed={worker.get('executed', 0)}, failed={worker.get('failed', 0)}, "
            f"source `{worker['candidate_sha']}`)."
        )
    for agent_id in state["agents"]:
        lines.append(f"- Fresh agent: https://github.com/{REPOSITORY}/actions/runs/{agent_id}.")
    if state.get("pull_request"):
        lines += ["", f"Draft PR: {state['pull_request']}"]
    if state.get("cancelled_run"):
        lines += ["", f"Owned cancellation probe: https://github.com/{REPOSITORY}/actions/runs/{state['cancelled_run']}."]
    lines += [
        "", "### Limitations",
        "Fork-only capability proof. Controlled failures are induced, not natural flake reproduction.",
        "Passing executions do not prove permanent flake elimination.",
        "No upstream deployment, unquarantine or issue closure was performed.",
        marker,
    ]
    body = "\n".join(lines)
    write("issue-comment-preview.json", {"body": body})
    print("ISSUE FINDINGS PREVIEW\n" + body, flush=True)
    existing = api(f"issues/{state['issue_number']}/comments?per_page=100")
    if any(marker in (comment.get("body") or "") for comment in existing):
        return
    api(f"issues/{state['issue_number']}/comments", {"body": body})


def finish(state, revision, outcome, explanation, proposal=None):
    if outcome not in TERMINAL:
        raise ValueError("Unknown terminal outcome")
    if outcome == "verified":
        require_verified(state, proposal or {})
        state["pull_request"] = pull_request(state)
    if outcome == "not-reproduced" and (
        not state["workers"] or any(
            not complete_evidence(item) or item["phase"] != "reproduce" or item["failed"] != 0
            for item in state["workers"]
        )
    ):
        raise ValueError("Non-reproduction requires complete passing baseline evidence")
    state.update(phase=outcome, explanation=safe_text(explanation))
    revision = save(state, revision)
    terminal_comment(state)
    return revision


def collect(state, revision, worker_id):
    if state["phase"] != "awaiting-worker" or state["worker"]["run_id"] != worker_id:
        print("Ignoring a duplicate or stale worker callback", flush=True)
        return
    worker_identity(worker_id)
    receipt = repair.collect_worker(worker_id, state["worker"]["configuration"])
    write("receipt.json", receipt)
    state["workers"].append(receipt)
    if not complete_evidence(receipt):
        finish(state, revision, "blocked", "Worker evidence is incomplete or contains unrelated failures. " + receipt.get("evidence_error", "Inspect the linked build and runner logs."))
        return
    if receipt["phase"] == "verify" and state["candidate"]["is_probe"]:
        if receipt["failed"] != 1 or not any(
            repair.matching_failure(item.get("failure") or "") for item in receipt["iterations"]
        ):
            raise ValueError("The deliberately ineffective candidate did not retain its expected failure")
        state["failed_probe_observed"] = True
    state["stage"] = "baseline" if receipt["phase"] == "reproduce" else "verification"
    state["phase"] = "evidence-ready"
    if not state.get("delay_exercised") and state["config"]["resume_delay_seconds"]:
        state["phase"] = "paused"
        state["resume_after"] = time.time() + state["config"]["resume_delay_seconds"]
        state["delay_exercised"] = True
        revision = save(state, revision)
        time.sleep(max(0, state["resume_after"] - time.time()))
        state["resumed_at"] = time.time()
    agent_dispatch(state, revision)


def handle_agent(state, revision, agent_id):
    if state["phase"] != "awaiting-agent" or state["agent_run_id"] != agent_id:
        print("Ignoring a duplicate or stale agent callback", flush=True)
        return
    proposal = proposal_for(state, agent_id)
    write("proposal.json", proposal)
    action = proposal["action"]
    if action == "reproduce":
        if len([w for w in state["workers"] if w["phase"] == "reproduce"]) >= state["config"]["max_rounds"]:
            raise ValueError("Baseline round budget exhausted")
        if state["candidates"]:
            raise ValueError("Cannot return to baseline after candidate publication")
        request = worker_request(
            state, "reproduce", repair.BASE, proposal.get("iterations"), proposal.get("runners")
        )
        worker_dispatch(state, revision, request)
    elif action == "publish":
        publish(state, revision, proposal)
    elif action == "finish":
        finish(state, revision, proposal.get("outcome"), proposal["explanation"], proposal)
    elif action == "cancel-probe":
        if not state["config"]["cancel_probe"] or state.get("worker"):
            raise ValueError("Cancellation probe is not authorized or already exists")
        worker_dispatch(state, revision, worker_request(state, "cancel-probe", repair.BASE, 1, 1))
    elif action == "cancel":
        if (
            state["stage"] != "awaiting-cancellation"
            or str(proposal.get("worker_run_id")) != state["worker"]["run_id"]
            or state["worker"]["phase"] != "cancel-probe"
        ):
            raise ValueError("Cannot cancel an unrelated run")
        worker_id = state["worker"]["run_id"]
        run, _, _, _ = worker_identity(worker_id)
        if run["status"] != "completed":
            api(f"actions/runs/{worker_id}/cancel", {})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            run = api(f"actions/runs/{worker_id}")
            if run["status"] == "completed":
                break
            time.sleep(5)
        if run["conclusion"] != "cancelled":
            raise ValueError("Owned run was not actually cancelled")
        state["cancelled_run"] = worker_id
        finish(state, revision, "cancelled", proposal["explanation"])


def advance(issue_id, event):
    issue = api(f"issues/{issue_id}")
    configure(issue)
    state, revision = load(issue_id)
    if state and state["phase"] in TERMINAL:
        terminal_comment(state)
        print("Investigation is terminal; no new agent or worker dispatch", flush=True)
        return
    if state and issue["body"] != state["issue_body"]:
        raise ValueError("Issue contract changed after investigation started")
    if event == "intake":
        if state:
            print("Issue already enrolled; no duplicate investigation", flush=True)
            return
        state = {
            "version": 1, "issue_number": int(issue_id), "issue_body": issue["body"],
            "config": configuration(issue), "phase": "new", "stage": "start",
            "agents": [], "workers": [], "candidates": [], "requested_executions": 0,
            "enrollment_run_id": os.environ["GITHUB_RUN_ID"],
        }
        agent_dispatch(state, None)
    elif state is None:
        raise ValueError("No enrolled issue checkpoint")
    elif event == "agent":
        handle_agent(state, revision, number(os.environ["AGENT_RUN_ID"]))
    elif event == "worker":
        collect(state, revision, number(os.environ["WORKER_RUN_ID"]))
    elif event == "resume":
        if state["phase"] == "paused":
            time.sleep(max(0, state["resume_after"] - time.time()))
            state["resumed_at"] = time.time()
            agent_dispatch(state, revision)
        elif state["phase"] == "awaiting-worker":
            collect(state, revision, state["worker"]["run_id"])
        elif state["phase"] == "awaiting-agent":
            handle_agent(state, revision, state["agent_run_id"])
        else:
            raise ValueError("Unreceipted dispatch intent requires inspection; never blindly retry")
    else:
        raise ValueError("Unsupported controller event")


def worker_state(issue_id):
    configure(api(f"issues/{issue_id}"))
    state, _ = load(issue_id)
    if state["phase"] not in {"dispatching-worker", "awaiting-worker", "awaiting-agent"}:
        raise ValueError("Worker has no live dispatch authorization")
    if state["worker"].get("run_id") not in {None, os.environ["GITHUB_RUN_ID"]}:
        raise ValueError("A different run owns this worker dispatch")
    return state, state["worker"]


def matrix(issue_id):
    state, request = worker_state(issue_id)
    run = run_identity(os.environ["GITHUB_RUN_ID"], WORKER)
    expected = (
        f"Issue worker #{issue_id} {request['phase']} "
        f"[{request['parent_run_id']}] {request['candidate_sha']}"
    )
    if run["display_title"] != expected:
        raise ValueError("Worker inputs differ from trusted dispatch intent")
    count = request["configuration"]["runners"]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("matrix=" + json.dumps({"include": [{"index": i} for i in range(1, count + 1)]}) + "\n")
        # Arcade resolves -projects from Build.proj, not this shell's directory.
        project = Path(os.environ["GITHUB_WORKSPACE"]) / "candidate" / CATALOG[state["config"]["target"]]["project"]
        output.write("project=" + str(project) + "\n")


def checkout_receipt(issue_id):
    state, request = worker_state(issue_id)
    actual = subprocess.check_output(
        ["git", "--no-pager", "rev-parse", "HEAD"], cwd="candidate", text=True,
    ).strip()
    if actual != request["candidate_sha"]:
        raise ValueError("Actual candidate checkout differs from authorization")
    index = bounded(int(os.environ["RUNNER_INDEX"]), request["configuration"]["runners"])
    receipt = {
        "candidate_sha": actual, "workflow_sha": os.environ["GITHUB_SHA"],
        "phase": request["phase"], "controller_run_id": request["parent_run_id"],
        "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "runner_index": index, **request["configuration"],
    }
    Path("candidate-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def execute(issue_id):
    state, request = worker_state(issue_id)
    policy = CATALOG[state["config"]["target"]]
    root = Path("worker-evidence").resolve()
    results = []
    for iteration in range(1, request["configuration"]["iterations_per_runner"] + 1):
        folder = root / f"iteration-{iteration}"
        folder.mkdir(parents=True)
        command = [
            "./dotnet.sh", "test", "--project", policy["project"], "--no-restore", "--no-build",
            "--", "--timeout", "6m", "--results-directory", str(folder),
            "--report-trx", "--report-trx-filename", "real-proof.trx",
            "--filter-method", f"{policy['class']}.{policy['method']}",
            "--filter-not-trait", "outerloop=true",
        ]
        with (folder / "output.log").open("wb") as output:
            result = subprocess.run(command, cwd="candidate", stdout=output, stderr=subprocess.STDOUT)
        print((folder / "output.log").read_text(errors="replace"), flush=True)
        results.append(result.returncode)
    observations = repair.real.validate_evidence(root, len(results))
    if any((code == 0) != (observation["failed"] == 0) for code, observation in zip(results, observations)):
        raise ValueError("Test process exit disagrees with actual target results")
    raise SystemExit(1 if any(results) else 0)


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("This lifecycle proof is restricted to radical/aspire")
    issue_id = number(os.environ["ISSUE_NUMBER"])
    operation = sys.argv[1]
    if operation == "advance":
        try:
            advance(issue_id, os.environ["EVENT"])
        except (ValueError, OSError, KeyError, StopIteration, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Surface the failure on the enrolled issue, but retain failed workflow status.
            state, revision = load(issue_id)
            if state and state["phase"] not in TERMINAL:
                configure(api(f"issues/{issue_id}"))
                finish(state, revision, "blocked", f"Controller stopped: {type(error).__name__}: {error}")
            raise
    elif operation == "prepare":
        prepare(issue_id, os.environ["STATE_REVISION"])
    elif operation == "matrix":
        matrix(issue_id)
    elif operation == "receipt":
        checkout_receipt(issue_id)
    elif operation == "execute":
        execute(issue_id)
    else:
        raise ValueError("Unknown operation")


if __name__ == "__main__":
    main()
