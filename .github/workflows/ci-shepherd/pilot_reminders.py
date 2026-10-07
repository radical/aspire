"""Delayed, deduplicated human notifications; never repair or billing authority."""

from datetime import timedelta
import re
import hashlib
import json
import uuid
from urllib.parse import parse_qs

from github import IncompleteInventory, LostResponse, Response
import issue_pr
import round as contracts

MARKER = "<!-- ci-shepherd:human-reminder:v1:"
KINDS = {"workflow-approval": "workflow approval", "worker-input": "worker input",
         "native-handoff": "an explicit human handoff",
         "child-adoption": "ambiguous child-adoption needing confirmation",
         "worker-result": "an ambiguous worker result needing review",
         "copilot-review": "Copilot review needing confirmation",
         "handoff-needed": "manual app handoff with merging OFF",
         "watching-stale": "stale operator-confirmed app work"}


def delay(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", value) or int(value) > 86400:
        raise ValueError("reminder delay must be 1..86400 whole seconds")
    return int(value)


def validate(value):
    contracts.exact(value, {"id", "head", "kind", "reason", "firstObservedAt", "sendState", "commentId"}, "reminder")
    issue_pr.text(value["id"], "reminder id")
    issue_pr.text(value["head"], "reminder head")
    issue_pr.text(value["kind"], "reminder kind")
    issue_pr.text(value["sendState"], "reminder send state")
    if str(uuid.UUID(value["id"])) != value["id"] or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["head"]):
        raise ValueError("invalid reminder identity/head")
    if value["kind"] not in KINDS:
        raise ValueError("invalid reminder kind")
    issue_pr.text(value["reason"], "reminder reason")
    pattern = r"[1-9][0-9]{0,19}" if value["kind"] == "workflow-approval" else r"[A-Za-z0-9_-]{1,256}"
    if not re.fullmatch(pattern, value["reason"]):
        raise ValueError("invalid reminder reason identity")
    issue_pr.timestamp(value["firstObservedAt"])
    if value["sendState"] not in {"observed", "sent", "uncertain", "confirmed"}:
        raise ValueError("invalid reminder send state")
    if value["commentId"] is not None:
        issue_pr.positive(value["commentId"], "reminder comment")
    if (value["sendState"] == "confirmed") != (value["commentId"] is not None):
        raise ValueError("reminder receipt mismatch")
    return value


def link(value, repository, number):
    base = f"https://github.com/{repository}"
    if value["kind"] == "workflow-approval":
        return base + "/actions/runs/" + value["reason"]
    if value["kind"] == "worker-input":
        return base + "/tasks/" + value["reason"]
    if value["kind"] == "worker-result":
        return base + "/tasks/" + value["reason"]
    # A still-childless issue-phase blocker (64-char content-state digest,
    # not a git head) must link the origin issue, never a nonexistent PR.
    return base + (f"/pull/{number}" if len(value["head"]) == 40 else f"/issues/{number}")


def render(value, repository, number):
    validate(value)
    subject, descriptor = ("PR", "head") if len(value["head"]) == 40 else ("Issue", "content state")
    return (f"[automated] @radical CI Shepherd needs human help.\n\n"
            f"{subject} #{number} at {descriptor} `{value['head']}` is blocked on {KINDS[value['kind']]}.\n"
            f"Please review: {link(value, repository, number)}\n\n{MARKER}{value['id']} -->")


