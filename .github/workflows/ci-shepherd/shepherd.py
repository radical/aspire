"""Inspect Shepherd tracking, stage narrow control changes, and explicitly apply them."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import uuid
from types import SimpleNamespace

import local
import pilot_binding as bindings
import pilot_github
import pilot_state as state
import pilot_storage as storage
import round as contracts
from standalone import secure_path
import work_item_execution as execution
import work_items as items
import tracking_reset


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


def resolve(args, token, environment):
    prefixes = {"fork": "CI_SHEPHERD_", "upstream": "CI_SHEPHERD_UPSTREAM_"}
    configuration = dict(environment)
    if args.tracker is None:
        names = [prefix + suffix for prefix in prefixes.values()
                 for suffix in ("TRACKER", "TRACKER_NODE", "AUTHORITY_COMMENT")]
        relevant = names if args.target is None else [prefixes[args.target] + "TRACKER"]
        if args.target is None or not any(configuration.get(name) for name in relevant):
            pages = local.metadata("repos/radical/aspire/actions/variables?per_page=100", token, paginate=True)
            for page in pages:
                for variable in page["variables"]:
                    if variable["name"] in names:
                        if not configuration.get(variable["name"]):
                            configuration[variable["name"]] = variable["value"]
    if args.target is None and args.tracker is None:
        configured = [target for target, prefix in prefixes.items() if configuration.get(prefix + "TRACKER")]
        if not configured:
            raise ValueError("configured authority missing or ambiguous: no tracker configured; supply --tracker and --target")
        if len(configured) > 1:
            raise ValueError("configured authority ambiguous: select --target fork or --target upstream")
        args.target = configured[0]
    prefix = prefixes.get(args.target)
    if (args.tracker is not None and prefix
            and str(args.tracker) != str(configuration.get(prefix + "TRACKER"))):
        prefix = None
    if args.tracker is None:
        configured_tracker = configuration.get(prefix + "TRACKER") if prefix else None
        if not configured_tracker:
            raise ValueError("tracker missing: supply --tracker or configure the selected namespace")
        args.tracker = int(configured_tracker)
    tracker = local.metadata(f"repos/radical/aspire/issues/{args.tracker}", token)
    if (tracker.get("number") != args.tracker or tracker.get("state") != "open"
            or "pull_request" in tracker or not tracker.get("node_id")):
        raise ValueError("discovery requires an open tracker issue")
    node = args.tracker_node or (configuration.get(prefix + "TRACKER_NODE") if prefix else None)
    if node is not None and node != tracker["node_id"]:
        raise ValueError("tracker node mismatch")
    args.tracker_node = tracker["node_id"]
    actor = local.metadata("user", token)
    pages = local.metadata(f"repos/radical/aspire/issues/{args.tracker}/comments?per_page=100",
                           token, paginate=True)
    owner = SimpleNamespace(actor=actor)
    candidates = [comment for page in pages for comment in page
                  if pilot_github.PilotGitHub.owned(owner, comment) and state.MARKER in comment.get("body", "")]
    if len(candidates) != 1 or not storage.is_pointer(candidates[0]["body"]):
        raise ValueError("activated authority pointer missing or ambiguous")
    comment = candidates[0]
    authority = args.authority or (configuration.get(prefix + "AUTHORITY_COMMENT") if prefix else None)
    if authority is not None and int(authority) != comment["id"]:
        raise ValueError("authority comment mismatch")
    args.authority = comment["id"]
    descriptor = contracts.loads(comment["body"][len(storage.HEADER):], max_bytes=storage.MAX_POINTER)
    if args.target is None:
        args.target = descriptor["authority"]["binding"]
    if args.target not in prefixes:
        raise ValueError("unsupported discovered target")
    return comment["body"]


def connect(args, *, write=False):
    token = local.selected_token()
    pointer = resolve(args, token, os.environ)
    revision = local.command(["git", "--no-pager", "-C", str(local.ROOT), "rev-parse", "HEAD"])
    api = TrackingGitHub(token, args.tracker, args.authority, args.tracker_node,
                         write=write, revision=revision, binding=bindings.select(args.target))
    storage.parse_pointer(pointer, storage.identity(api))
    return api


def validate_graph(ledger):
    """Reuse record/import validators, then check explicit retry links in both directions."""
    state.validate(ledger)
    for record in ledger.get("workItems", []):
        execution.predecessors(ledger, record)
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


def candidate(ledger, operation, control, retry_of=None):
    items.validate_control(control)
    changed = deepcopy(ledger)
    records = changed.setdefault("workItems", [])
    record = next((entry for entry in records if entry["id"] == control["id"]), None)
    if operation == "retry":
        if record is not None:
            raise ValueError("retry requires a new item ID")
        import normal_loop
        expected = {"schema_version": 1, "id": control["id"], "revision": 1, "issue": control["issue"],
                    "reported_kind": control["reported_kind"], "requested_route": control["requested_route"],
                    "action": "run", "occurrence": control["occurrence"], "scope": normal_loop.SCOPE,
                    "authority": {"local_edits": False, "issue_comment": False, "draft_pr": False,
                                  "destination": None}, "pr_review": None}
        if control != expected or retry_of is None:
            raise ValueError("retry must request fresh normal intake without effect permissions")
        record = items.new_record(control)
        record["retry_of"] = deepcopy(retry_of)
        records.append(record)
        history = execution.predecessors(changed, record)
        if any(control[key] != history[0][0]["control"][key]
               for key in ("requested_route", "reported_kind", "occurrence")):
            raise ValueError("retry must retain the predecessor intake identity")
        if control["occurrence"] is None:
            raise ValueError("normal retry requires a pinned failure occurrence")
        if any(entry["control"]["action"] != "pause" and not entry["assignments"]
               for entry in records if entry is not record and entry["control"]["issue"] == control["issue"]):
            raise ValueError("unassigned same-issue work requires reconciliation")
        if any("execution" not in assignment
               for entry in records if entry is not record and entry["control"]["issue"] == control["issue"]
               for assignment in entry["assignments"]):
            raise ValueError("retry requires reconciled execution history, not packet assignments")
        receipts = {(old["id"], assignment["id"]) for old, assignment in history}
        if (any((old["id"], assignment["id"]) not in receipts
                for old, assignment, saved in execution.executions(changed)
                if old is not record and old["control"]["issue"] == control["issue"] and saved["state"] != "no_send")
                or any(old is not record and old["control"]["issue"] == control["issue"]
                       and "retry_of" in old and old["id"] not in {entry["id"] for entry, _ in history}
                       for old in records)):
            raise ValueError("retry must cover the complete previous attempt chain")
    elif operation == "add":
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


def stage(api, operation, item_id, *, issue=None, route=None, scope=None, new_id=None, expected_task=None):
    items.identifier(item_id, "item ID")
    ledger = load(api)
    if api.storage_revision is None:
        raise ValueError("staged management requires already activated branch authority")
    retry_of = None
    if operation == "retry":
        items.identifier(new_id, "new retry item ID")
        items.identifier(expected_task, "expected retry task")
        old = next((entry for entry in ledger.get("workItems", []) if entry["id"] == item_id), None)
        if old is None or not old["assignments"]:
            raise ValueError("retry requires an exact previous task")
        assignment = old["assignments"][-1]
        if assignment.get("execution", {}).get("task_id") != expected_task:
            raise ValueError("retry requires an exact previous task")
        retry_of = {"item_id": old["id"], "assignment_id": assignment["id"], "task_id": expected_task}
        import normal_loop
        control = {"schema_version": 1, "id": new_id, "revision": 1, "issue": deepcopy(old["control"]["issue"]),
                   "reported_kind": old["control"]["reported_kind"],
                   "requested_route": old["control"]["requested_route"], "action": "run",
                   "occurrence": deepcopy(old["control"]["occurrence"]), "scope": normal_loop.SCOPE,
                   "authority": {"local_edits": False, "issue_comment": False,
                                 "draft_pr": False, "destination": None}, "pr_review": None}
    elif operation == "add":
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
    changed = candidate(ledger, operation, control, retry_of)
    if operation == "retry":
        for old, assignment in execution.predecessors(changed, changed["workItems"][-1]):
            execution.retryable(api, deepcopy(old), deepcopy(assignment))
    return {**snapshot(api), "schema_version": 1, "operation": operation, "control": control,
            **({"retry_of": retry_of} if retry_of is not None else {})}


def reset_subject(api, ledger, selector):
    record = next((record for record in ledger.get("workItems", []) if record["id"] == selector), None)
    receipts = [receipt["context"]["issue"]["identity"] for receipt in ledger.get("normalRuns", [])
                if receipt["item"] == selector]
    if record is not None:
        subject = dict(record["control"]["issue"], kind="issues")
    elif receipts:
        if any(identity != receipts[0] for identity in receipts):
            raise ValueError("ambiguous orphan item identity")
        subject = dict(receipts[0], kind="issues")
    else:
        repository, kind, number = reference(selector)
        if repository != api.repository:
            raise ValueError("reset requires the selected authority repository")
        value = api.api.get(f"repos/{repository}/issues/{number}")
        if (value.get("number") != number or not value.get("node_id")
                or ("pull_request" in value) != (kind == "pull")):
            raise ValueError("reset subject type/identity mismatch")
        subject = {"repository": repository, "number": number, "node_id": value["node_id"], "kind": kind}
    observe_link(api, subject["repository"], "pull" if subject["kind"] == "pull" else "issue",
                 subject["number"], subject["node_id"])
    return subject


def stage_reset(api, operation, selector):
    ledger = load(api)
    if api.storage_revision is None:
        raise ValueError("reset requires already activated branch authority")
    subject = reset_subject(api, ledger, selector)
    identity = str(uuid.uuid4())
    if operation == "drop":
        saved = tracking_reset.closure(ledger, subject)
        tracking_reset.verify(api, saved)
        changed = tracking_reset.drop(ledger, subject, identity)
        affected = {"items": [record["id"] for record in saved["workItems"]],
                    "receipts": [receipt["id"] for receipt in saved["normalRuns"]],
                    "chains": [chain["id"] for chain in saved["chains"]]}
    elif operation == "admit":
        if subject["kind"] != "pull":
            raise ValueError("explicit admission requires a PR URL; issues use the normal feed")
        tracking_reset.guard(api, subject["node_id"])
        if state.find_chain(ledger, subject["number"]) is not None:
            raise ValueError("PR already actively tracked")
        value = api.mapping(subject["number"])
        if value["node_id"] != subject["node_id"] or value["state"] != "open" or not pilot_github.managed(value):
            raise ValueError("PR admission requires an open managed PR")
        changed = deepcopy(ledger)
        state.adopt(changed, subject["number"], "pr", subject["node_id"])["id"] = identity
        affected = {"items": [], "receipts": [], "chains": [identity]}
    else:
        raise ValueError("unsupported reset operation")
    validate_graph(changed)
    return {**snapshot(api), "schema_version": 1, "operation": operation, "subject": subject,
            "id": identity, "affected": affected}


def apply_reset(api, proposal, expected_revision):
    contracts.exact(proposal, {"authority", "generation", "head", "schema_version", "operation",
                              "subject", "id", "affected"}, "tracking reset")
    ledger = load(api)
    if (type(expected_revision) is not int or type(proposal["generation"]) is not int
            or type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or expected_revision != proposal["generation"]
            or api.storage_revision is None or any(proposal[key] != value for key, value in snapshot(api).items())):
        raise ValueError("authority changed or staged reset stale")
    items.identifier(proposal["id"], "reset identity")
    subject = proposal["subject"]
    observed = reset_subject(api, ledger, f"https://github.com/{subject['repository']}/{subject['kind']}/{subject['number']}")
    if observed != subject:
        raise ValueError("reset subject identity changed")
    # Recompute closure, live safety and admission checks under the apply lock.
    current = stage_reset(api, proposal["operation"],
                          f"https://github.com/{subject['repository']}/{subject['kind']}/{subject['number']}")
    if any(current[key] != proposal[key] for key in ("authority", "generation", "head")):
        raise ValueError("authority changed or staged reset stale during revalidation")
    if proposal["operation"] == "drop":
        if current["affected"] != proposal["affected"]:
            raise ValueError("drop ownership closure changed")
        changed = tracking_reset.drop(ledger, subject, proposal["id"])
    else:
        changed = deepcopy(ledger)
        state.adopt(changed, subject["number"], "pr", subject["node_id"])["id"] = proposal["id"]
        if proposal["affected"] != {"items": [], "receipts": [], "chains": [proposal["id"]]}:
            raise ValueError("PR admission identity changed")
    api.normal_guard()
    api.authority_guard()
    api.control = None
    api.tracking_candidate = deepcopy(changed)
    if proposal["operation"] == "drop":
        saved = tracking_reset.closure(ledger, subject)
        api.tracking_guard = lambda: tracking_reset.verify(api, saved)
    else:
        api.tracking_guard = lambda: tracking_reset.guard(api, subject["node_id"])
    if hasattr(api, "transport"):
        api.transport.control = None
        api.transport.tracking_candidate = deepcopy(changed)
    api.ledger = changed
    storage.validate_candidate(api, changed)
    api.persist()
    return {"schema_version": 1, "outcome": "applied", "operation": proposal["operation"],
            "subject": subject, "affected": proposal["affected"],
            "notice": "Only Shepherd records changed; no external resource mutation or dispatch."}


def apply(api, proposal, *, expected_revision):
    if proposal.get("operation") in {"drop", "admit"}:
        return apply_reset(api, proposal, expected_revision)
    contracts.exact(proposal, {"schema_version", "authority", "generation", "head", "operation", "control"}
                    | ({"retry_of"} if proposal.get("operation") == "retry" else set()),
                    "tracking change")
    ledger = load(api)
    if (type(expected_revision) is not int or type(proposal["generation"]) is not int
            or expected_revision != proposal["generation"]):
        raise ValueError("expected authority revision mismatch")
    if (type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or api.storage_revision is None
            or any(proposal[key] != value for key, value in snapshot(api).items())):
        raise ValueError("authority changed or staged change stale")
    changed = candidate(ledger, proposal["operation"], proposal["control"], proposal.get("retry_of"))
    control = proposal["control"]
    original = next((entry for entry in ledger.get("workItems", []) if entry["id"] == control["id"]), None)
    api.normal_guard()
    api.authority_guard()
    api.guard_work_item(original["control"] if original is not None else control)
    if proposal["operation"] == "retry":
        for old, assignment in execution.predecessors(changed, changed["workItems"][-1]):
            execution.retryable(api, deepcopy(old), deepcopy(assignment))
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


def display(value):
    if value is None:
        return "unknown"
    if type(value) is bool:
        return "yes" if value else "no"
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def table(headers, rows, limits):
    cells = [[display(cell) for cell in row] for row in rows]
    widths = [min(limit, max(len(header), *(len(row[index]) for row in cells)))
              for index, (header, limit) in enumerate(zip(headers, limits))]
    lines = ["  ".join(header.ljust(width) for header, width in zip(headers, widths)).rstrip(),
             "  ".join("-" * width for width in widths)]
    for row in cells:
        wrapped = [textwrap.wrap(cell, width=width, break_on_hyphens=False) or [""]
                   for cell, width in zip(row, widths)]
        for index in range(max(len(cell) for cell in wrapped)):
            lines.append("  ".join((cell[index] if index < len(cell) else "").ljust(width)
                                   for cell, width in zip(wrapped, widths)).rstrip())
    return lines


def paragraph(label, value):
    return textwrap.wrap(f"{label}: {display(value)}", width=96,
                         subsequent_indent="  ", break_on_hyphens=False) or [f"{label}:"]


def short_link(url):
    repository, _, number = reference(url)
    return f"{repository}#{number}"


def full_links(value, records, chains):
    links = set()
    selected = {item["id"]: item for item in value["items"]}
    for item in value["items"]:
        links.add(item["url"])
        for task in item.get("live_tasks", []):
            repository, _, _ = reference(item["url"])
            links.add(f"https://github.com/{repository}/tasks/{task['task_id']}")
            if task["pr"] is not None:
                links.add(task["pr"]["url"])
    for record in records:
        if record["id"] not in selected:
            continue
        repository = record["control"]["issue"]["repository"]
        for pull in pull_links(record):
            links.add(f"https://github.com/{pull['repository']}/pull/{pull['number']}")
        for assignment in record["assignments"]:
            saved = assignment.get("execution", {})
            if saved.get("task_id") is not None:
                links.add(f"https://github.com/{repository}/tasks/{saved['task_id']}")
        # Occurrences are pinned repository/run/attempt/job identities, not
        # arbitrary worker result text containing an unverified URL.
        occurrences = [record["control"]["occurrence"],
                       *(assignment["basis"]["occurrence"] for assignment in record["assignments"])]
        for occurrence in occurrences:
            if occurrence is not None:
                links.add(f"https://github.com/{occurrence['repository']}/actions/runs/"
                          f"{occurrence['run_id']}/attempts/{occurrence['run_attempt']}/job/{occurrence['job_id']}")
    for chain in value["chains"]:
        links.add(chain["url"])
        if chain["child"] is not None:
            repository, _, _ = reference(chain["url"])
            links.add(f"https://github.com/{repository}/pull/{chain['child']}")
    selected_chains = {chain["id"] for chain in value["chains"]}
    for chain in chains:
        if chain["id"] in selected_chains:
            summary = next(entry for entry in value["chains"] if entry["id"] == chain["id"])
            repository, _, _ = reference(summary["url"])
            for operation in chain["operations"]:
                if operation["taskId"] is not None:
                    links.add(f"https://github.com/{repository}/tasks/{operation['taskId']}")
    return ["", "Full links (saved references; verified live task/PR links included with --live)",
            *("  " + display(link) for link in sorted(links))]


def human(value, *, links=False, records=(), chains=()):
    if value.get("operation") in {"drop", "admit"}:
        subject = value["subject"]
        return "\n".join([
            "APPLIED" if value.get("outcome") == "applied" else "NOT APPLIED",
            f"{value['operation'].upper()}: https://github.com/{subject['repository']}/{subject['kind']}/{subject['number']}",
            *[f"{key}: {', '.join(entries) or 'none'}" for key, entries in value["affected"].items()],
            *([f"Authority revision: {value['generation']}"] if "generation" in value else []),
            "Only Shepherd records change. External issues, PRs, tasks and comments stay unchanged.",
            "Safety/accounting receipts remain inactive; no task is launched."])
    lines = []

    def section(title, headers, rows, limits):
        lines.extend(["", title])
        lines.extend(table(headers, rows, limits) if rows else ["  None."])

    if "operation" in value:
        control = value["control"]
        lines.append(f"STAGED {display(value['operation'])} - NOT APPLIED")
        section("Proposed control", ["Field", "Value"], [
            ["Item ID", control["id"]], ["Issue", issue_url(control["issue"])],
            ["Action", control["action"]], ["Route", control["requested_route"]],
            ["Control revision", control["revision"]], ["Authority revision", value["generation"]],
            ["PR-content approval", "cleared"]], [20, 70])
        if value["operation"] == "retry":
            lines.extend(paragraph("Predecessor item", value["retry_of"]["item_id"]))
            lines.extend(paragraph("Predecessor task", value["retry_of"]["task_id"]))
            lines.append("Requests fresh normal intake; cloud execution requires its own exact approval.")
        lines.extend(["", *paragraph("Scope", control["scope"]), "",
                      "No task is launched or cancelled; history and credit holds are retained.",
                      "New adds have no effect permissions. Review the proposal before apply."])
        return "\n".join(lines)
    if "outcome" in value:
        return "\n".join([f"{display(value['outcome']).upper()} - {display(value['id'])}",
                          *table(["Action", "Control revision"], [[value["action"], value["revision"]]], [20, 20]),
                          "No dispatch, cancellation or publication performed."])

    lines.extend(["CI Shepherd tracking",
                  f"Evidence: {value['evidence'].upper()} | Authority revision: {display(value['generation'])}",
                  f"Authority head: {display(value['head'])}"])
    if value["evidence"] == "live":
        lines.append("Saved records and credit holds are unchanged; live observations appear separately.")
    else:
        lines.append("Saved snapshot only; use show --live to verify current platform state.")

    details = any("record" in item for item in value["items"]) or any("record" in chain for chain in value["chains"])
    if not details:
        section("Tracked issues", ["Item", "Issue", "Action", "Saved status", "Route", "Credits", "Hold"], [
            [index, short_link(item["url"]), item["action"], item["state"], item["route"],
             item["credits_actual"], item["credits_reserved"]]
            for index, item in enumerate(value["items"], 1)], [5, 23, 6, 13, 16, 12, 7])
        if value["items"]:
            lines.extend(["", "Item IDs (use the full ID with show)"])
            lines.extend(f"  {index}: {display(item['id'])}" for index, item in enumerate(value["items"], 1))
        section("Managed PRs / issue chains", ["Chain", "Subject", "Saved status", "Child PR"], [
            [index, short_link(chain["url"]), chain["state"], chain["child"] or "-"]
            for index, chain in enumerate(value["chains"], 1)], [5, 23, 16, 10])
        if value["chains"]:
            lines.extend(["", "Chain IDs (use the full ID with show)"])
            lines.extend(f"  {index}: {display(chain['id'])}" for index, chain in enumerate(value["chains"], 1))
        for item in value["items"]:
            if item["attention"]:
                lines.extend(["", *paragraph("Attention - " + item["id"], item["attention"])])
        if not value["items"] and not value["chains"]:
            lines.extend(["", "No matching tracked items."])
        if links:
            lines.extend(full_links(value, records, chains))
        return "\n".join(lines)

    for item in value["items"]:
        record = item["record"]
        lines.extend(["", f"Item: {display(item['id'])}", f"Issue: {item['url']}"])
        section("Saved overview", ["Action", "Status", "Route", "Credits", "Hold"],
                [[item["action"], item["state"], item["route"], item["credits_actual"], item["credits_reserved"]]],
                [8, 16, 20, 15, 12])
        lines.extend(["", *paragraph("Scope", record["control"]["scope"])])
        section("Saved assignments", ["Attempt", "Revision", "Delivery", "Platform", "Credits", "Hold"], [
            [index, assignment["revision"], assignment["delivery"],
             assignment.get("execution", {}).get("task_state"),
             assignment.get("execution", {}).get("worker_actual"),
             assignment.get("execution", {}).get("worker_reserved")]
            for index, assignment in enumerate(record["assignments"], 1)], [7, 8, 12, 16, 15, 12])
        for index, assignment in enumerate(record["assignments"], 1):
            saved = assignment.get("execution", {})
            if saved.get("attention"):
                lines.extend(paragraph(f"Attempt {index} attention", saved["attention"]))
            if saved.get("followup") is not None:
                followup = saved["followup"]
                lines.extend(paragraph(f"Attempt {index} follow-up ({followup['status']})", followup["reason"]))
            if assignment["result"] is not None:
                lines.extend(paragraph(f"Attempt {index} result", assignment["result"]["summary"]))

        section("Related attempts (saved)", ["Item ID", "Action", "Status", "Hold"], [
            [other["id"], other["action"], other["state"], other["credits_reserved"]]
            for other in item["related_status"]], [42, 8, 16, 12])
        repository = record["control"]["issue"]["repository"]
        candidates = record.get("candidate_fixes", {}).get("pulls", [])
        section("Candidate PRs (saved; not management authority)",
                ["PR", "Status", "Merged", "Owned task"], [
                    [f"{repository}#{pull['number']}", pull["state"], pull["merged"], pull["own"]]
                    for pull in candidates], [30, 12, 8, 12])
        if "live_tasks" in item:
            observations = [["Issue", short_link(item["url"]), item["live_issue"]["state"], "-", "-"]]
            observations.extend(["Task", index, task["state"], "-", "-"]
                                for index, task in enumerate(item["live_tasks"], 1))
            pulls = {pull["number"]: pull for pull in item["live_prs"]}
            pulls.update({task["pr"]["number"]: task["pr"] for task in item["live_tasks"] if task["pr"] is not None})
            observations.extend(["PR", f"{repository}#{number}", pull["state"], pull["draft"], pull["merged"]]
                                for number, pull in sorted(pulls.items()))
            section("Live observations", ["Kind", "Reference", "Status", "Draft", "Merged"],
                    observations, [8, 30, 16, 8, 8])
            if item["live_issue"]["hands_off"]:
                lines.append("Attention: source issue is marked shepherd-hands-off.")

        lines.extend(["", "Identifiers and links", f"  Item: {display(item['id'])}"])
        for index, assignment in enumerate(record["assignments"], 1):
            saved = assignment.get("execution", {})
            lines.extend([f"  Attempt {index} assignment: {display(assignment['id'])}",
                          f"  Attempt {index} task:       {display(saved.get('task_id'))}",
                          f"  Attempt {index} session:    {display(saved.get('session_id'))}"])
            if saved.get("pr") is not None:
                pr = saved["pr"]
                lines.append(f"  Saved PR: https://github.com/{pr['repository']}/pull/{pr['number']}")
        lines.extend(f"  Candidate PR: https://github.com/{repository}/pull/{pull['number']}" for pull in candidates)
        for index, task in enumerate(item.get("live_tasks", []), 1):
            lines.append(f"  Live task {index}: {display(task['task_id'])}")
            if task["pr"] is not None:
                lines.append(f"  Live task PR: {task['pr']['url']}")

    for chain in value["chains"]:
        lines.extend(["", f"Chain: {display(chain['id'])}", f"Subject: {chain['url']}"])
        section("Saved chain", ["Status", "Child PR"], [[chain["state"], chain["child"] or "-"]], [20, 15])
        for kind in ("pr", "issue"):
            observed = chain.get("live_" + kind)
            if observed is not None:
                section("Live " + kind.upper(), ["Number", "Status", "Draft", "Merged"], [
                    [observed["number"], observed["state"], observed.get("draft", "-"), observed.get("merged", "-")]],
                    [10, 16, 8, 8])
    if links:
        lines.extend(full_links(value, [item["record"] for item in value["items"]],
                                [chain["record"] for chain in value["chains"]]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["fork", "upstream"])
    parser.add_argument("--tracker", type=int)
    parser.add_argument("--authority", type=int)
    parser.add_argument("--tracker-node")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "show", "add", "edit", "remove", "retry", "drop", "admit", "apply"):
        child = commands.add_parser(command)
        child.add_argument("--json", action="store_true", help="Stable sorted JSON; diagnostics remain on stderr")
        if command in {"list", "show"}:
            child.add_argument("--links", action="store_true", help="Include full issue, PR, task and pinned run links")
        if command == "list":
            child.add_argument("--all", action="store_true", help="Include paused items")
        elif command == "show":
            child.add_argument("selector")
            child.add_argument("--live", action="store_true", help="GET-only verified observation, never checkpoint")
        elif command == "apply":
            child.add_argument("file", type=Path)
            child.add_argument("--expected-revision", type=int, required=True, help="Authority generation in staged preview")
        elif command == "retry":
            child.add_argument("previous", help="Exact immediate predecessor item ID")
            child.add_argument("--id", required=True, help="New retry item ID")
            child.add_argument("--expected-task", required=True, help="Exact predecessor task ID")
            child.add_argument("--out", type=Path, required=True, help="New exclusive local proposal file")
        elif command in {"drop", "admit"}:
            child.add_argument("selector", help="Issue/PR URL or exact item ID; admit accepts PR URLs")
            child.add_argument("--out", type=Path, required=True)
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
            api = connect(args, write=True)
            with local.authority_lock(Path.home() / ".copilot" / "ci-shepherd" / "locks", args.authority):
                value = apply(api, proposal, expected_revision=args.expected_revision)
        else:
            api = connect(args)
            if args.command == "list":
                value = listing(api, include_paused=args.all)
            elif args.command == "show":
                value = show(api, args.selector, live=args.live)
            elif args.command in {"drop", "admit"}:
                value = stage_reset(api, args.command, args.selector)
                storage.write_host_json(args.out, value, max_bytes=storage.MAX_ENVELOPE)
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
                value = stage(api, args.command, args.previous if args.command == "retry" else args.id, issue=issue,
                              route=getattr(args, "route", None), scope=getattr(args, "scope", None),
                              new_id=args.id if args.command == "retry" else None,
                              expected_task=getattr(args, "expected_task", None))
                storage.write_host_json(args.out, value, max_bytes=storage.MAX_ENVELOPE)
        if not args.json:
            print(f"Namespace: {api.binding.name} | Tracker: {api.tracker} | Authority: {api.authority_id}")
        print(json.dumps(value, sort_keys=True, allow_nan=False) if args.json else human(
            value, links=getattr(args, "links", False), records=api.ledger.get("workItems", []),
            chains=api.ledger["chains"]))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"CI Shepherd tracking failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
