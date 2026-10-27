"""Validation at the boundary between untrusted model proposals and orchestration."""

from datetime import datetime, timezone
from pathlib import PurePosixPath
import os
import re


# This identity comes from the trusted runner, never an issue or proposal.
# Fork validation must use its own issues, runs, refs and API authority.
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "microsoft/aspire")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", REPOSITORY):
    raise ValueError("Invalid trusted runner repository identity")
AGENT = "flaky-test-investigator.lock.yml"
WORKER = "reproduce-flaky-tests.yml"
LIMITS = {"models": 8, "workers": 5, "fixes": 2, "runner_iterations": 180}
TERMINAL = {
    "verified", "not-flaky", "ambiguous", "infrastructure", "not-reproduced",
    "budget-exhausted", "blocked", "cancelled",
}
OSES = {"ubuntu-latest", "windows-latest", "macos-latest"}
SHA = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


class BudgetExceeded(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def require_candidate_isolation():
    # Only the trusted controller/worker configuration selects this policy.
    # Standard Actions isolation and human draft review are not adversarial TRX attestation.
    if os.environ.get("FLAKY_INVESTIGATION_EXECUTION_POLICY") == "actions-draft-review-v1":
        return
    raise ValueError(
        "Automated candidate execution/publication is blocked: an explicit trusted execution "
        "boundary policy is absent or unknown")


def issue_number(url):
    require(isinstance(url, str), "An Aspire issue URL is required")
    match = re.fullmatch(r"https://github\.com/" + re.escape(REPOSITORY) + r"/issues/([1-9][0-9]{0,9})", url)
    require(match is not None, f"Expected an exact {REPOSITORY} issue URL")
    return int(match[1])


def text(value, maximum=8000):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum,
            "Expected bounded nonempty text")
    require(not re.search(r"github_pat_|gh[pousr]_[A-Za-z0-9]{15}|[?&](?:sig|token)=", value),
            "Refusing credential-shaped output")
    return value


def integer(value, maximum, minimum=1):
    require(type(value) is int and minimum <= value <= maximum, "Invalid bounded integer")
    return value


def sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "Expected a full immutable Git SHA")
    return value


def source_path(value):
    require(isinstance(value, str) and re.fullmatch(r"(src|tests)/[A-Za-z0-9_./-]+\.cs", value),
            "Only existing C# source under src/ or tests/ is eligible")
    path = PurePosixPath(value)
    require(".." not in path.parts and "/api/" not in value
            and not value.endswith((".Designer.cs", ".g.cs", ".generated.cs")),
            "Generated or traversal paths are forbidden")
    return value


def selection(proposal, issue, files):
    """files is the immutable source tree, never a model-supplied catalog."""
    require(isinstance(proposal, dict), "Missing test selection")
    source = source_path(proposal.get("source"))
    project = proposal.get("project")
    require(source.startswith("tests/") and source in files, "Test source does not exist")
    require(isinstance(project, str) and re.fullmatch(r"tests/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.csproj", project)
            and project in files and source.startswith(str(PurePosixPath(project).parent) + "/"),
            "Test must belong to the selected existing project directory")
    method = proposal.get("method")
    class_name = proposal.get("class_name")
    require(isinstance(method, str) and IDENTIFIER.fullmatch(method), "Invalid exact method")
    require(isinstance(class_name, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)+", class_name),
            "Invalid fully qualified test class")
    source_text = files[source]
    # Match ordinary C# declarations, not call sites. Unusual generated/nested tests
    # deliberately stop for a human rather than broadening an execution filter.
    declarations = list(re.finditer(
        rf"\bpublic\s+(?:(?:async|static|override|virtual)\s+)*(?:Task(?:<[^>]+>)?|ValueTask|void)\s+{re.escape(method)}\s*\(",
        source_text))
    require(len(declarations) == 1, "Test declaration is missing or ambiguous")
    namespace, _, short_class = class_name.rpartition(".")
    require(re.search(rf"\bnamespace\s+{re.escape(namespace)}\s*[;{{]", source_text)
            and re.search(rf"\bclass\s+{re.escape(short_class)}\b", source_text),
            "Class/namespace does not match source")
    attributes = source_text[max(0, declarations[0].start() - 1500):declarations[0].start()]
    attributes = attributes[attributes.rfind("}") + 1:]
    require(re.search(r"\[(?:Fact|Theory|QuarantinedTest)\b", attributes),
            "Selected declaration is not an ordinary xUnit test")
    issue_url = issue["html_url"]
    require(issue_url in attributes or method in (issue.get("title", "") + "\n" + (issue.get("body") or "")),
            "Issue does not identify this test")
    oses = proposal.get("oses")
    require(isinstance(oses, list) and 1 <= len(oses) <= 3
            and all(isinstance(item, str) and item in OSES for item in oses)
            and len(set(oses)) == len(oses),
            "Unknown or duplicated runner OS")
    signature = text(proposal.get("failure_signature"), 500)
    require(signature in (issue.get("body") or "") or signature in issue.get("title", ""),
            "Failure signature must be a literal excerpt from the issue")
    return {
        "source": source, "project": project, "class_name": class_name,
        "method": method, "oses": oses, "failure_signature": signature,
        "quarantined": re.search(r"\[QuarantinedTest\b", attributes) is not None,
    }


