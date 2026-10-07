"""Documented local CLI adapter; never copy private transport endpoints."""

import os
import re
import selectors
import subprocess
import time
from urllib.parse import urlencode

import round as contracts
from pilot_results import CollectionError, MAX_LOG, safe_text


def bounded_command(argv, environment, *, limit=MAX_LOG, timeout=60):
    """Drain both pipes without buffering an unbounded response or deadlocking."""
    output = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CollectionError("transient")
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output[key.data].extend(chunk)
                        if sum(map(len, output.values())) > limit:
                            raise CollectionError("bounded")
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CollectionError("transient") from None
            except BaseException:
                process.kill()
                process.wait()
                raise
    try:
        return code, output["out"].decode("utf-8"), output["err"].decode("utf-8")
    except UnicodeError:
        raise CollectionError("bounded") from None


class LocalCollector:
    """Only gh 2.101.0's proven renderer and the selected keyring identity."""

    def __init__(self, token, *, command=bounded_command, environment=None):
        self.token, self.command = token, command
        self.environment = dict(os.environ if environment is None else environment)

    def verified_environment(self):
        environment = dict(self.environment)
        # CLI rejects environment credential sources. Removing the overrides is
        # allowed only if the resulting keyring token equals selected authority.
        environment.pop("GH_TOKEN", None)
        environment.pop("GITHUB_TOKEN", None)
        environment["GH_PAGER"] = "cat"
        environment["GH_PROMPT_DISABLED"] = "1"
        environment["GH_HOST"] = "github.com"
        code, version, _ = self.command(["gh", "--version"], environment, limit=4096, timeout=10)
        if code or not version.startswith("gh version 2.101.0 "):
            raise CollectionError("unsupported")
        for args in (["--user", "radical"], []):
            code, token, _ = self.command(
                ["gh", "auth", "token", "--hostname", "github.com", *args], environment,
                limit=4096, timeout=10)
            if code or not token.strip() or token.strip() != self.token:
                raise CollectionError("identity")
        return environment

    @property
    def capable(self):
        try:
            self.verified_environment()
            return True
        except (CollectionError, OSError):
            return False

    def __call__(self, repository, session):
        if (repository not in {"radical/aspire", "microsoft/aspire"}
                or not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session)):
            raise CollectionError("identity")
        environment = self.verified_environment()
        code, output, error = self.command(
            ["gh", "agent-task", "view", session, "-R", repository, "--log"], environment)
        if code:
            # Only positively identified transport failures retry. Never expose
            # stderr: it can include credentials or signed storage URLs.
            if re.search(r"\b(?:401|403)\b|authentication|token", error, re.I):
                raise CollectionError("authentication")
            if re.search(r"\b(?:502|503|504)\b|connection reset|TLS handshake timeout", error, re.I):
                raise CollectionError("transient")
            raise CollectionError("transport")
        return output

    def fallback(self, repository, task, source, repository_id):
        """Independent public Actions reads under the already selected token."""
        try:
            if repository not in {"radical/aspire", "microsoft/aspire"} or len(task["sessions"]) != 1:
                return None
            session = task["sessions"][0]
            if (task["repository"]["id"] != repository_id or session["repository"]["id"] != repository_id
                    or session["task_id"] != task["id"] or session["base_ref"] != "main"
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session["id"])
                    or not re.fullmatch(r"[0-9a-f]{40}", source["head"])
                    or not isinstance(session["head_ref"], str) or not session["head_ref"]):
                return None
            environment = dict(self.environment)
            environment.update(GH_TOKEN=self.token, GH_HOST="github.com", GH_PAGER="cat", GH_PROMPT_DISABLED="1")
            environment.pop("GITHUB_TOKEN", None)
            deadline, remaining = time.monotonic() + 60, MAX_LOG

            def read(argv, *, json_output=False):
                nonlocal remaining
                seconds = deadline - time.monotonic()
                if seconds <= 0 or remaining <= 0:
                    raise CollectionError("bounded")
                code, output, error = self.command(argv, environment, limit=remaining, timeout=seconds)
                remaining -= len(output.encode()) + len(error.encode())
                if code or remaining < 0:
                    raise CollectionError("transport")
                return contracts.loads(output, max_bytes=MAX_LOG) if json_output else output

            # https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
            # Branch/head only bound the candidate inventory; they never select a
            # run. Exact host-generated session/repository setup fields must match.
            endpoint = f"repos/{repository}/actions/runs"
            query = urlencode({"head_sha": source["head"], "branch": session["head_ref"],
                               "per_page": 10, "page": 1})
            inventory = read(["gh", "api", endpoint + "?" + query, "--method", "GET"], json_output=True)
            runs = inventory["workflow_runs"]
            if (not isinstance(runs, list) or type(inventory["total_count"]) is not int
                    or not 0 <= inventory["total_count"] <= 10 or len(runs) != inventory["total_count"]):
                return None
            matches, seen = [], set()
            for candidate in runs:
                run_id = candidate["id"]
                if type(run_id) is not int or run_id <= 0 or run_id in seen:
                    return None
                seen.add(run_id)
                run = read(["gh", "api", f"{endpoint}/{run_id}", "--method", "GET"], json_output=True)
                if (run["id"] != run_id or run["repository"]["id"] != repository_id
                        or run["repository"]["full_name"] != repository
                        or run["head_sha"] != source["head"] or run["head_branch"] != session["head_ref"]):
                    return None
                # This platform workflow path/event was independently checked in
                # the primary Actions API. Ordinary workflows, even ones printing
                # a copied session ID, cannot establish a worker-host mapping.
                if run["event"] != "dynamic" or run["path"] != "dynamic/copilot-swe-agent/copilot":
                    continue
                if (run["status"] != "completed" or type(run["run_attempt"]) is not int
                        or run["run_attempt"] <= 0
                        or run["conclusion"] not in {"success", "failure", "cancelled", "timed_out", "neutral", "skipped"}):
                    return None
                log = read(["gh", "run", "view", str(run_id), "-R", repository,
                            "--attempt", str(run["run_attempt"]), "--log"])
                evidence = host_evidence(log, session["id"], repository_id)
                fresh = read(["gh", "api", f"{endpoint}/{run_id}", "--method", "GET"], json_output=True)
                if any(fresh.get(key) != run.get(key) for key in (
                        "id", "repository", "head_sha", "head_branch", "event", "path",
                        "run_attempt", "status", "conclusion")):
                    return None
                if evidence is not None:
                    matches.append({"runId": run_id, "runAttempt": run["run_attempt"],
                                    "conclusion": run["conclusion"], **evidence})
            return matches[0] if len(matches) == 1 else None
        except (CollectionError, OSError, ValueError, KeyError, TypeError, AttributeError):
            # Never publish stderr, arbitrary storage links or guessed identity.
            return None


