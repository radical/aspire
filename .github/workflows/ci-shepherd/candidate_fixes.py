"""Bounded candidate-fix observation; never grants PR management authority."""

from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urlparse

from github import IncompleteInventory, LostResponse, Response
import issue_pr
import work_items as items


MAX_TRACKERS, MAX_PULLS, MAX_NOTICES = 5, 10, 10
MAX_BYTES = 400_000
PREFIX = "[automated] **CI Shepherd: tracking candidate fixes**"
QUERY = """query($owner:String!,$name:String!,$number:Int!){
repository(owner:$owner,name:$name){nameWithOwner pullRequest(number:$number){
id number closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}
pageInfo{hasNextPage}}}}}"""


def capability(transport, method, endpoint, body):
    path = urlparse(endpoint)
    repository = getattr(transport, "candidate_repository", None)
    if method == "POST" and endpoint == "graphql" and repository is not None:
        if (not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != QUERY
                or not isinstance(body["variables"], dict)
                or set(body["variables"]) != {"owner", "name", "number"}
                or body["variables"]["owner"] + "/" + body["variables"]["name"] != repository):
            return False
        issue_pr.positive(body["variables"]["number"], "candidate query PR")
        return True
    if method == "GET" and repository is not None and body is None:
        prefix = re.escape("repos/" + repository)
        if (not path.scheme and not path.netloc and not path.fragment
                and re.fullmatch(prefix + r"/(?:pulls/[1-9][0-9]*|issues/[1-9][0-9]*(?:/timeline)?)", path.path)):
            query = parse_qs(path.query, strict_parsing=True)
            if query and (not path.path.endswith("/timeline") or set(query) != {"page", "per_page"}
                          or query["per_page"] != ["100"] or len(query["page"]) != 1
                          or query["page"][0] not in {"1", "2"}):
                raise ValueError("invalid candidate pagination")
            return True
    request = getattr(transport, "candidate_notice", None)
    if (method == "POST" and request is not None
            and request == {"endpoint": endpoint, "body": body}):
        if (not transport.write or not re.fullmatch(
                re.escape("repos/" + transport.binding.repository) + r"/issues/[1-9][0-9]*/comments", endpoint)
                or not isinstance(body, dict) or set(body) != {"body"}
                or not isinstance(body["body"], str) or not body["body"].startswith(PREFIX)
                or len(body["body"].encode()) > 4000):
            raise ValueError("invalid exact candidate notice")
        return True
    return False


def validate(value):
    items.contracts.exact(value, {"pulls", "complete", "attention", "notices"}, "candidate tracking")
    items.boolean(value["complete"], "candidate completeness")
    if value["attention"] is not None:
        items.text(value["attention"], "candidate attention", 1000)
    if not isinstance(value["pulls"], list) or len(value["pulls"]) > MAX_PULLS:
        raise ValueError("candidate inventory bound")
    seen = set()
    for pull in value["pulls"]:
        items.contracts.exact(pull, {"number", "node_id", "sha", "state", "merged", "own", "linked", "via"},
                              "candidate PR")
        issue_pr.positive(pull["number"], "candidate PR")
        items.identifier(pull["node_id"], "candidate PR node")
        items.sha(pull["sha"])
        issue_pr.choice(pull["state"], {"open", "closed"}, "candidate state")
        for key in ("merged", "own", "linked"):
            items.boolean(pull[key], "candidate " + key)
        issue_pr.positive(pull["via"], "candidate source issue")
        if pull["number"] in seen:
            raise ValueError("duplicate candidate PR")
        seen.add(pull["number"])
    if not isinstance(value["notices"], list) or len(value["notices"]) > MAX_NOTICES:
        raise ValueError("candidate notice bound")
    for notice in value["notices"]:
        items.contracts.exact(notice, {"body", "state", "id"}, "candidate notice")
        items.text(notice["body"], "candidate notice body", 4000)
        if not notice["body"].startswith(PREFIX):
            raise ValueError("candidate notice prefix")
        issue_pr.choice(notice["state"], {"sending", "uncertain", "delivered"}, "candidate notice state")
        if notice["id"] is not None:
            issue_pr.positive(notice["id"], "candidate notice ID")
        if (notice["state"] == "delivered") != (notice["id"] is not None):
            raise ValueError("candidate notice receipt mismatch")


