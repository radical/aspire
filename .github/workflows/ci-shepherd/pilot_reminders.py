"""Delayed, deduplicated human notifications; never repair or billing authority."""

from datetime import timedelta
import re
import uuid
from urllib.parse import parse_qs

from github import IncompleteInventory, LostResponse, Response
import issue_pr
import round as contracts

MARKER = "<!-- ci-shepherd:human-reminder:v1:"
KINDS = {"workflow-approval": "workflow approval", "worker-input": "worker input",
         "native-handoff": "an explicit human handoff"}


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
    if str(uuid.UUID(value["id"])) != value["id"] or not re.fullmatch(r"[0-9a-f]{40}", value["head"]):
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
    return base + f"/pull/{number}"


def render(value, repository, number):
    validate(value)
    return (f"[automated] @radical CI Shepherd needs human help.\n\n"
            f"PR #{number} at head `{value['head']}` is blocked on {KINDS[value['kind']]}.\n"
            f"Please review: {link(value, repository, number)}\n\n{MARKER}{value['id']} -->")


def valid_body(body, repository, number):
    if not isinstance(body, str):
        return False
    base = re.escape(f"https://github.com/{repository}")
    match = re.fullmatch(
        r"\[automated\] @radical CI Shepherd needs human help\.\n\n"
        + re.escape(f"PR #{number} at head `") + r"([0-9a-f]{40})` is blocked on "
        r"(workflow approval|worker input|an explicit human handoff)\.\nPlease review: ("
        + base + r"/(?:actions/runs/[1-9][0-9]{0,19}|tasks/[A-Za-z0-9_-]{1,256}|pull/"
        + str(number) + r"))\n\n" + re.escape(MARKER) + r"([0-9a-f-]{36}) -->", body)
    if match is None:
        return False
    _head, description, url, identity = match.groups()
    kind = next(key for key, text in KINDS.items() if text == description)
    reason = "handoff" if kind == "native-handoff" else url.rsplit("/", 1)[1]
    try:
        return str(uuid.UUID(identity)) == identity and url == link({"kind": kind, "reason": reason}, repository, number)
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
            pending |= run["status"] != "completed"
            green &= run["status"] == "completed" and run["conclusion"] in {"success", "neutral", "skipped"}
            if run["status"] == "completed" and run["conclusion"] == "action_required":
                if approval is None or run["id"] < int(approval["id"]):
                    approval = {"id": str(run["id"]), "url": run["html_url"]}
        return {"approval": approval, "pending": pending, "green": green, "attention": None}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"CI Shepherd #{api.binding.subject or 'PR'} workflow approval evidence unknown: {error}")
        return {"approval": None, "pending": False, "green": False,
                "attention": "Current-head workflow runs unavailable/incomplete; approval is unknown."}


def blocker(chain, observation):
    if observation["approval"] is not None:
        return "workflow-approval", observation["approval"]["id"]
    for operation in reversed(chain["operations"]):
        if operation["taskId"] is not None and operation["workerState"] == "waiting_for_user":
            return "worker-input", operation["taskId"]
    if chain["state"] == "human" and chain["operations"]:
        operation = chain["operations"][-1]
        if operation["state"] == "completed" and operation["taskId"] is None and operation["sessionId"] is not None:
            return "native-handoff", operation["id"]
    return None


def matches(value, observation, current):
    return current is not None and (value["head"], value["kind"]) == (observation["head"], current[0])


def evidence_unknown(chain, observation):
    return observation["workflowAttention"] is not None or any(
        op["taskId"] is not None and op["workerState"] == "unknown" for op in chain["operations"])


def notification_guard(api, chain, observation, value):
    if not api.write or chain["state"] not in {"open", "human"}:
        raise ValueError("notification authority disabled or closed")
    if value["kind"] == "worker-input":
        api.reconcile_workers()
        api.persist()
    # Human handoff permits a notification, never the ordinary repair effect.
    observed = api.guard(chain, observation, effect=False)
    current = blocker(chain, observed)
    if evidence_unknown(chain, observed) or not matches(value, observed, current):
        raise ValueError("human blocker changed or unknown before notification")
    if value["reason"] != current[1]:
        raise ValueError("human blocker link changed before notification")


def reconcile(api, chain, number):
    value = chain["reminder"]
    expected = render(value, api.repository, number)
    try:
        comments = api.api.pages(f"{api.prefix}/issues/{number}/comments")
        candidates = [comment for comment in comments if api.owned(comment) and comment.get("body") == expected]
        if len(candidates) == 1:
            issue_pr.positive(candidates[0]["id"], "reminder receipt")
            value.update(sendState="confirmed", commentId=candidates[0]["id"])
            api.persist()
            return "confirmed by owned comment receipt"
        return "uncertain; no unique owned receipt, never retry"
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"receipt unavailable; never retry: {error}"


def process(api, chain, observation, now):
    def log(message):
        print(f"CI Shepherd reminder #{observation['number']}: {message}")

    if not api.write or observation["kind"] != "pr" or not observation["managed"] or chain["state"] in {"closed", "hands-off"}:
        return
    current = blocker(chain, observation)
    value = chain.get("reminder")
    if evidence_unknown(chain, observation):
        log("blocker evidence unknown; timer/receipt retained, no ping")
        return
    if current is None:
        if value is not None:
            chain.pop("reminder")
            api.persist()
            log("blocker resolved; episode cleared")
        return
    if value is None or not matches(value, observation, current):
        value = {"id": str(uuid.uuid4()), "head": observation["head"], "kind": current[0], "reason": current[1],
                 "firstObservedAt": issue_pr.stamp(now), "sendState": "observed", "commentId": None}
        chain["reminder"] = value
        api.persist()
    elif value["sendState"] == "observed" and value["reason"] != current[1]:
        # Refresh the unsent link without restarting the blocker timer.
        value["reason"] = current[1]
        api.persist()
    if value["sendState"] in {"sent", "uncertain"}:
        log(reconcile(api, chain, observation["number"]))
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

    attempted = False
    try:
        notification_guard(api, chain, observation, value)
        value["sendState"] = "sent"
        api.persist()
        notification_guard(api, chain, observation, value)
        body = render(value, api.repository, observation["number"])
        attempted = True
        response = api.transport("POST", f"{api.prefix}/issues/{observation['number']}/comments", {"body": body})
        if (not isinstance(response, Response) or response.status != 201 or not isinstance(response.payload, dict)
                or not isinstance(response.payload.get("user"), dict)
                or not api.owned(response.payload) or response.payload.get("body") != body):
            raise LostResponse("reminder result unknown")
        issue_pr.positive(response.payload["id"], "reminder receipt")
        value.update(sendState="confirmed", commentId=response.payload["id"])
        api.persist()
        log(f"{KINDS[value['kind']]}; notified @radical, comment {value['commentId']}")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if value["sendState"] == "confirmed":
            log(f"comment {value['commentId']} confirmed; receipt publication failed: {error}")
            raise
        if attempted:
            value["sendState"] = "uncertain"
            api.persist()
            log(reconcile(api, chain, observation["number"]))
        else:
            if value["sendState"] == "sent":
                value["sendState"] = "observed"
                api.persist()
            log(f"not sent; fresh guard unavailable: {error}")