def validate_candidate(files, originals):
    require(isinstance(files, dict) and 1 <= len(files) <= 5, "Candidate requires 1-5 source files")
    require(sum(len(value.encode("utf-8")) for value in files.values() if isinstance(value, str)) <= 250000,
            "Candidate file content exceeds budget")
    changed_lines = 0
    import difflib
    for path, proposed in files.items():
        source_path(path)
        require(path in originals and isinstance(proposed, str) and "\x00" not in proposed,
                "Candidate may only replace existing regular source files")
        text(proposed, 250000)
        original = originals[path]
        require(proposed != original, "Candidate includes an unchanged file")
        # Ignore comments as C# does, retain complete string arguments and
        # declaration ownership, and preserve parallelism/custom attributes too.
        from csharp import scheduling_surface
        require(scheduling_surface(proposed) == scheduling_surface(original),
                "Test scheduling/data attributes and declarations must remain unchanged")
        for line in difflib.ndiff(original.splitlines(), proposed.splitlines()):
            if line.startswith(("+ ", "- ")):
                changed_lines += 1
                require(not re.search(r"\b(?:Skip|SkipWhen|SkipUnless)\s*=", line),
                        "Candidate cannot introduce test skipping")
    require(0 < changed_lines <= 800, "Candidate diff exceeds the 800-line budget")
    return files


def repair_scope(project, projects):
    """Follow explicit repository project references without evaluating MSBuild."""
    import posixpath
    import xml.etree.ElementTree as ET
    pending = [project]
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        require(len(visited) < 150 and current in projects, "Unresolved source project reference")
        visited.add(current)
        directory = posixpath.dirname(current)
        root = ET.fromstring(projects[current])
        for reference in root.iter("ProjectReference"):
            include = reference.get("Include", "")
            # Dynamic/glob references are not permission to edit arbitrary source.
            # Their consumers can still be diagnosed, but need a human repair.
            if any(char in include for char in "$*;"):
                continue
            resolved = posixpath.normpath(posixpath.join(directory, include.replace("\\", "/")))
            if resolved in projects:
                pending.append(resolved)
    return sorted({posixpath.dirname(path) + "/" for path in visited})


def reserve(state, kind, amount=1):
    require(kind in LIMITS, "Unknown budget")
    used = state["used"][kind] + integer(amount, LIMITS[kind])
    if used > LIMITS[kind]:
        raise BudgetExceeded(f"{kind} budget exhausted")
    state["used"][kind] = used


def local_precheck(value):
    require(isinstance(value, dict) and isinstance(value.get("status"), str)
            and value["status"] in {"completed", "skipped"},
            "Local precheck result or explicit environment skip required")
    result = {"status": value["status"], "reason": text(value.get("reason"), 2000)}
    if result["status"] == "completed":
        commands = value.get("commands")
        require(isinstance(commands, list) and 1 <= len(commands) <= 5,
                "Completed local precheck must record commands")
        result.update(
            commands=[text(command, 1000) for command in commands],
            os=text(value.get("os"), 100),
            observed_output=text(value.get("observed_output"), 8000),
        )
    return result


def expired(state, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(state["created_at"])).total_seconds() >= 12 * 60 * 60


