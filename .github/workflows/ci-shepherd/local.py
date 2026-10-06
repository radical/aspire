"""Explicit operator-authenticated local execution of the shared upstream pilot."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import live
import issue_pr
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_state as state
import reasoning
import round as contracts


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = "repos/radical/aspire/actions/workflows/ci-shepherd.lock.yml"


def command(argv, *, token=None):
    environment = {**os.environ, "GH_TOKEN": token} if token is not None else os.environ
    return subprocess.run(argv, env=environment, check=True, capture_output=True, text=True).stdout.strip()


def metadata(path, token, *, paginate=False):
    argv = ["gh", "api", "--hostname", "github.com", path]
    if paginate:
        argv += ["--paginate", "--slurp"]
    return contracts.loads(command(argv, token=token), max_bytes=live.MAX_API_JSON_BYTES)


def require_source(revision):
    if revision != command(["git", "--no-pager", "-C", str(ROOT), "rev-parse", "HEAD"]) or command(
            ["git", "--no-pager", "-C", str(ROOT), "status", "--porcelain"]):
        raise ValueError("local controller source changed; no new effects")


def require_idle_actions(token, *, reader=metadata):
    if reader(WORKFLOW, token).get("state") != "disabled_manually":
        raise ValueError("disable the hosted Shepherd workflow before local effects")
    pages = reader(WORKFLOW + "/runs?per_page=100", token, paginate=True)
    if not isinstance(pages, list) or not pages:
        raise ValueError("hosted run inventory unavailable")
    total, identities = None, []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            raise ValueError("hosted run inventory malformed")
        count = page.get("total_count")
        if type(count) is not int or count < 0 or total is not None and total != count:
            raise ValueError("hosted run count missing or changed")
        total = count
        for run in page["workflow_runs"]:
            if not isinstance(run, dict) or run.get("status") != "completed":
                raise ValueError("hosted Shepherd work is still active or unknown; no local effects")
            issue_pr.positive(run.get("id"), "hosted run id")
            identities.append(run["id"])
    issue_pr.unique(identities, "hosted run id")
    if len(identities) != total:
        raise ValueError("hosted run inventory incomplete")


class LocalGitHub(github.PilotGitHub):
    def __init__(self, token, tracker, authority, node, *, write, revision):
        self.token = token
        self.revision = revision
        super().__init__(
            github.PilotTransport(token, write=write, binding=bindings.UPSTREAM,
                                  tracker=tracker, authority=authority),
            tracker, authority, node, write=write, binding=bindings.UPSTREAM)

    def authority_guard(self):
        require_idle_actions(self.token)
        super().authority_guard()

    def enabled(self):
        value = metadata("repos/radical/aspire/actions/variables/CI_SHEPHERD_ENABLE", self.token)
        if value.get("name") != "CI_SHEPHERD_ENABLE" or value.get("value") not in {"true", "false"}:
            raise ValueError("Shepherd enable configuration is unavailable or malformed")
        return value["value"] == "true"

    def guard(self, chain, observation, *, effect=True):
        # Notifications use effect=False to skip repair-only checks, but still
        # need the local stop controls. Billing persistence does not use guard.
        require_source(self.revision)
        if not self.enabled():
            raise ValueError("Shepherd globally disabled; no new effects")
        return super().guard(chain, observation, effect=effect)


@contextmanager
def authority_lock(directory, authority):
    if os.name != "posix":
        raise ValueError("local pilot currently requires a POSIX host")
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(directory / f"{authority}.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ValueError("local authority lock has a different owner")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another local Shepherd owns this tracking authority") from None
        yield
    finally:
        os.close(descriptor)


def prompt(packet):
    # Use the actual workflow body, including its untrusted-evidence instructions.
    # The local adapter supplies exactly the same prepare output, not this chat.
    body = (ROOT / ".github/workflows/ci-shepherd.md").read_text().split("\n---\n", 1)[1]
    placeholder = "${{ needs.prepare.outputs.prompt }}"
    if body.count(placeholder) != 1:
        raise ValueError("workflow body has an unsupported prompt binding")
    if "${{" in body.replace(placeholder, ""):
        raise ValueError("unresolved workflow prompt expression")
    return body.replace(placeholder, pilot.prompt(packet))


def checkpoint_usage(events):
    previous = None
    for event in events:
        if event.get("type") != "session.usage_checkpoint":
            continue
        # Same authoritative checkpoint consumed by pinned gh-aw:
        # {"type":"session.usage_checkpoint","data":{"totalNanoAiu":4481190000}}
        # https://github.com/github/gh-aw/blob/v0.89.17/actions/setup/js/parse_token_usage.cjs
        raw = event["data"]["totalNanoAiu"]
        if isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
            raw = int(raw)
        value = state.amount(raw) / 1e9
        if previous is not None and value < previous:
            raise ValueError("native usage checkpoint moved backwards")
        previous = value
    return previous


def execute(directory, packet, token, *, process=subprocess.run, executable="copilot"):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700)
    session_id = str(uuid.uuid4())
    executable = shutil.which(executable) or str(Path(executable).resolve())
    text = prompt(packet)
    (directory / "prompt.txt").write_text(text, encoding="utf-8")
    contracts.write_json(directory / "launch.json", {"sessionId": session_id, "resumed": False})
    # Outside the checkout, with a new HOME: no repository/user instructions,
    # memory, MCP configuration, plugins or prior session can become input.
    with tempfile.TemporaryDirectory(prefix="ci-shepherd-native-") as temporary:
        isolated = Path(temporary)
        environment = reasoning.child_environment(isolated, {})
        environment["COPILOT_GITHUB_TOKEN"] = token
        config = isolated / "mcp.json"
        contracts.write_json(config, {"mcpServers": {"safeoutputs": {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("decision_server.py")), "--output", str(isolated / "decision.json")],
            "tools": ["*"],
        }}})
        contracts.write_json(isolated / "packet.json", packet)
        argv = [
            executable, "--no-auto-update", "--no-custom-instructions", "--disable-builtin-mcps",
            "--no-remote", "--no-remote-export", "--no-ask-user",
            "--additional-mcp-config", "@" + str(config),
            "--available-tools", "safeoutputs-submit_decision",
            # CLI permissions match server(tool), unlike model-visible server-tool names.
            "--allow-tool", "safeoutputs(submit_decision)", "--deny-tool", "shell", "write", "task",
            "--max-ai-credits", str(state.NATIVE_RESERVE), "--session-id", session_id,
            "--log-level", "all", "--log-dir", str(isolated / "logs"),
            "--usage-output-file", str(isolated / "usage.json"),
            "--secret-env-vars", "COPILOT_GITHUB_TOKEN",
            "--silent", "--output-format", "json", "--prompt", text,
        ]
        failure = None
        try:
            result = process(argv, cwd=isolated, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             check=False, timeout=600)
            (directory / "stdout.jsonl").write_text(result.stdout, encoding="utf-8")
            (directory / "stderr.txt").write_text(result.stderr, encoding="utf-8")
            if result.returncode != 0:
                failure = ValueError(f"fresh Copilot exited {result.returncode}; see {directory / 'stderr.txt'}")
        except subprocess.TimeoutExpired:
            failure = ValueError("fresh Copilot exceeded the ten-minute packet lifetime")
        sessions = Path(environment["COPILOT_HOME"]) / "session-state"
        files = list(sessions.glob("*/events.jsonl"))
        if len(files) == 1 and files[0].parent.name == session_id:
            events = reasoning.jsonl(files[0].read_text(encoding="utf-8"))
            # Preserve actual process evidence and billing even if the decision fails.
            contracts.write_json(directory / "session-events.json", events)
            usage = checkpoint_usage(events)
            if usage is not None:
                contracts.write_json(directory / "usage.json", {"ai_credits": usage})
        if failure is not None:
            raise failure
        evidence = reasoning.collect(sessions, isolated / "logs", "success", directory / "evidence.json")
        decision, _ = reasoning.validate_evidence(evidence, session_id, hosted=True)
        if contracts.read_json(isolated / "decision.json") != decision:
            raise ValueError("recorded safe output differs from the actual final decision")
        return evidence


def sweep(api, directory, revision, *, executor=execute):
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700)
    run = {"repository": github.REPOSITORY, "runId": "local-" + str(uuid.uuid4()),
           "runAttempt": "1", "workflowSha": revision}
    contracts.write_json(directory / "run.json", run)
    if not api.enabled():
        api.read_authority()
        api.reconcile_workers()
        api.persist()
        result = {"outcome": "disabled; billing observation only"}
    else:
        packet = pilot.prepare(api, run, live.clock())
        contracts.write_json(directory / "packet.json", packet)
        if packet is None:
            result = {"outcome": "waiting; no inference"}
            if api.ledger["chains"]:
                result = {
                    "outcome": "observed; no inference",
                    "reasons": [{"chain": chain["id"], "reason": api.next_action(chain, api.observe(chain))}
                                for chain in api.ledger["chains"]],
                    "roundLimitReached": any(chain["rounds"] >= api.binding.round_limit for chain in api.ledger["chains"]),
                }
        else:
            evidence = None
            try:
                evidence = executor(directory / "agent", packet, api.token)
            except (ValueError, OSError, KeyError, TypeError) as error:
                print(f"CI Shepherd local decision unavailable: {error}", file=sys.stderr)
                contracts.write_json(directory / "native-failure.json", {"error": str(error)})
            usage = None
            try:
                usage = pilot.native_usage(directory / "agent" / "usage.json")
            except (ValueError, OSError, KeyError) as error:
                print(f"CI Shepherd local billing unavailable; reservation retained: {error}", file=sys.stderr)
            if usage is None:
                print("CI Shepherd local billing unknown; native reservation retained.", file=sys.stderr)
            api.packet_time = issue_pr.timestamp(packet["preparedAt"])
            # Packet validity and fresh session evidence are still checked by the core.
            result = pilot.settle(api, packet, evidence, usage, live.clock(), billing_only=not api.enabled())
            if result["outcome"] == "validate":
                raise ValueError("inline fork repair is outside this fixed upstream local runner")
    contracts.write_json(directory / "result.json", result)
    print(json.dumps({"run": run["runId"], **result}, allow_nan=False), flush=True)
    return result


def resume(api, operation_id, expected_head, now):
    """Explicitly unmask one completed upstream native handoff, without work."""
    if api.binding != bindings.UPSTREAM or not api.write:
        raise ValueError("resume requires the fixed upstream local writer")
    if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise ValueError("resume requires an exact expected current head")
    api.read_authority()
    original = deepcopy(api.ledger)
    try:
        chain = state.find_chain(api.ledger, bindings.UPSTREAM.subject)
        if (chain is None or chain["kind"] != "pr" or chain["child"] is not None
                or chain["state"] != "human" or not chain["operations"]):
            raise ValueError("not an upstream native handoff")
        latest = chain["operations"][-1]
        if (latest["id"] != operation_id or latest["state"] != "completed"
                or latest["taskId"] is not None or latest["sessionId"] is None
                or latest["workerState"] is not None or latest["workerAt"] is not None
                or latest["workerActual"] not in {None, 0}):
            raise ValueError("latest exact completed native handoff required")
        if (state.pending(chain) or chain["rounds"] >= api.binding.round_limit
                or state.chain_spend(chain) + state.NATIVE_RESERVE > state.chain_allowance(api.ledger)
                or state.repository_spend(api.ledger, now) + state.NATIVE_RESERVE > state.REPOSITORY_ALLOWANCE
                or state.worker_slots(api.ledger) >= 2):
            raise ValueError("resume prospective admission budget/round/pending limit")
        # Direct GETs only. Reconciliation would alter billing/history and could
        # reinsert old worker dispositions into the deliberately unmasked batch.
        for current in api.ledger["chains"]:
            if state.pending(current):
                raise ValueError("unresolved authority work")
            for operation in current["operations"]:
                if operation["nativeActual"] is None or operation["nativeReserved"] or operation["workerReserved"]:
                    raise ValueError("unknown billing/reservation; no resume")
                if operation["taskId"] is None and (
                        operation["workerState"] not in {None, "failed"} or operation["workerActual"] not in {None, 0}):
                    raise ValueError("unresolved/missing worker identity")
                if operation["taskId"] is not None:
                    task, usage = api.task_detail(operation["taskId"], current, operation)
                    if (task["state"] not in state.TERMINAL or operation["workerState"] not in state.TERMINAL
                            or usage is None or usage != operation["workerActual"]):
                        raise ValueError("saved task resumed or billing changed/unknown")
        basis = contracts.loads(latest["identity"].rsplit(":round:", 1)[0])
        batch = basis.get("feedback")
        if (not isinstance(batch, list) or not batch or any(not isinstance(item, str) for item in batch)
                or len(batch) != len(set(batch)) or basis.get("number") != chain["origin"]
                or basis.get("node") != chain["node"] or basis.get("head") != expected_head
                or any(item not in chain["dispositions"] for item in batch)):
            raise ValueError("saved handoff batch/head unavailable")
        for operation in chain["operations"][:-1]:
            if operation["taskId"] is not None:
                old = contracts.loads(operation["identity"].rsplit(":round:", 1)[0])
                if set(batch) & set(old.get("feedback", [])):
                    raise ValueError("handoff overlaps older completed worker feedback")
        removed = [item for item in batch if chain["dispositions"][item] == "needs-human"]
        if not removed and not all(chain["dispositions"][item] == "declined" for item in batch):
            raise ValueError("latest handoff has no needs-human feedback and is not all declined")
        chain["state"] = "open"
        for item in removed:
            del chain["dispositions"][item]
        reminder = chain.get("reminder")
        if (reminder is not None and reminder["kind"] == "native-handoff"
                and reminder["reason"] == operation_id and reminder["head"] == expected_head):
            del chain["reminder"]
        # Compare freshness only AFTER unmasking. Resume is an admission change,
        # not repair: a pending/infra wait may legitimately be reopened.
        observed = api.observe(chain)
        if observed["head"] != expected_head or not observed["managed"]:
            raise ValueError("resume source head/management changed")
        visible = {item["id"] for item in observed["feedback"]}
        missing = set(removed) - visible
        # Same-head reruns can supersede failed-check IDs even after recovery.
        # The operator authorizes this exact saved batch, not those old checks'
        # continued existence. Vanished/edited non-CI feedback remains stale.
        if (basis.get("description") != observed["description"] or any(
                not item.startswith(("check:", "status:", "workflow:")) for item in missing)):
            raise ValueError("saved handoff feedback/source changed")
        api.guard(chain, observed, effect=False)
        # The guard may perform slow reads. Do not publish an admission change
        # if any history/counter/billing changed during that boundary.
        expected = deepcopy(original)
        resumed = state.find_chain(expected, chain["origin"])
        resumed["state"] = "open"
        for item in removed:
            del resumed["dispositions"][item]
        if reminder is not None and "reminder" not in chain:
            resumed.pop("reminder")
        if api.ledger != expected:
            raise ValueError("resume authority history/budget changed")
        api.persist()
        return {"outcome": "resumed; no inference", "operation": operation_id, "head": expected_head}
    except (ValueError, KeyError, TypeError):
        api.ledger = original
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["observe", "run", "watch", "resume"])
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--operation")
    parser.add_argument("--expected-head")
    args = parser.parse_args(argv)
    if args.interval < 30:
        parser.error("interval must be at least 30 seconds")
    if args.mode == "resume" and (not args.operation or not args.expected_head):
        parser.error("resume requires --operation and --expected-head")
    try:
        token = command(["gh", "auth", "token", "--hostname", "github.com", "--user", "radical"])
        revision = command(["git", "--no-pager", "-C", str(ROOT), "rev-parse", "HEAD"])
        if args.mode != "observe" and command(["git", "--no-pager", "-C", str(ROOT), "status", "--porcelain"]):
            raise ValueError("commit the reviewed controller source before local effects")
        if args.mode == "observe":
            api = LocalGitHub(token, args.tracker, args.authority, args.tracker_node, write=False, revision=revision)
            api.read_authority()
            observations = api.sweep()
            for chain in api.ledger["chains"]:
                api.log_status(chain, observations[chain["child"] or chain["origin"]], live.clock())
            return 0
        if args.mode != "resume" and not command(["copilot", "--no-auto-update", "--version"]).startswith("GitHub Copilot CLI 1.0.92-3."):
            raise ValueError("local decision engine must match the pinned Copilot1.0.92-3")
        lock_root = Path.home() / ".copilot" / "ci-shepherd" / "locks"
        with authority_lock(lock_root, args.authority):
            while True:
                require_source(revision)
                require_idle_actions(token)
                api = LocalGitHub(token, args.tracker, args.authority, args.tracker_node, write=True, revision=revision)
                if args.mode == "resume":
                    print(json.dumps(resume(api, args.operation, args.expected_head, live.clock())), flush=True)
                    return 0
                result = sweep(api, args.workdir / str(uuid.uuid4()), revision)
                if result["outcome"] in {"failed", "uncertain", "no-send"}:
                    return 1
                if args.mode == "run" or not api.enabled():
                    return 0
                time.sleep(args.interval)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"CI Shepherd local stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
