"""Explicit operator-authenticated local execution of the shared upstream pilot."""

import argparse
from contextlib import contextmanager
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
        if effect:
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["observe", "run", "watch"])
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    if args.interval < 30:
        parser.error("interval must be at least 30 seconds")
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
        if not command(["copilot", "--no-auto-update", "--version"]).startswith("GitHub Copilot CLI 1.0.92-3."):
            raise ValueError("local decision engine must match the pinned Copilot1.0.92-3")
        lock_root = Path.home() / ".copilot" / "ci-shepherd" / "locks"
        with authority_lock(lock_root, args.authority):
            while True:
                require_source(revision)
                require_idle_actions(token)
                api = LocalGitHub(token, args.tracker, args.authority, args.tracker_node, write=True, revision=revision)
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
