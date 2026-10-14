"""Inspect Shepherd tracking, stage narrow control changes, and explicitly apply them."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys

import local
import pilot_binding as bindings
import pilot_github
import pilot_state as state
import pilot_storage as storage
import round as contracts
from standalone import secure_path
import work_item_execution as execution
import work_items as items


class TrackingTransport(pilot_github.PilotTransport):
    def validate_endpoint(self, method, endpoint, body):
        if self.memory_capability(method, endpoint, body):
            return
        if method != "GET":
            raise ValueError("tracking CLI permits only reads and exact memory publication")
        super().validate_endpoint(method, endpoint, body)


class TrackingGitHub(local.LocalGitHub):
    def __init__(self, token, tracker, authority, node, *, write, revision, binding):
        self.token, self.revision = token, revision
        transport = TrackingTransport(token, write=write, binding=binding, tracker=tracker, authority=authority)
        pilot_github.PilotGitHub.__init__(self, transport, tracker, authority, node, write=write, binding=binding)
        storage.install_local_policy(self)
        # Management never activates/migrates authority as an incidental effect.
        self.memory_authorized = False

    def authority_guard(self):
        if self.write:
            self.normal_guard()
        super().authority_guard()


def connect(args, *, write=False):
    token = local.selected_token()
    revision = local.command(["git", "--no-pager", "-C", str(local.ROOT), "rev-parse", "HEAD"])
    return TrackingGitHub(token, args.tracker, args.authority, args.tracker_node,
                          write=write, revision=revision, binding=bindings.select(args.target))


def validate_graph(ledger):
    """Reuse record/import validators, then check explicit retry links in both directions."""
    state.validate(ledger)
    records = {record["id"]: record for record in ledger.get("workItems", [])}
    for record in records.values():
        visited = {record["id"]}
        current = record
        while "retry_of" in current:
            reference = current["retry_of"]
            parent = records.get(reference["item_id"])
            if parent is None or parent["id"] in visited:
                raise ValueError("retry reference missing or cyclic")
            assignments = [entry for entry in parent["assignments"] if entry["id"] == reference["assignment_id"]]
            if (len(assignments) != 1
                    or assignments[0].get("execution", {}).get("task_id") != reference["task_id"]
                    or parent["control"]["issue"] != current["control"]["issue"]):
                raise ValueError("retry issue/assignment/task reference mismatch")
            visited.add(parent["id"])
            current = parent
    return ledger


def load(api):
    ledger = api.read_authority()
    validate_graph(ledger)
    if hasattr(api, "adopt_authority"):
        api.adopt_authority(ledger)
    else:
        api.ledger, api.expected = deepcopy(ledger), deepcopy(ledger)
    return ledger


def snapshot(api):
    revision = api.storage_revision
    return {"authority": storage.identity(api),
            "generation": revision.generation if revision is not None else None,
            "head": revision.snapshot.commit if revision is not None else None}


def issue_url(identity):
    return f"https://github.com/{identity['repository']}/issues/{identity['number']}"


def reference(url):
    # Accept only canonical URLs, e.g. https://github.com/microsoft/aspire/issues/123.
    match = re.fullmatch(r"https://github\.com/(radical/aspire|microsoft/aspire)/(issues|pull)/([1-9][0-9]*)", url)
    if match is None:
        raise ValueError("expected a canonical supported issue or PR URL")
    return match[1], match[2], int(match[3])


def related(ledger, record):
    identity = record["control"]["issue"]
    return sorted(entry["id"] for entry in ledger.get("workItems", [])
                  if entry["id"] != record["id"] and entry["control"]["issue"] == identity)


def row(record):
    assignments = record["assignments"]
    saved = assignments[-1].get("execution", {}) if assignments else {}
    return {"id": record["id"], "kind": "issue", "url": issue_url(record["control"]["issue"]),
            "action": record["control"]["action"], "route": record["control"]["requested_route"],
            "state": saved.get("task_state") or saved.get("state")
            or (assignments[-1]["delivery"] if assignments else "not_dispatched"),
            "attention": saved.get("attention"), "task_id": saved.get("task_id"),
            "session_id": saved.get("session_id"), "pr": saved.get("pr"),
            "credits_actual": saved.get("worker_actual"), "credits_reserved": saved.get("worker_reserved")}


def pull_links(record):
    repository = record["control"]["issue"]["repository"]
    links = {pull["number"]: {"repository": repository, **pull}
             for pull in record.get("candidate_fixes", {}).get("pulls", [])}
    for _, _, saved in execution.executions({"workItems": [record]}):
        if saved["pr"] is not None:
            links[saved["pr"]["number"]] = saved["pr"]
    return [links[number] for number in sorted(links)]


def observe_link(api, repository, kind, number, node):
    value = api.api.get(f"repos/{repository}/{'pulls' if kind == 'pull' else 'issues'}/{number}")
    if (repository != api.repository or value.get("number") != number or value.get("node_id") != node
            or value.get("state") not in {"open", "closed"}
            or kind == "issue" and "pull_request" in value):
        raise ValueError(f"{'PR' if kind == 'pull' else 'issue'} reference identity changed")
    if kind == "pull":
        if value.get("base", {}).get("repo", {}).get("id") != api.repository_id:
            raise ValueError("PR repository identity changed")
        return {"number": number, "state": value["state"], "draft": value["draft"],
                "merged": value["merged"], "head": value["head"]["sha"]}
    return {"number": number, "state": value["state"],
            "hands_off": any(label.get("name") == "shepherd-hands-off" for label in value.get("labels", []))}


def chain_row(ledger, chain):
    return {"id": chain["id"], "kind": "chain",
            "url": f"https://github.com/{ledger['repository']}/"
            f"{'pull' if chain['kind'] == 'pr' else 'issues'}/{chain['origin']}",
            "state": chain["state"], "child": chain["child"],
            "source": chain.get("sourceWorkItem")}


def listing(api, *, include_paused=False):
    ledger = load(api)
    return {**snapshot(api), "schema_version": 1, "evidence": "saved",
            "items": sorted((row(record) for record in ledger.get("workItems", [])
                             if include_paused or record["control"]["action"] != "pause"), key=lambda value: value["id"]),
            "chains": sorted((chain_row(ledger, chain) for chain in ledger["chains"]), key=lambda value: value["id"])}


def show(api, selector, *, live=False):
    ledger = load(api)
    records = ledger.get("workItems", [])
    chains = ledger["chains"]
    if selector.startswith("https://"):
        repository, kind, number = reference(selector)
        if kind == "issues":
            selected = [record for record in records if record["control"]["issue"]["repository"] == repository
                        and record["control"]["issue"]["number"] == number]
        else:
            selected = [record for record in records if any(
                pull["repository"] == repository and pull["number"] == number for pull in pull_links(record))]
        selected_chains = [chain for chain in chains if ledger["repository"] == repository and (
            chain["origin"] == number and chain["kind"] == ("pr" if kind == "pull" else "issue")
            or kind == "pull" and chain["child"] == number)]
    else:
        selected = [record for record in records if record["id"] == selector]
        selected_chains = [chain for chain in chains if chain["id"] == selector]
    sources = {chain.get("sourceWorkItem", {}).get("item_id") for chain in selected_chains}
    selected = [record for record in records if record in selected or record["id"] in sources]
    selected_ids = {record["id"] for record in selected}
    selected_chains = [chain for chain in chains if chain in selected_chains
                       or chain.get("sourceWorkItem", {}).get("item_id") in selected_ids]
    if not selected and not selected_chains:
        raise ValueError("tracked item not found")
    result = {**snapshot(api), "schema_version": 1, "evidence": "live" if live else "saved",
              "items": [], "chains": []}
    for record in sorted(selected, key=lambda value: value["id"]):
        entry = {**row(record), "related_items": related(ledger, record),
                 "related_status": [row(other) for other in sorted(records, key=lambda value: value["id"])
                                    if other["id"] in related(ledger, record)], "record_evidence": "saved",
                 "record": deepcopy(record)}
        if live:
            issue = record["control"]["issue"]
            entry["live_issue"] = observe_link(api, issue["repository"], "issue", issue["number"], issue["node_id"])
            entry["live_tasks"] = []
            entry["live_prs"] = [observe_link(api, pull["repository"], "pull", pull["number"], pull["node_id"])
                                 for pull in pull_links(record)]
            for _, assignment, saved in execution.executions({"workItems": [record]}):
                if saved["task_id"] is not None:
                    observed = execution.task(api, record, deepcopy(assignment))
                    task = {"task_id": observed["id"], "state": observed["state"], "pr": None}
                    if any(artifact.get("provider") == "github" and artifact.get("type") == "pull"
                           for artifact in observed["artifacts"]):
                        # Observe only: do not adopt the PR, bind a chain or
                        # require nonempty changes from a worker still running.
                        pull = execution.observe_task_pr(api, observed)
                        task["pr"] = {"number": pull["number"], "state": pull["state"],
                                      "draft": pull["draft"], "merged": pull["merged"],
                                      "url": f"https://github.com/{api.repository}/pull/{pull['number']}"}
                    entry["live_tasks"].append(task)
        result["items"].append(entry)
    for chain in sorted(selected_chains, key=lambda value: value["id"]):
        entry = {**chain_row(ledger, chain), "record_evidence": "saved", "record": deepcopy(chain)}
        if live:
            kind = "pull" if chain["kind"] == "pr" or chain["child"] else "issue"
            entry["live_pr" if kind == "pull" else "live_issue"] = observe_link(
                api, ledger["repository"], kind, chain["child"] or chain["origin"],
                chain["childNode"] if chain["child"] else chain["node"])
        result["chains"].append(entry)
    return result


def candidate(ledger, operation, control):
    items.validate_control(control)
    changed = deepcopy(ledger)
    records = changed.setdefault("workItems", [])
    record = next((entry for entry in records if entry["id"] == control["id"]), None)
    if operation == "add":
        if record is not None or any(
                entry["control"]["issue"]["repository"] == control["issue"]["repository"]
                and entry["control"]["issue"]["number"] == control["issue"]["number"] for entry in records):
            raise ValueError("issue or ID already tracked; use the explicit retry lane, not reset")
        if (control["revision"] != 1 or control["action"] != "pause" or control["reported_kind"] != "unknown"
                or control["occurrence"] is not None or control["pr_review"] is not None
                or control["authority"] != {"local_edits": False, "issue_comment": False,
                                             "draft_pr": False, "destination": None}):
            raise ValueError("new tracking items must be paused and permissionless")
        records.append(items.new_record(control))
    elif operation in {"edit", "remove"}:
        if record is None:
            raise ValueError("exact existing item ID required")
        expected = deepcopy(record["control"])
        expected.update(revision=expected["revision"] + 1, pr_review=None)
        if operation == "remove":
            expected["action"] = "pause"
        else:
            expected.update(requested_route=control["requested_route"], scope=control["scope"])
        if control != expected:
            raise ValueError("change exceeds narrow control-edit permissions")
        assignments = deepcopy(record["assignments"])
        items.reconcile(record, control)
        # Management is not delivery reconciliation. Even definitely-unsent
        # reservations and their credit holds remain owned by the execution lane.
        record["assignments"] = assignments
    else:
        raise ValueError("unsupported tracking change")
    validate_graph(changed)
    return changed


def stage(api, operation, item_id, *, issue=None, route=None, scope=None):
    items.identifier(item_id, "item ID")
    ledger = load(api)
    if api.storage_revision is None:
        raise ValueError("staged management requires already activated branch authority")
    if operation == "add":
        control = {"schema_version": 1, "id": item_id, "revision": 1, "issue": issue,
                   "reported_kind": "unknown", "requested_route": route, "action": "pause",
                   "occurrence": None, "scope": scope,
                   "authority": {"local_edits": False, "issue_comment": False,
                                 "draft_pr": False, "destination": None}, "pr_review": None}
    else:
        record = next((entry for entry in ledger.get("workItems", []) if entry["id"] == item_id), None)
        if record is None:
            raise ValueError("exact existing item ID required")
        control = deepcopy(record["control"])
        control.update(revision=control["revision"] + 1, pr_review=None)
        if operation == "remove":
            control["action"] = "pause"
        else:
            if route is None and scope is None:
                raise ValueError("edit requires --route or --scope")
            if route is not None:
                control["requested_route"] = route
            if scope is not None:
                control["scope"] = scope
    candidate(ledger, operation, control)
    return {**snapshot(api), "schema_version": 1, "operation": operation, "control": control}


def apply(api, proposal, *, expected_revision):
    contracts.exact(proposal, {"schema_version", "authority", "generation", "head", "operation", "control"},
                    "tracking change")
    ledger = load(api)
    if (type(expected_revision) is not int or type(proposal["generation"]) is not int
            or expected_revision != proposal["generation"]):
        raise ValueError("expected authority revision mismatch")
    if (type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or api.storage_revision is None
            or any(proposal[key] != value for key, value in snapshot(api).items())):
        raise ValueError("authority changed or staged change stale")
    changed = candidate(ledger, proposal["operation"], proposal["control"])
    control = proposal["control"]
    original = next((entry for entry in ledger.get("workItems", []) if entry["id"] == control["id"]), None)
    api.normal_guard()
    api.authority_guard()
    api.guard_work_item(original["control"] if original is not None else control)
    # Verify saved task/session bindings without changing receipts or settling
    # credits. Same-issue attempts may refer back to this item.
    for record in ledger.get("workItems", []):
        if record["control"]["issue"] == control["issue"]:
            for pull in pull_links(record):
                observe_link(api, pull["repository"], "pull", pull["number"], pull["node_id"])
            for _, assignment, saved in execution.executions({"workItems": [record]}):
                if saved["task_id"] is not None:
                    execution.task(api, record, deepcopy(assignment))
    api.ledger = changed
    api.control = deepcopy(control)
    if hasattr(api, "transport"):
        api.transport.control = deepcopy(control)
    storage.validate_candidate(api, changed)
    api.normal_guard()
    api.persist()
    return {"schema_version": 1, "outcome": "applied", "id": control["id"],
            "action": control["action"], "revision": control["revision"],
            "notice": "No dispatch, cancellation, task deletion or publication performed."}


def human(value):
    def safe(text):
        return "unknown" if text is None else json.dumps(str(text), ensure_ascii=True)[1:-1]
    if "operation" in value:
        control = value["control"]
        return (f"STAGED {safe(value['operation'])} {safe(control['id'])} "
                f"revision={control['revision']} action={control['action']}\n"
                f"Issue: {issue_url(control['issue'])}\n"
                f"Route: {control['requested_route']}\nScope: {safe(control['scope'])}\n"
                "PR-content approval: cleared; new adds have no effect permissions.\n"
                f"Expected authority revision: {value['generation']}\n"
                "Not applied. No task is launched or cancelled; history and credit holds are retained.")
    if "outcome" in value:
        return f"{value['outcome']} {safe(value['id'])}: action={value['action']} revision={value['revision']}"
    lines = [f"{value['evidence'].upper()} evidence; authority revision={value['generation']} head={value['head']}"]
    for item in value["items"]:
        prefix = "SAVED " if "record" in item else ""
        lines.append(f"{prefix}{safe(item['id'])}  {item['action']}  {item['state']}  {item['url']}")
        if "record" in item:
            lines.extend([f"  Route: {item['route']}; scope: {safe(item['record']['control']['scope'])}",
                          f"  Related items: {', '.join(item['related_items']) or '-'}"])
            lines.extend(f"    SAVED {safe(other['id'])}: action={other['action']} state={other['state']}"
                         for other in item["related_status"])
            for assignment in item["record"]["assignments"]:
                saved = assignment.get("execution", {})
                lines.append(f"  Assignment {safe(assignment['id'])}: delivery={assignment['delivery']} "
                             f"task={safe(saved.get('task_id'))} session={safe(saved.get('session_id'))} "
                             f"platform={safe(saved.get('task_state'))} "
                             f"credits={safe(saved.get('worker_actual'))} hold={safe(saved.get('worker_reserved'))}")
                if saved.get("pr") is not None:
                    pr = saved["pr"]
                    lines.append(f"    PR: https://github.com/{pr['repository']}/pull/{pr['number']}")
                if saved.get("attention"):
                    lines.append("    Attention: " + safe(saved["attention"]))
                if saved.get("followup") is not None:
                    followup = saved["followup"]
                    lines.append(f"    Follow-up: {followup['status']}; {safe(followup['reason'])}")
                if assignment["result"] is not None:
                    lines.append("    Result: " + safe(assignment["result"]["summary"]))
            for pull in item["record"].get("candidate_fixes", {}).get("pulls", []):
                repository = item["record"]["control"]["issue"]["repository"]
                lines.append(f"  Candidate PR: https://github.com/{repository}/pull/{pull['number']} "
                             f"{pull['state']} merged={pull['merged']} owned={pull['own']}")
            if "live_tasks" in item:
                lines.extend(f"  LIVE task {safe(task['task_id'])}: {task['state']}" for task in item["live_tasks"])
                lines.extend(f"    LIVE task PR: {task['pr']['url']} {task['pr']['state']} "
                             f"draft={task['pr']['draft']} merged={task['pr']['merged']}"
                             for task in item["live_tasks"] if task["pr"] is not None)
                lines.append(f"  LIVE issue: {item['live_issue']['state']}")
                lines.extend(f"  LIVE PR {pull['number']}: {pull['state']} draft={pull['draft']} merged={pull['merged']}"
                             for pull in item["live_prs"])
    for chain in value["chains"]:
        lines.append(f"SAVED {safe(chain['id'])}  chain  {chain['state']}  {chain['url']}")
        if chain.get("live_pr") is not None:
            lines.append("  LIVE PR: " + safe(chain["live_pr"]))
        if chain.get("live_issue") is not None:
            lines.append("  LIVE issue: " + safe(chain["live_issue"]))
    if not value["items"] and not value["chains"]:
        lines.append("No matching tracked items.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["fork", "upstream"], default="upstream")
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "show", "add", "edit", "remove", "apply"):
        child = commands.add_parser(command)
        child.add_argument("--json", action="store_true", help="Stable sorted JSON; diagnostics remain on stderr")
        if command == "list":
            child.add_argument("--all", action="store_true", help="Include paused items")
        elif command == "show":
            child.add_argument("selector")
            child.add_argument("--live", action="store_true", help="GET-only verified observation, never checkpoint")
        elif command == "apply":
            child.add_argument("file", type=Path)
            child.add_argument("--expected-revision", type=int, required=True, help="Authority generation in staged preview")
        else:
            if command == "add":
                child.add_argument("issue")
                child.add_argument("--id", required=True)
            else:
                child.add_argument("id", help="Exact item ID; URLs are inspection selectors only")
            if command in {"add", "edit"}:
                child.add_argument("--route", choices=sorted(items.ROUTES), required=command == "add")
                child.add_argument("--scope", required=command == "add")
            child.add_argument("--out", type=Path, required=True, help="New exclusive local proposal file")
    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            proposal = storage.read_host_json(secure_path(args.file), max_bytes=storage.MAX_ENVELOPE)
            with local.authority_lock(Path.home() / ".copilot" / "ci-shepherd" / "locks", args.authority):
                api = connect(args, write=True)
                value = apply(api, proposal, expected_revision=args.expected_revision)
        else:
            api = connect(args)
            if args.command == "list":
                value = listing(api, include_paused=args.all)
            elif args.command == "show":
                value = show(api, args.selector, live=args.live)
            else:
                issue = None
                if args.command == "add":
                    repository, kind, number = reference(args.issue)
                    if kind != "issues" or repository != api.repository:
                        raise ValueError("add requires an issue in the selected target repository")
                    observed = api.api.get(f"repos/{repository}/issues/{number}")
                    issue = {"repository": repository, "number": number, "node_id": observed.get("node_id")}
                    load(api)
                    api.verify_issue(issue)
                value = stage(api, args.command, args.id, issue=issue,
                              route=getattr(args, "route", None), scope=getattr(args, "scope", None))
                storage.write_host_json(args.out, value, max_bytes=storage.MAX_ENVELOPE)
        print(json.dumps(value, sort_keys=True, allow_nan=False) if args.json else human(value))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"CI Shepherd tracking failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