def timestamp(value):
    require(isinstance(value, str) and 0 < len(value) <= 100, "Expected a bounded ISO timestamp")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "Timestamp must include an offset")
    return parsed


def validate_checkpoint(state, issue):
    require(isinstance(state, dict) and type(state.get("version")) is int and state["version"] == 1
            and type(state.get("issue")) is int and state["issue"] == issue,
            "Foreign checkpoint schema or issue")
    require(issue_number(state.get("issue_url")) == issue, "Checkpoint issue authority does not match its ref")
    require({"target", "candidate", "local_precheck", "outcome"} <= state.keys(),
            "Missing checkpoint selection/result fields")
    phases = {
        "ready", "agent-intent", "agent", "worker-intent", "worker", "candidate-ready",
        "publication-intent", "terminal", "cancellation-intent", "cancelling", "cleanup-pending",
    }
    require(isinstance(state.get("phase"), str) and state["phase"] in phases, "Unknown checkpoint phase")
    for key in ("base_sha", "control_sha", "agent_sha", "worker_sha"):
        sha(state.get(key))
    for key in ("created_at", "updated_at"):
        timestamp(state.get(key))
    require(isinstance(state.get("agent_workflow"), str) and state["agent_workflow"] in {AGENT, WORKER},
            "Unknown checkpoint agent workflow")
    require(isinstance(state.get("used"), dict) and set(state["used"]) == set(LIMITS),
            "Missing checkpoint budgets")
    for key, limit in LIMITS.items():
        integer(state["used"][key], limit, minimum=0)

    def records(value, name, maximum):
        require(isinstance(value, list) and len(value) <= maximum
                and all(isinstance(item, dict) for item in value), f"Malformed checkpoint {name}")
        return value

    def child(owned, kind):
        require(isinstance(owned, dict), f"Malformed checkpoint {kind}")
        require(sha(owned.get("sha")) == state[kind + "_sha"], "Owned child trusted source changed")
        require(owned.get("workflow") == (state["agent_workflow"] if kind == "agent" else WORKER),
                "Owned child workflow changed")
        branch = owned.get("branch")
        require(isinstance(branch, str) and re.fullmatch(
            rf"ankj/flaky-test-{issue}-{'agent' if kind == 'agent' else 'run'}-[1-9][0-9]*", branch),
            "Owned child branch changed")
        if "id" in owned:
            integer(owned["id"], 10 ** 15)
        if "revision" in owned:
            sha(owned["revision"])
        for key in ("deadline_at", "jobs_completed_at"):
            if key in owned:
                timestamp(owned[key])
        if kind == "worker":
            sha(owned.get("source_sha"))
            require(isinstance(owned.get("phase"), str) and owned["phase"] in {"reproduce", "verify"}
                    and isinstance(owned.get("mode"), str) and owned["mode"] in {"single", "project"},
                    "Malformed worker phase/mode")
            integer(owned.get("runners"), 3)
            integer(owned.get("iterations"), 10)

    for kind in ("agent", "worker"):
        if kind in state:
            child(state[kind], kind)
        if state["phase"] in {kind, kind + "-intent"}:
            require(kind in state, "Missing owned child checkpoint")
        if state["phase"] == kind:
            integer(state[kind].get("id"), 10 ** 15)
            if kind == "agent":
                sha(state[kind].get("revision"))
    for batch in records(state.get("history"), "history", LIMITS["workers"]):
        child(batch, "worker")
        integer(batch.get("id"), 10 ** 15)
        for key in ("passed", "failed", "actual_tests", "runner_iterations"):
            integer(batch.get(key), 10 ** 9, minimum=0)
        require(batch["passed"] + batch["failed"] <= batch["actual_tests"], "Invalid accepted test counts")
        errors = batch.get("errors", [])
        require(isinstance(errors, list) and len(errors) <= 3
                and all(isinstance(item, str) and len(item) <= 4000 for item in errors),
                "Malformed checkpoint failure evidence")
        for case in records(batch.get("cases", []), "test cases", 100000):
            require(isinstance(case.get("os"), str) and case["os"] in OSES,
                    "Malformed checkpoint case OS")
            text(case.get("name"), 2000)
            if "test_id" in case:
                text(case["test_id"], 2000)
            for key in ("passed", "failed"):
                integer(case.get(key), 10 ** 9, minimum=0)
    target = state.get("target")
    require(target is None or isinstance(target, dict), "Malformed checkpoint target")
    if target is not None:
        source = source_path(target.get("source"))
        project = target.get("project")
        require(isinstance(project, str) and re.fullmatch(
            r"tests/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.csproj", project)
            and source.startswith(str(PurePosixPath(project).parent) + "/"), "Malformed target project")
        require(isinstance(target.get("method"), str) and IDENTIFIER.fullmatch(target["method"])
                and isinstance(target.get("class_name"), str) and re.fullmatch(
                    r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)+", target["class_name"]),
                "Malformed target method/class")
        text(target.get("failure_signature"), 500)
        oses = target.get("oses")
        require(isinstance(oses, list) and 1 <= len(oses) <= 3
                and all(isinstance(item, str) and item in OSES for item in oses)
                and len(set(oses)) == len(oses) and type(target.get("quarantined")) is bool,
                "Malformed checkpoint target OS/quarantine")
    if state["phase"] in {"worker", "worker-intent", "candidate-ready"} or state["history"]:
        require(target is not None, "Worker evidence requires a target")
    candidate = state.get("candidate")
    require(candidate is None or isinstance(candidate, dict), "Malformed checkpoint candidate")
    if candidate is not None:
        sha(candidate.get("sha"))
        paths = candidate.get("paths")
        require(isinstance(paths, list) and 1 <= len(paths) <= 5, "Malformed candidate paths")
        for path in paths:
            source_path(path)
        text(candidate.get("diagnosis"))
        if "mode" in candidate:
            require(isinstance(candidate["mode"], str) and candidate["mode"] in {"single", "project"}
                    and target is not None and (candidate["mode"] != "project" or target["quarantined"]),
                    "Malformed candidate verification mode")
    if state["phase"] == "candidate-ready":
        require(candidate is not None, "Missing candidate identity")
    if "repair_scope" in state:
        scope = state["repair_scope"]
        require(isinstance(scope, list) and 1 <= len(scope) <= 150 and all(
            isinstance(path, str) and re.fullmatch(r"(src|tests)/[A-Za-z0-9_./-]+/", path)
            and ".." not in PurePosixPath(path).parts for path in scope), "Malformed repair scope")
    for note in records(state.get("notes"), "notes", LIMITS["models"]):
        require(isinstance(note.get("action"), str)
                and note["action"] in {"select", "reproduce", "repair", "finish"}, "Malformed model action")
        text(note.get("explanation"))
        inspected = note.get("evidence_read")
        require(isinstance(inspected, list) and 1 <= len(inspected) <= 30, "Malformed inspected evidence")
        for item in inspected:
            text(item, 1000)
        if note.get("run_id") is not None:
            integer(note["run_id"], 10 ** 15)
        if "local_precheck" in note:
            local_precheck(note["local_precheck"])
    if state.get("local_precheck") is not None:
        local_precheck(state["local_precheck"])
    for transition in records(state.get("transitions", []), "transitions", 30):
        require(isinstance(transition.get("phase"), str) and transition["phase"] in phases,
                "Malformed transition phase")
        text(transition.get("reason"))
        timestamp(transition.get("at"))
    if "transitions" in state:
        require(state["transitions"], "Checkpoint transition list cannot be empty")
    for rejected in records(state.get("rejected_attempts", []), "rejected attempts", 100):
        text(rejected.get("phase"), 100)
        text(rejected.get("reason"))
        for key in ("run_id", "runners", "iterations"):
            if key in rejected:
                integer(rejected[key], 10 ** 15)
        for key in ("source_sha", "sha"):
            if key in rejected:
                sha(rejected[key])
    if "cancellation" in state:
        cancellation = state["cancellation"]
        require(isinstance(cancellation, dict) and isinstance(cancellation.get("kind"), str)
                and cancellation["kind"] in {"agent", "worker"}, "Malformed checkpoint cancellation")
        owned = state.get(cancellation["kind"])
        require(isinstance(owned, dict) and integer(cancellation.get("run_id"), 10 ** 15) == owned.get("id"),
                "Cancellation no longer matches an owned run")
        require(isinstance(cancellation.get("outcome"), str)
                and cancellation["outcome"] in {"cancelled", "budget-exhausted", "blocked"}
                and type(cancellation.get("confirmed")) is bool, "Malformed cancellation outcome/receipt")
        text(cancellation.get("reason"))
        timestamp(cancellation.get("requested_at"))
    if "dispatch_recovery" in state:
        recovery = state["dispatch_recovery"]
        require(isinstance(recovery, dict) and {
            "kind", "revision", "requested_at", "observed_ids", "confirmed", "outcome", "reason",
        } <= recovery.keys() and isinstance(recovery.get("kind"), str)
                and recovery["kind"] in {"agent", "worker"} and recovery["kind"] in state
                and type(recovery.get("confirmed")) is bool, "Malformed dispatch recovery")
        sha(recovery.get("revision"))
        timestamp(recovery.get("requested_at"))
        text(recovery.get("reason"))
        require(recovery.get("outcome") is None or isinstance(recovery["outcome"], str)
                and recovery["outcome"] in {"blocked", "cancelled", "budget-exhausted"},
                "Malformed dispatch recovery outcome")
        observed = recovery.get("observed_ids")
        require(isinstance(observed, list) and len(observed) <= 100, "Malformed discovered dispatch IDs")
        for run_id in observed:
            integer(run_id, 10 ** 15)
        require(len(set(observed)) == len(observed), "Duplicate discovered dispatch IDs")
        if recovery["confirmed"]:
            require(observed, "Dispatch confirmation requires an observed exact run")
            if len(observed) == 1:
                require(state[recovery["kind"]].get("id") == observed[0],
                        "Recovered child differs from the observed exact run")
            else:
                require(state["phase"] in {"publication-intent", "terminal"},
                        "Ambiguous dispatch confirmation cannot resume work")
        else:
            require("id" not in state[recovery["kind"]]
                    and state["phase"] in {recovery["kind"] + "-intent", "cleanup-pending", "publication-intent"},
                    "Unconfirmed dispatch ownership cannot advance work")
    if state["phase"] == "cancelling":
        require("cancellation" in state, "Missing cancellation reconciliation receipt")
    if state["phase"] == "cleanup-pending":
        require(any(state.get(key, {}).get("confirmed") is False
                    for key in ("cancellation", "dispatch_recovery")), "Missing cleanup reconciliation receipt")
    if state.get("outcome") is not None:
        require(isinstance(state["outcome"], str) and state["outcome"] in TERMINAL, "Malformed terminal outcome")
    if state.get("analysis") is not None:
        terminal_analysis(state["analysis"])
    for key in ("explanation", "finding_url"):
        if key in state:
            text(state[key])
    if "publication_uncertain" in state:
        require(type(state["publication_uncertain"]) is bool, "Malformed publication receipt")