def scan(api, record):
    issue = record["control"]["issue"]
    repository, root = issue["repository"], issue["number"]
    prefix = "repos/" + repository
    total = 0

    def get(path):
        nonlocal total
        value = api.api.get(path)
        total += len(json.dumps(value).encode())
        if total > MAX_BYTES:
            raise IncompleteInventory("candidate aggregate byte bound")
        return value

    def timeline(number):
        nonlocal total
        events = api.api.pages(f"{prefix}/issues/{number}/timeline", max_pages=2,
                               max_bytes=MAX_BYTES, identity_key=None)
        total += len(json.dumps(events).encode())
        if total > MAX_BYTES:
            raise IncompleteInventory("candidate aggregate byte bound")
        references = {}
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = event.get("source", {}).get("issue", {})
            number = source.get("number")
            issue_pr.positive(number, "candidate reference")
            if source.get("url") != f"https://api.github.com/{prefix}/issues/{number}":
                continue
            references[number] = source
        if len(references) > MAX_PULLS + MAX_TRACKERS:
            raise IncompleteInventory("candidate reference bound")
        return references

    def closing_references(pull):
        nonlocal total
        # Use GitHub's native closing relationship, not keyword proximity in
        # untrusted prose. A plain mention can create a cross-reference too.
        # https://docs.github.com/en/graphql/reference/objects#pullrequest
        owner, name = repository.split("/")
        response = api.transport("POST", "graphql", {"query": QUERY, "variables": {
            "owner": owner, "name": name, "number": pull["number"]}})
        if not isinstance(response, Response) or response.status != 200:
            raise IncompleteInventory("candidate closing references unavailable")
        value = response.payload
        total += len(json.dumps(value).encode())
        if total > MAX_BYTES or not isinstance(value, dict) or value.get("errors"):
            raise IncompleteInventory("candidate closing references incomplete")
        repo = value["data"]["repository"]
        current = repo["pullRequest"]
        connection = current["closingIssuesReferences"]
        if (repo["nameWithOwner"] != repository or current["id"] != pull["node_id"]
                or current["number"] != pull["number"] or connection["pageInfo"]["hasNextPage"] is not False
                or not isinstance(connection["nodes"], list) or len(connection["nodes"]) > 20):
            raise IncompleteInventory("candidate closing references identity/completeness mismatch")
        refs = set()
        for node in connection["nodes"]:
            issue_pr.positive(node["number"], "candidate closing issue")
            if node["repository"]["nameWithOwner"] == repository:
                refs.add(node["number"])
        return refs

    references = timeline(root)
    trackers = {}
    for number, source in references.items():
        if "pull_request" in source:
            continue
        tracker = get(f"{prefix}/issues/{number}")
        body = tracker.get("body") or ""
        # Follow only the failing-test tracker convention, not arbitrary issue mentions.
        backlink = re.search(rf"(?<![A-Za-z0-9/#])#{root}(?![0-9])", body)
        if ("<!-- failing-test-signature: v1:" in body and backlink
                and tracker.get("number") == number and tracker.get("node_id") == source.get("node_id")
                and "pull_request" not in tracker):
            trackers[number] = tracker
    for assignment in record["assignments"]:
        saved = assignment.get("execution", {}).get("tracker_followup", {}).get("linked_issue")
        if saved is not None and saved["repository"] == repository:
            number = saved["number"]
            tracker = get(f"{prefix}/issues/{number}")
            if tracker.get("node_id") != saved["node_id"] or "pull_request" in tracker:
                raise ValueError("saved test tracker identity changed")
            trackers[number] = tracker
    if len(trackers) > MAX_TRACKERS:
        raise IncompleteInventory("candidate tracker bound")
    sources = [(root, references)]
    sources.extend((number, timeline(number)) for number in sorted(trackers))
    linked = {}
    for via, references in sources:
        for number, source in references.items():
            if "pull_request" in source:
                linked.setdefault(number, set()).add(via)
    # Refresh historical identities even after links disappear. Do not silently
    # turn a previously observed fix into permission for a fresh paid assignment.
    previous = {pull["number"]: pull for pull in record.get("candidate_fixes", {}).get("pulls", [])}
    own = set()
    import work_item_execution as execution
    for assignment in record["assignments"]:
        saved = assignment.get("execution")
        if saved is None:
            continue
        if saved.get("pr") is not None:
            own.add(saved["pr"]["number"])
        elif saved["state"] == "sent":
            observed = execution.refresh(api, record, assignment)
            if any(entry.get("type") == "pull" for entry in observed["artifacts"]):
                own.add(execution.observe_task_pr(api, observed)["number"])
    numbers = set(linked) | set(previous) | own
    if len(numbers) > MAX_PULLS:
        raise IncompleteInventory("candidate PR bound")
    pulls = []
    for number in sorted(numbers):
        pull = get(f"{prefix}/pulls/{number}")
        if (pull.get("number") != number or pull.get("base", {}).get("repo", {}).get("full_name") != repository
                or pull.get("base", {}).get("ref") != "main"):
            raise ValueError("candidate target identity mismatch")
        if number in previous and pull.get("node_id") != previous[number]["node_id"]:
            raise ValueError("candidate immutable identity changed")
        closing = closing_references(pull) if number in linked else set()
        vias = sorted(linked.get(number, set()) & closing)
        if not vias and number not in previous and number not in own:
            continue
        pulls.append({"number": number, "node_id": pull["node_id"], "sha": pull["head"]["sha"],
                      "state": pull["state"], "merged": pull["merged"], "own": number in own,
                      "linked": bool(vias), "via": vias[0] if vias else previous.get(number, {}).get("via", root)})
    value = {"pulls": pulls, "complete": True, "attention": None,
             "notices": deepcopy(record.get("candidate_fixes", {}).get("notices", []))}
    validate(value)
    return value


