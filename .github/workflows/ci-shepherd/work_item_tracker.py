"""Test-tracker dedup and narrow app/tool publication boundary; no issue POST."""

from copy import deepcopy
import re
from urllib.parse import urlencode

from github import LostResponse
import round as contracts
import work_items as items
import work_item_followup as followup
import work_item_receiver as receiver


def validate(value, record, assignment):
    contracts.exact(value, {"action", "signature", "body", "delivery", "linked_issue"}
                    | ({"generated"} if "generated" in value else set())
                    | ({"operator_basis"} if "operator_basis" in value else set()), "tracker follow-up")
    items.validate_actions([value["action"]])
    if value["action"]["kind"] not in {"create_issue", "link_existing_tracker"}:
        raise ValueError("test tracker action required")
    operator = value.get("operator_basis")
    if operator is not None:
        validate_operator(operator, record, assignment)
        if value["action"]["test_identity"] != operator["test_identity"] or "generated" not in value:
            raise ValueError("operator tracker lacks bound generated evidence")
    if (operator is None and (
            assignment["result"] is None or value["action"] not in assignment["result"].get("proposed_actions", []))
            or value["action"]["target"]["repository"] != record["control"]["issue"]["repository"]):
        raise ValueError("tracker proposal assignment/target mismatch")
    if not isinstance(value["signature"], str) or not re.fullmatch(r"[0-9a-f]{16}", value["signature"]):
        raise ValueError("invalid failing-test signature")
    items.text(value["body"], "tracker body", 10000)
    if value["body"].count(owner_marker(record, assignment)) != 1:
        raise ValueError("tracker owner marker mismatch")
    generated = value.get("generated")
    if generated is not None:
        validate_generated(generated, record, value["action"])
        if generated["signature"] != value["signature"]:
            raise ValueError("generated test signature mismatch")
    if value["body"] != proposal_body(record, assignment, value["action"], value["signature"], generated):
        raise ValueError("tracker body differs from checkpointed proposal")
    if value["delivery"] not in {"reserved", "delivering", "uncertain", "delivered", "linked", "closed_match"}:
        raise ValueError("invalid tracker publication receipt")
    linked = value["linked_issue"]
    if linked is not None:
        contracts.exact(linked, {"repository", "number", "node_id", "state"}, "linked test tracker")
        items.repository(linked["repository"])
        items.issue_pr.positive(linked["number"], "linked issue")
        items.identifier(linked["node_id"], "linked issue node")
        if linked["state"] not in {"open", "closed"}:
            raise ValueError("invalid linked tracker state")
        if linked["repository"] != record["control"]["issue"]["repository"]:
            raise ValueError("test tracker repository changed")
    if (value["delivery"] in {"delivered", "linked", "closed_match"}) != (linked is not None):
        raise ValueError("tracker delivery/identity mismatch")


def owner_marker(record, assignment):
    return f"<!-- ci-shepherd:test-tracker:{record['id']}:{assignment['id']} -->"


def signature_marker(signature):
    # Interoperate with CreateFailingTestIssue, never deduplicate issue titles.
    # tools/CreateFailingTestIssue/FailingTestIssueCommand.cs:ComputeStableSignature
    return f"<!-- failing-test-signature: v1:{signature} -->"


def identity_lines(action):
    identity = action["test_identity"]
    return ["Canonical test: " + identity["canonical_name"],
            "Workflow file: " + identity["workflow_file"]]


def validate_operator(value, record, assignment):
    contracts.exact(value, {"item_id", "assignment_id", "evaluated_revision", "test_identity"},
                    "operator tracker basis")
    if (value["item_id"] != record["id"] or value["assignment_id"] != assignment["id"]
            or type(value["evaluated_revision"]) is not int or value["evaluated_revision"] != assignment["revision"]):
        raise ValueError("operator tracker assignment mismatch")


def validate_generated(value, record, action):
    contracts.exact(value, {"title", "body", "signature", "occurrence", "test_identity"}, "generated tracker")
    if value["occurrence"] != record["control"]["occurrence"] or value["test_identity"] != action["test_identity"]:
        raise ValueError("generated tracker occurrence/test mismatch")
    items.proposal({"title": value["title"], "body": value["body"]})
    if not re.fullmatch(r"[0-9a-f]{16}", value["signature"]):
        raise ValueError("generated tracker signature unavailable")
    if value["body"].count(signature_marker(value["signature"])) != 1:
        raise ValueError("generated tracker body signature mismatch")