def validate_proposal(state, revision, proposal):
    require(isinstance(proposal, dict) and type(proposal.get("version")) is int and proposal["version"] == 1
            and proposal.get("issue_url") == state["issue_url"]
            and proposal.get("state_revision") == revision, "Stale or foreign proposal")
    require(isinstance(proposal.get("action"), str)
            and proposal["action"] in {"select", "reproduce", "repair", "finish"}, "Unknown proposal action")
    text(proposal.get("explanation"))
    evidence = proposal.get("evidence_read")
    require(isinstance(evidence, list) and 1 <= len(evidence) <= 30, "Evidence inspected must be recorded")
    for item in evidence:
        text(item, 1000)
    if "analysis" in proposal:
        require(proposal["action"] == "finish" and proposal.get("outcome") != "verified",
                "Analysis is only for terminal findings without a verified fix")
        terminal_analysis(proposal["analysis"])


def terminal_analysis(value):
    require(isinstance(value, dict) and set(value) == {"why_no_fix", "attempted", "next_action"},
            "Terminal analysis requires why_no_fix, attempted and next_action")
    def short_line(value, maximum):
        text(value, maximum)
        require(not any(character in value for character in "\r\n"), "Analysis must use short single-line fields")
        return value.strip()
    attempted = value["attempted"]
    require(isinstance(attempted, list) and 1 <= len(attempted) <= 3, "Analysis requires 1-3 attempted steps")
    return {
        "why_no_fix": short_line(value["why_no_fix"], 600),
        "attempted": [short_line(step, 240) for step in attempted],
        "next_action": short_line(value["next_action"], 300),
    }