def valid_body(body, repository, number):
    if not isinstance(body, str):
        return False
    base = re.escape(f"https://github.com/{repository}")
    match = re.fullmatch(
        r"\[automated\] @radical CI Shepherd needs human help\.\n\n"
        r"(?:PR #" + str(number) + r" at head `([0-9a-f]{40})`"
        r"|Issue #" + str(number) + r" at content state `([0-9a-f]{64})`)"
        r" is blocked on (workflow approval|worker input|an explicit human handoff"
        r"|ambiguous child-adoption needing confirmation|an ambiguous worker result needing review"
        r"|Copilot review needing confirmation|manual app handoff with merging OFF"
        r"|stale operator-confirmed app work)\.\n"
        r"Please review: (" + base + r"/(?:actions/runs/[1-9][0-9]{0,19}|tasks/[A-Za-z0-9_-]{1,256}|pull/"
        + str(number) + r"|issues/" + str(number) + r"))\n\n" + re.escape(MARKER) + r"([0-9a-f-]{36}) -->", body)
    if match is None:
        return False
    pr_head, issue_head, description, url, identity = match.groups()
    head = pr_head or issue_head
    kind = next(key for key, text in KINDS.items() if text == description)
    reason = ("handoff" if kind in {"native-handoff", "child-adoption"}
              else url.rsplit("/", 1)[1])
    try:
        return (str(uuid.UUID(identity)) == identity
                and url == link({"kind": kind, "reason": reason, "head": head}, repository, number))
    except ValueError:
        return False


def validate_runs_endpoint(path, binding):
    parameters = parse_qs(path.query, strict_parsing=True)
    if (path.path != f"repos/{binding.repository}/actions/runs" or set(parameters) != {"head_sha", "page", "per_page"}
            or parameters["per_page"] != ["100"] or len(parameters["head_sha"]) != 1
            or not re.fullmatch(r"[0-9a-f]{40}", parameters["head_sha"][0])
            or len(parameters["page"]) != 1 or not re.fullmatch(r"[1-9][0-9]*", parameters["page"][0])
            or int(parameters["page"][0]) > 10):
        raise ValueError("only bounded current-head workflow runs are allowed")