def proposal_body(record, assignment, action, signature, generated=None):
    if generated is not None:
        return "\n\n".join([
            "[automated] Dedicated failing-test tracker proposal from verified pinned occurrence artifacts.",
            generated["body"], owner_marker(record, assignment), *identity_lines(action),
            f"Original occurrence: https://github.com/{record['control']['issue']['repository']}"
            f"/issues/{record['control']['issue']['number']}. Original issue remains open.",
            "Generation supplies artifact evidence, not confirmed flakiness or root cause."])
    occurrence = record["control"]["occurrence"]
    build = ("N/A" if occurrence is None else
             f"https://github.com/{occurrence['repository']}/actions/runs/{occurrence['run_id']}"
             f"/attempts/{occurrence['run_attempt']}")
    lines = ["[automated] Dedicated failing-test tracker proposal.",
             signature_marker(signature), owner_marker(record, assignment),
             "### Is there an existing issue for this?",
             "- [x] I have searched the existing issues by exact failing-test signature.",
             "### Build information", "Build: " + build, *identity_lines(action),
             "### Fill in the error message template",
             "<!-- Error message template  -->",
             "## Error Message",
             "<!-- Use ErrorMessage for String.Contains matches. Use ErrorPattern for regex matches "
             "(single line/no backtracking). Set BuildRetry to `true` to retry builds with this error. "
             "Set ExcludeConsoleLog to `true` to skip helix logs analysis. -->",
             '```json\n{\n  "ErrorMessage": "",\n  "ErrorPattern": "",\n'
             '  "BuildRetry": false,\n  "ExcludeConsoleLog": false\n}\n```',
             "### Error details", "Summary (worker proposal): " + followup.safe(action["body"], limit=6000),
             "Evidence (worker-reported):"]
    lines.extend(followup.safe(entry, limit=900) for entry in action["evidence"])
    lines.extend(["Justification (worker-reported): " + followup.safe(action["justification"]),
                  f"Original occurrence: https://github.com/{record['control']['issue']['repository']}"
                  f"/issues/{record['control']['issue']['number']}. Original issue remains open.",
                  "### Standard output", "N/A; use the separately collected, redacted session evidence."])
    return "\n\n".join(lines)


def tracker_workflow(api, repository, body, identity):
    # CreateFailingTestIssue emits e.g.:
    #   Build: https://github.com/microsoft/aspire/actions/runs/123/attempts/1
    # It omits the workflow path. Resolve that exact prior build, not the
    # current item's occurrence, so older matching trackers remain reusable.
    body = body.replace("\r\n", "\n")
    builds = re.findall(r"^Build: ([^\r\n]+)$", body, re.M)
    if len(builds) != 1:
        raise ValueError("matching tracker canonical build evidence unavailable")
    build = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)"
        r"(?:/attempts/([1-9][0-9]*))?", builds[0])
    if build is None:
        raise ValueError("matching tracker foreign or malformed build")
    run_id, attempt = int(build[1]), int(build[2]) if build[2] else None
    endpoint = f"repos/{repository}/actions/runs/{run_id}"
    if attempt is not None:
        endpoint += f"/attempts/{attempt}"
    if hasattr(api.transport, "tracker_runs"):
        api.transport.tracker_runs.add(endpoint)
    run = api.api.get(endpoint)
    if (not isinstance(run, dict) or run.get("id") != run_id or type(run.get("id")) is not int
            or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0
            or attempt is not None and run["run_attempt"] != attempt
            or not isinstance(run.get("repository"), dict)
            or run["repository"].get("full_name") != repository
            or run.get("path") != identity["workflow_file"]):
        raise ValueError("matching tracker host workflow/build identity mismatch")