def refresh(api, record):
    import pilot_github
    import work_item_followup as followup
    try:
        value = api.observe_candidates(record)
    except pilot_github.AuthorityUncertain:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IncompleteInventory, LostResponse) as error:
        value = deepcopy(record.get("candidate_fixes", {"pulls": [], "notices": []}))
        value.update(complete=False, attention=followup.safe(error, getattr(api, "token", None), 1000))
    validate(value)
    record["candidate_fixes"] = value
    return value


def blocks(value):
    # Closed/unlinked historical candidates need human reassessment, not auto retry.
    return not value["complete"] or any(not pull["own"] for pull in value["pulls"])


def guard_launch(api, record):
    if not hasattr(api, "observe_candidates"):
        return
    value = refresh(api, record)
    if blocks(value):
        import work_item_execution as execution
        saved = record["assignments"][-1]["execution"]
        # This guard is called only before POST, never for an escaped request.
        saved.update(state="reserved", worker_reserved=0, attention="Candidate fix tracking blocks new dispatch.")
        api.persist()
        raise execution.AdmissionDeferred(saved["attention"])


def body(record):
    value = record["candidate_fixes"]
    repository = record["control"]["issue"]["repository"]
    lines = [PREFIX, f"<!-- ci-shepherd:candidates:{record['id']} -->"]
    for pull in value["pulls"]:
        status = "merged; validation pending" if pull["merged"] else pull["state"]
        if not pull["linked"] and not pull["own"]:
            status += "; link removed, reassessment needed"
        ownership = "our task" if pull["own"] else "observation only"
        lines.append(f"- [#{pull['number']}](https://github.com/{repository}/pull/{pull['number']}): {status}, {ownership}.")
    lines.append("No additional Copilot repair will be launched. Already-started work is not cancelled.")
    lines.append("External PR branches are not managed by Shepherd. This is not an issue-resolution claim.")
    if not value["complete"]:
        lines.append("Candidate discovery is incomplete; dispatch remains on hold.")
    return "\n".join(lines)


def publish(api, record):
    import normal_loop
    control = record["control"]
    value = record["candidate_fixes"]
    if (not any(not pull["own"] for pull in value["pulls"])
            or not normal_loop.report_authorized(api, control) or control["action"] == "pause"):
        return
    notices = value["notices"]
    prefix = f"repos/{control['issue']['repository']}/issues/{control['issue']['number']}/comments"
    # Reconcile escaped intent before generating another version, even if the
    # inventory changed. Never blindly replay a missing/uncertain POST receipt.
    for notice in notices:
        if notice["state"] == "delivered":
            continue
        matches = [comment for comment in api.api.pages(prefix, max_pages=2, max_bytes=MAX_BYTES)
                   if api.owned(comment) and comment.get("body") == notice["body"]]
        if len(matches) != 1:
            value["attention"] = "Candidate notice outcome unknown; no duplicate publication."
            api.persist()
            return
        notice.update(state="delivered", id=matches[0]["id"])
        api.persist()
    rendered = body(record)
    if any(notice["body"] == rendered for notice in notices):
        return
    if len(notices) >= MAX_NOTICES:
        value["attention"] = "Candidate notice history bound; human attention required."
        api.persist()
        return
    notice = {"body": rendered, "state": "sending", "id": None}
    notices.append(notice)
    api.persist()
    api.guard_candidates(control)
    latest = api.read_authority()
    saved = next(entry for entry in latest["workItems"] if entry["id"] == record["id"])
    if (saved["control"] != control or saved.get("candidate_fixes") != value
            or not normal_loop.report_authorized(api, control)):
        raise ValueError("candidate notice authority changed")
    # Relationship/state changes invalidate publication; ordinary head updates do not.
    current = refresh(api, record)
    if body(record) != rendered:
        current["notices"].pop()
        current["attention"] = "Candidate inventory changed before notice; reconciliation required."
        api.persist()
        return
    api.guard_candidates(control)
    latest = api.read_authority()
    saved = next(entry for entry in latest["workItems"] if entry["id"] == record["id"])
    if (saved["control"] != control or saved["candidate_fixes"]["notices"][-1] != notice
            or not normal_loop.report_authorized(api, control)):
        raise ValueError("candidate notice authority changed")
    request = {"endpoint": prefix, "body": {"body": rendered}}
    api.transport.candidate_notice = request
    try:
        response = api.transport("POST", prefix, request["body"])
        if (not isinstance(response, Response) or response.status != 201
                or response.payload.get("body") != rendered or not api.owned(response.payload)
                or type(response.payload.get("id")) is not int or response.payload["id"] <= 0):
            raise LostResponse("candidate notice receipt unavailable")
    except LostResponse:
        record["candidate_fixes"]["notices"][-1]["state"] = "uncertain"
        api.persist()
        return
    finally:
        api.transport.candidate_notice = None
    # refresh replaces the observation object, so settle the canonical notice.
    record["candidate_fixes"]["notices"][-1].update(state="delivered", id=response.payload["id"])
    api.persist()