def host_evidence(log, session, repository_id):
    """Parse platform host labels, not arbitrary worker transcript substrings."""
    if not isinstance(log, str) or len(log.encode()) > MAX_LOG:
        return None
    sessions, repositories, action, error = set(), set(), None, None
    environment, setup_complete, processing = False, False, False
    # gh run view --log emits JOB<TAB>STEP<TAB>TIMESTAMP MESSAGE, e.g.:
    # copilot  Start MCP Servers (Linux)  2026-10-06T20:04:52.711Z   COPILOT_AGENT_SESSION_ID: <id>
    # copilot  Processing Request (Linux) ... [cca-engine] turn=2 tool.execution_complete: bash success=true
    # Only runner-generated env fields before processing can correlate a session.
    for line in log.splitlines():
        match = re.fullmatch(r"copilot\t([^\t]+)\t\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z (.*)", line)
        if match is None:
            continue
        step, message = match.groups()
        if step in {"Start MCP Servers (Linux)", "Start MCP Servers (Windows)"} and not processing:
            if message == "env:":
                environment = True
            elif message == "##[endgroup]":
                setup_complete |= environment
                environment = False
            elif environment:
                if message.startswith("  COPILOT_AGENT_SESSION_ID: "):
                    sessions.add(message[len("  COPILOT_AGENT_SESSION_ID: "):])
                if message.startswith("  GITHUB_REPOSITORY_ID: "):
                    repositories.add(message[len("  GITHUB_REPOSITORY_ID: "):])
        elif step in {"Processing Request (Linux)", "Processing Request (Windows)"}:
            processing = True
            if message == "env:":
                environment = True
            elif message == "##[endgroup]":
                environment = False
            elif environment:
                if message.startswith("  COPILOT_AGENT_SESSION_ID: "):
                    sessions.add(message[len("  COPILOT_AGENT_SESSION_ID: "):])
                if message.startswith("  GITHUB_REPOSITORY_ID: "):
                    repositories.add(message[len("  GITHUB_REPOSITORY_ID: "):])
            event = re.fullmatch(r"\[cca-engine\] turn=\d+ tool\.execution_complete: ([A-Za-z0-9_.-]+) success=(true|false)",
                                 message)
            if event:
                action = safe_text(f"Tool {event[1]} completion recorded; success={event[2]}", 250)
        if message.startswith("##[error]"):
            error = safe_text(message[len("##[error]"):], 250)
    if not setup_complete or sessions != {session} or repositories != {str(repository_id)}:
        return None
    return {"lastAction": action, "platformError": error}