def dedup(api, value):
    action, signature = value["action"], value["signature"]
    repository = action["target"]["repository"]
    marker = signature_marker(signature)
    endpoint = "search/issues?" + urlencode({
        "q": f'repo:{repository} is:issue "{marker}"', "per_page": "100"})
    if hasattr(api.transport, "tracker_search"):
        api.transport.tracker_search = endpoint
    result = api.api.get(endpoint)
    if (result.get("incomplete_results") is not False or type(result.get("total_count")) is not int
            or not isinstance(result.get("items"), list)
            or result["total_count"] != len(result["items"]) or result["total_count"] > 100):
        raise ValueError("test tracker dedup search incomplete")
    matches = []
    seen = set()
    for candidate in result["items"]:
        number = candidate.get("number")
        items.issue_pr.positive(number, "dedup issue")
        if number in seen:
            raise ValueError("duplicate search result")
        seen.add(number)
        path = f"repos/{repository}/issues/{number}"
        if candidate.get("url") != "https://api.github.com/" + path:
            raise ValueError("foreign test tracker search result")
        if hasattr(api.transport, "tracker_issues"):
            api.transport.tracker_issues.add(path)
        issue = api.api.get(path)
        body = issue.get("body")
        if (issue.get("number") != number or issue.get("node_id") != candidate.get("node_id")
                or issue.get("state") not in {"open", "closed"} or "pull_request" in issue
                or not isinstance(body, str) or body.count(marker) != 1):
            raise ValueError("test tracker current identity/marker unavailable")
        identity = action["test_identity"]
        # Normalize only the parsing copy; exact original bodies remain intact
        # for publication receipts and uncertain-delivery recovery.
        fields = body.replace("\r\n", "\n")
        tests = re.findall(r"^(Canonical test|Build error leg or test failing): ([^\r\n]+)$", fields, re.M)
        # Match the tool's EscapeMarkdownInline serialization, not a lossy
        # unescape: raw Canonical test fields and escaped tool fields differ.
        escaped = re.sub(r"([\\`*_\[\]\(\)~])", r"\\\1", identity["canonical_name"])
        if not tests or any(test != (escaped if field == "Build error leg or test failing"
                                     else identity["canonical_name"]) for field, test in tests):
            raise ValueError("matching signature without exact test evidence")
        workflows = re.findall(r"^Workflow file: ([^\r\n]+)$", fields, re.M)
        if workflows and (len(workflows) != 1 or workflows[0] != identity["workflow_file"]):
            raise ValueError("matching signature without exact workflow evidence")
        if not workflows or re.search(r"^Build:", fields, re.M):
            tracker_workflow(api, repository, body, identity)
        if any(label.get("name") == "shepherd-hands-off" for label in issue.get("labels", [])):
            raise ValueError("matching tracker is hands-off")
        matches.append(issue)
    return matches


def linked(repository, issue):
    return {"repository": repository, "number": issue["number"], "node_id": issue["node_id"],
            "state": issue["state"]}


def refresh_link(value, matches):
    saved = value["linked_issue"]
    if (len(matches) != 1 or saved is None or matches[0]["number"] != saved["number"]
            or matches[0]["node_id"] != saved["node_id"]):
        raise ValueError("saved test tracker current identity unavailable")
    value["linked_issue"] = linked(saved["repository"], matches[0])
    if value["delivery"] != "delivered":
        value["delivery"] = "linked" if matches[0]["state"] == "open" else "closed_match"


def prepare(api, read_control, action, signature_for=None, *, generated=None, operator_basis=None):
    """signature_for is a host capability, never a worker-supplied hash.

    It must compute CreateFailingTestIssue's XxHash3 canonical test/workflow
    signature. App integration supplies the existing .NET implementation.
    """
    control, record, assignment = followup.owned(api, read_control)
    collection = assignment["execution"].get("followup")
    result = assignment["result"]
    if operator_basis is not None:
        validate_operator(operator_basis, record, assignment)
        if generated is None or action["test_identity"] != operator_basis["test_identity"]:
            raise ValueError("operator tracker requires verified generator output")
    if (collection is None or operator_basis is None and (
            result is None or action not in result.get("proposed_actions", []))
            or action["kind"] not in {"create_issue", "link_existing_tracker"}
            or action["target"]["repository"] != control["issue"]["repository"]):
        raise ValueError("owned worker test tracker proposal required")
    followup.stable(api, read_control, control, record, assignment, collection["task_version"])
    old = assignment["execution"].get("tracker_followup")
    if old is not None:
        if old["action"] != action:
            raise ValueError("another tracker proposal already reserved")
        if old["linked_issue"] is not None:
            refresh_link(old, dedup(api, old))
            receiver.fresh(api, read_control, control)
            api.persist()
        return {"outcome": old["delivery"], "title": action["title"], "body": old["body"],
                "linked_issue": old["linked_issue"]}
    if generated is not None:
        validate_generated(generated, record, action)
        signature = generated["signature"]
    elif signature_for is not None:
        signature = signature_for(action["test_identity"]["canonical_name"], action["test_identity"]["workflow_file"])
    else:
        raise ValueError("verified host tracker generation required")
    value = {"action": deepcopy(action), "signature": signature,
             "body": proposal_body(record, assignment, action, signature, generated),
             "delivery": "reserved", "linked_issue": None}
    if generated is not None:
        value["generated"] = deepcopy(generated)
    if operator_basis is not None:
        value["operator_basis"] = deepcopy(operator_basis)
    validate(value, record, assignment)
    matches = dedup(api, value)
    if len(matches) > 1:
        raise ValueError("ambiguous matching test trackers")
    if matches:
        issue = matches[0]
        if action["kind"] == "link_existing_tracker" and (
                action["target"]["number"] != issue["number"]
                or action["target"]["node_id"] != issue["node_id"]):
            raise ValueError("worker proposed tracker differs from verified test identity")
        value.update(delivery="linked" if issue["state"] == "open" else "closed_match",
                     linked_issue=linked(control["issue"]["repository"], issue))
    elif action["kind"] == "link_existing_tracker":
        raise ValueError("proposed existing test tracker unavailable")
    receiver.fresh(api, read_control, control)
    followup.stable(api, read_control, control, record, assignment, collection["task_version"])
    assignment["execution"]["tracker_followup"] = value
    api.persist()
    return {"outcome": value["delivery"], "title": action["title"], "body": value["body"],
            "linked_issue": value["linked_issue"]}