def workflow_evidence(api, head):
    try:
        # An approval-gated run can have no jobs/check-runs. The head_sha
        # filtered run connection, including its total_count, is the witness.
        # https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
        runs = api.api.pages(f"{api.prefix}/actions/runs", key="workflow_runs",
                             query={"head_sha": head}, require_total_count=True)
        approval, pending, green = None, False, True
        for run in runs:
            issue_pr.positive(run["id"], "workflow run")
            if (run["head_sha"] != head or run["repository"]["id"] != api.repository_id
                    or run["repository"]["full_name"] != api.repository
                    or len(str(run["id"])) > 20
                    or run["html_url"] != f"https://github.com/{api.repository}/actions/runs/{run['id']}"
                    or run["status"] not in {"completed", "queued", "in_progress", "waiting", "requested", "pending"}
                    or run["status"] == "completed" and not isinstance(run["conclusion"], str)):
                raise ValueError("workflow run identity/state unavailable")
        # Newer terminal same-head outcomes supersede older terminal attempts.
        # Nonterminal runs remain evidence regardless of run ID; rerunning an
        # older ID must not be hidden by a newer run that already finished.
        latest = {}
        for run in runs:
            workflow_id = run.get("workflow_id", run["id"])
            issue_pr.positive(workflow_id, "workflow id")
            if run["status"] != "completed":
                pending = True
                green = False
                continue
            key = (workflow_id, run.get("event"))
            if key not in latest or run["id"] > latest[key]["id"]:
                latest[key] = run
        failures = []
        failed_runs = []
        for run in latest.values():
            green &= run["conclusion"] in {"success", "neutral", "skipped"}
            if run["status"] == "completed" and run["conclusion"] not in {"success", "neutral", "skipped"}:
                failures.append((run.get("check_suite_id"), run["conclusion"]))
                issue_pr.positive(run["run_attempt"], "workflow run attempt")
                failed_runs.append({"id": run["id"], "suite": run.get("check_suite_id"),
                                    "conclusion": run["conclusion"], "url": run["html_url"],
                                    "runAttempt": run["run_attempt"]})
            if run["status"] == "completed" and run["conclusion"] == "action_required":
                if approval is None or run["id"] < int(approval["id"]):
                    approval = {"id": str(run["id"]), "url": run["html_url"]}
        return {"approval": approval, "pending": pending, "green": green, "attention": None,
                "revision": hashlib.sha256(json.dumps(
                    runs, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest(),
                "failures": failures, "failedRuns": failed_runs}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"CI Shepherd #{api.binding.subject or 'PR'} workflow approval evidence unknown: {error}")
        return {"approval": None, "pending": False, "green": False,
                "attention": "Current-head workflow runs unavailable/incomplete; approval is unknown."}


def blocker(chain, observation):
    from pilot_github import native_handoff
    if "handoff" in chain:
        value = chain["handoff"]
        if observation["attention"] is not None or observation["state"] != "open":
            return None
        if value["phase"] == "handoff_needed":
            return "handoff-needed", value["id"]
        if value["phase"] == "watching":
            return "watching-stale", value["id"]
        return None

    if observation["approval"] is not None:
        return "workflow-approval", observation["approval"]["id"]
    if chain["child"] is not None and chain["childAdoption"] in {"sent", "uncertain"}:
        # Other notifications require managed subjects. Notify the origin about
        # adoption first; preserve any separate human stop until confirmation.
        return "child-adoption", chain["childAdoption"]
    for operation in reversed(chain["operations"]):
        if operation["taskId"] is not None and operation["workerState"] == "waiting_for_user":
            return "worker-input", operation["taskId"]
    if chain["state"] == "human" and chain["operations"]:
        operation = chain["operations"][-1]
        if native_handoff(chain):
            return "native-handoff", operation["id"]
        if operation["taskId"] is not None and operation["state"] in {"completed", "failed"} and chain["child"] is None:
            # A terminal worker without a mappable child needs owner review.
            return "worker-result", operation["taskId"]
    if observation.get("copilotReview", {}).get("state") in {"uncertain", "blocked", "limit"}:
        records = chain.get("reviews", [])
        return "copilot-review", records[-1]["id"] if records else "review-limit"
    return None


def matches(value, observation, current, *, progress=None):
    return (current is not None and (value["head"], value["kind"]) == (observation["head"], current[0])
            and (progress is None or value["firstObservedAt"] == progress))


def evidence_unknown(chain, observation):
    if "handoff" in chain:
        return observation["attention"] is not None
    return (observation["workflowAttention"] is not None
            or observation.get("copilotReview", {}).get("state") == "unavailable") or any(
        op["taskId"] is not None and op["workerState"] == "unknown" for op in chain["operations"])


def notification_guard(api, chain, observation, value):
    if not api.write or "handoff" not in chain and chain["state"] not in {"open", "human"}:
        raise ValueError("notification authority disabled or closed")
    if value["kind"] in {"worker-input", "worker-result"}:
        # A saved task may have resumed since observation. Refresh its receipt
        # without adoption effects before deciding whether the notice is still due.
        api.reconcile_workers(adopt_children=False)
        api.persist()
    if value["kind"] == "child-adoption":
        # Confirmation or takeover cancels a stale adoption notice.
        api.adopt_child(chain)
        api.persist()
    # Only adoption notices may inspect an unmanaged child; the origin stays managed.
    observed = api.guard(chain, observation, effect=False, require_managed=value["kind"] != "child-adoption")
    current = blocker(chain, observed)
    if evidence_unknown(chain, observed) or not matches(
            value, observed, current, progress=chain.get("handoff", {}).get("progressAt")):
        raise ValueError("human blocker changed or unknown before notification")
    if value["reason"] != current[1]:
        raise ValueError("human blocker link changed before notification")


def reconcile(api, chain, number, target=None):
    from pilot_github import AuthorityUncertain
    value = chain["reminder"]
    target = number if target is None else target
    expected = render(value, api.repository, number)
    try:
        comments = api.api.pages(f"{api.prefix}/issues/{target}/comments")
        candidates = [comment for comment in comments if api.owned(comment) and comment.get("body") == expected]
        if len(candidates) == 1:
            issue_pr.positive(candidates[0]["id"], "reminder receipt")
            value.update(sendState="confirmed", commentId=candidates[0]["id"])
            api.persist()
            return "confirmed by owned comment receipt"
        return "uncertain; no unique owned receipt, never retry"
    except AuthorityUncertain:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"receipt unavailable; never retry: {error}"


def process(api, chain, observation, now):
    from pilot_github import AuthorityUncertain
    def log(message):
        print(f"CI Shepherd reminder #{observation['number']}: {message}")

    # Match guard()'s fresh-management exception, never cached chain state alone.
    pending_adoption = (chain["child"] is not None and chain["childAdoption"] in {"sent", "uncertain"}
                         and observation["originManaged"] is True and observation["handsOff"] is False
                         and observation["state"] == "open" and not observation["managed"])
    if not api.write or "handoff" not in chain and chain["state"] in {"closed", "hands-off"} or (
            not observation["managed"] and not pending_adoption):
        return
    current = blocker(chain, observation)
    value = chain.get("reminder")
    if current is not None and current[0] == "worker-result" and any(
            operation["taskId"] == current[1]
            and operation.get("result", {}).get("publication") == "sent"
            and operation["workerState"] == operation["result"]["platformState"]
            for operation in chain["operations"]):
        # A controller-owned result report already covers this task's terminal
        # blocker. Keep unrelated input, adoption, approval and wait episodes.
        log("worker result already reported; no duplicate terminal reminder")
        return
    if evidence_unknown(chain, observation):
        log("blocker evidence unknown; timer/receipt retained, no ping")
        return
    if current is None:
        if value is not None:
            chain.pop("reminder")
            api.persist()
            log("blocker resolved; episode cleared")
        return
    if value is None or not matches(value, observation, current, progress=chain.get("handoff", {}).get("progressAt")):
        value = {"id": str(uuid.uuid4()), "head": observation["head"], "kind": current[0], "reason": current[1],
                 "firstObservedAt": (chain["handoff"]["progressAt"] if "handoff" in chain
                                     else issue_pr.stamp(now)), "sendState": "observed", "commentId": None}
        chain["reminder"] = value
        api.persist()
    elif value["sendState"] == "observed" and value["reason"] != current[1]:
        # Refresh the unsent link without restarting the blocker timer.
        value["reason"] = current[1]
        api.persist()
    if value["sendState"] in {"sent", "uncertain"}:
        # Reconcile on the managed origin, while the body identifies the pending child.
        target = chain["origin"] if value["kind"] == "child-adoption" else observation["number"]
        log(reconcile(api, chain, observation["number"], target))
        return
    if value["sendState"] == "confirmed":
        log(f"{KINDS[value['kind']]}; already notified, comment {value['commentId']}")
        return
    elapsed = (now - issue_pr.timestamp(value["firstObservedAt"])).total_seconds()
    if elapsed < 0:
        log("clock rollback; timer retained, no ping")
        return
    if elapsed < api.reminder_delay:
        log(f"{KINDS[value['kind']]}; delay {api.reminder_delay}s, remaining {max(0, api.reminder_delay - elapsed):g}s")
        return

    target = chain["origin"] if value["kind"] == "child-adoption" else observation["number"]
    attempted = False
    try:
        notification_guard(api, chain, observation, value)
        value["sendState"] = "sent"
        api.persist()
        notification_guard(api, chain, observation, value)
        body = render(value, api.repository, observation["number"])
        attempted = True
        response = api.transport("POST", f"{api.prefix}/issues/{target}/comments", {"body": body})
        if (not isinstance(response, Response) or response.status != 201 or not isinstance(response.payload, dict)
                or not isinstance(response.payload.get("user"), dict)
                or not api.owned(response.payload) or response.payload.get("body") != body):
            raise LostResponse("reminder result unknown")
        issue_pr.positive(response.payload["id"], "reminder receipt")
        value.update(sendState="confirmed", commentId=response.payload["id"])
        api.persist()
        log(f"{KINDS[value['kind']]}; notified @radical, comment {value['commentId']}")
    except AuthorityUncertain:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if value["sendState"] == "confirmed":
            log(f"comment {value['commentId']} confirmed; receipt publication failed: {error}")
            raise
        if attempted:
            value["sendState"] = "uncertain"
            api.persist()
            log(reconcile(api, chain, observation["number"], target))
        else:
            if value["sendState"] == "sent":
                value["sendState"] = "observed"
                api.persist()
            log(f"not sent; fresh guard unavailable: {error}")
