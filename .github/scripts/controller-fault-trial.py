"""Fork-only fault injection around the real short production controller."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys


MARKER = "Controlled lifecycle fault fixture; no test ran"
ALLOWED_ISSUES = {152}


def validate_enrollment(state, issue, control_sha, worker_sha, worker_workflow):
    if (state["issue"] != issue
            or state["issue_url"] != f"https://github.com/radical/aspire/issues/{issue}"
            or state["control_sha"] != control_sha
            or state["worker_sha"] != worker_sha or state["agent_sha"] != worker_sha
            or state["agent_workflow"] != worker_workflow):
        raise ValueError("Enrolled fixture source identity differs from this activation")
    for role, suffix in (("agent", "agent"), ("worker", "run")):
        owned = state.get(role)
        if owned is not None and (
                owned["sha"] != worker_sha or owned["workflow"] != worker_workflow
                or not re.fullmatch(
                    rf"ankj/flaky-test-{issue}-{suffix}-[1-9][0-9]*", owned["branch"])):
            raise ValueError("Owned child identity is outside this fixture activation")


def require_unstarted(state):
    if (state["phase"] != "ready" or state["target"] is not None
            or any(state["used"].values())
            or any(state.get(field) is not None for field in ("agent", "worker", "cancellation"))):
        raise ValueError("Selection fault requires an unstarted fixture without an active child")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--issue-url", required=True)
    parser.add_argument("--action", required=True, choices=[
        "select", "resume", "cancel", "expire", "snapshot",
        "kill-after-dispatch", "lose-cancel-response", "preflight",
    ])
    parser.add_argument("--intended-action")
    parser.add_argument("--control-sha")
    parser.add_argument("--worker-sha")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    match = re.fullmatch(r"https://github\.com/radical/aspire/issues/([1-9][0-9]*)",
                         args.issue_url)
    if (not match or int(match[1]) not in ALLOWED_ISSUES
            or os.environ.get("GITHUB_REPOSITORY") != "radical/aspire"):
        raise ValueError("Fault injection is restricted to an allowed exact fork fixture issue")
    intended = args.intended_action if args.action == "preflight" else args.action
    if intended not in {
            "start", "select", "resume", "cancel", "expire", "snapshot",
            "kill-after-dispatch", "lose-cancel-response"}:
        raise ValueError("An exact intended fixture action is required")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value or "")
               for value in (args.control_sha, args.worker_sha)):
        raise ValueError("Published control and worker SHAs must be exact")
    if args.receipt.exists():
        raise ValueError("Existing trial receipt; reconcile before rerunning")
    sys.path.insert(0, str(args.source_root / ".github/scripts/flaky_investigation"))
    import controller
    from github import GitHub

    github = GitHub()
    issue = github.api(f"issues/{match[1]}")
    if ("pull_request" in issue or issue["html_url"] != args.issue_url or issue["state"] != "open"
            or MARKER not in issue["body"] or not issue["title"].startswith("[automated]")):
        raise ValueError("Only an explicitly marked open fault-fixture issue is allowed")
    state, revision = github.load(int(match[1]))
    if state is not None:
        validate_enrollment(state, int(match[1]), args.control_sha, args.worker_sha,
                            controller.WORKER)
        if intended in ("select", "kill-after-dispatch"):
            require_unstarted(state)
    elif intended not in ("start", "select", "kill-after-dispatch"):
        raise ValueError("Fixture has no enrolled controller state")
    if args.action == "preflight":
        args.receipt.write_text(json.dumps({
            "fixture": "preflight", "intended_action": intended,
            "issue_url": args.issue_url, "revision": revision,
        }, indent=2) + "\n")
        return
    original_api = GitHub.api

    def interrupted_api(instance, endpoint, body=None, method=None, binary=False, timeout=120):
        response = original_api(instance, endpoint, body, method, binary, timeout)
        dispatch = endpoint.endswith("/dispatches") and body is not None
        cancellation = endpoint.endswith("/cancel") and method == "POST"
        if ((args.action == "kill-after-dispatch" and dispatch)
                or (args.action == "lose-cancel-response" and cancellation)):
            args.receipt.write_text(json.dumps({
                "fixture": args.action, "endpoint": endpoint, "response": response,
                "issue_url": args.issue_url, "tests_executed": 0,
            }, indent=2) + "\n")
            os.kill(os.getpid(), signal.SIGKILL)
        return response

    GitHub.api = interrupted_api
    engine = controller.Controller(github)
    if args.action in ("select", "kill-after-dispatch"):
        state, revision = engine.start(
            args.issue_url, args.control_sha, agent_workflow=controller.WORKER,
            agent_sha=args.worker_sha, worker_sha=args.worker_sha,
        )
        require_unstarted(state)
        engine.handle(state, revision, {
            "version": 1, "issue_url": args.issue_url, "state_revision": revision,
            "action": "select", "explanation": MARKER,
            "evidence_read": ["Fork main SlimTestProgramTests.cs; controlled hung-child fixture"],
            "local_precheck": {"status": "skipped", "reason": MARKER},
            "selection": {
                "source": "tests/Aspire.Hosting.Tests/SlimTestProgramTests.cs",
                "project": "tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj",
                "class_name": "Aspire.Hosting.Tests.SlimTestProgramTests",
                "method": "TestProjectStartsAndStopsCleanly",
                "oses": ["ubuntu-latest"], "failure_signature": MARKER,
            },
        })
    else:
        if args.action == "expire":
            # This is deliberate checkpoint fault injection, not elapsed-budget proof.
            owned = state.get("worker") or state.get("agent")
            if not isinstance(owned, dict) or not owned.get("deadline_at"):
                raise ValueError("No exact owned child deadline to expire")
            owned["deadline_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            engine.save(state, revision, reason="fixture-seeded expired child deadline")
        elif args.action != "snapshot":
            engine.advance(state, revision, cancel=args.action in ("cancel", "lose-cancel-response"))
    state, revision = github.load(int(match[1]))
    args.receipt.write_text(json.dumps({
        "fixture": args.action, "issue_url": args.issue_url,
        "tests_executed": 0, "revision": revision, "state": state,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