def publish(api, read_control, approved, create_issue):
    """create_issue receives exact approved repo/title/body and final guard.

    The app adapter calls its create_issue tool. This module and its transport
    have no general issue POST capability. Unknown outcomes never resend.
    """
    control, record, assignment = followup.owned(api, read_control)
    value = assignment["execution"].get("tracker_followup")
    if value is None:
        raise ValueError("tracker preview required")
    collection = assignment["execution"]["followup"]
    followup.stable(api, read_control, control, record, assignment, collection["task_version"])
    matches = dedup(api, value)
    if value["delivery"] in {"delivering", "uncertain"}:
        owned = [issue for issue in matches if issue["body"] == value["body"] and api.owned(issue)]
        if len(owned) == 1:
            value.update(delivery="delivered", linked_issue=linked(control["issue"]["repository"], owned[0]))
            api.persist()
            return {"outcome": "delivered", "linked_issue": value["linked_issue"]}
        return {"outcome": "uncertain"}
    if value["delivery"] != "reserved":
        refresh_link(value, matches)
        receiver.fresh(api, read_control, control)
        api.persist()
        return {"outcome": value["delivery"], "linked_issue": value["linked_issue"]}
    if len(matches) > 1:
        raise ValueError("ambiguous matching test trackers")
    if matches:
        value.update(delivery="linked" if matches[0]["state"] == "open" else "closed_match",
                     linked_issue=linked(control["issue"]["repository"], matches[0]))
        api.persist()
        return {"outcome": value["delivery"], "linked_issue": value["linked_issue"]}
    expected = {"item_id": record["id"], "assignment_id": assignment["id"],
                "revision": control["revision"], "repository": control["issue"]["repository"],
                "title": value["action"]["title"], "body": value["body"]}
    if not api.write or not control["authority"].get("issue_create", False):
        return {"outcome": "preview_only", "approval_template": expected}
    contracts.exact(approved, set(expected), "tracker approval")
    items.issue_pr.positive(approved["revision"], "tracker approval revision")
    if approved != expected:
        raise ValueError("exact item-bound tracker title/body approval required")
    receiver.fresh(api, read_control, control)
    value["delivery"] = "delivering"
    api.persist()
    def before_send():
        receiver.fresh(api, read_control, control)
        followup.stable(api, read_control, control, record, assignment, collection["task_version"])
        if dedup(api, value):
            raise ValueError("matching test tracker appeared before create; no POST")
    try:
        before_send()
        issue = create_issue(expected["repository"], expected["title"], expected["body"], before_send)
        if (not isinstance(issue, dict) or issue.get("body") != value["body"] or not api.owned(issue)
                or issue.get("repository_url") != f"https://api.github.com/repos/{expected['repository']}"
                or issue.get("state") != "open"):
            raise LostResponse("issue creation response identity uncertain")
        try:
            identity = linked(expected["repository"], issue)
            items.issue_pr.positive(identity["number"], "created tracker")
            items.identifier(identity["node_id"], "created tracker node")
        except (ValueError, KeyError) as error:
            raise LostResponse("created tracker returned identity unavailable") from error
        path = f"repos/{expected['repository']}/issues/{identity['number']}"
        if hasattr(api.transport, "tracker_issues"):
            api.transport.tracker_issues.add(path)
        current = api.api.get(path)
        if (not isinstance(current, dict) or current.get("node_id") != identity["node_id"]
                or current.get("number") != identity["number"] or current.get("body") != value["body"]
                or current.get("repository_url") != f"https://api.github.com/repos/{expected['repository']}"
                or "pull_request" in current or not api.owned(current) or current.get("state") != "open"):
            raise LostResponse("created tracker current identity unavailable")
    except (LostResponse, OSError):
        value["delivery"] = "uncertain"
        api.persist()
        return {"outcome": "uncertain"}
    value.update(delivery="delivered", linked_issue=identity)
    api.persist()
    return {"outcome": "delivered", "linked_issue": identity}
