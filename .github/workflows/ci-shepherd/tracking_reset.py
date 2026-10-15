"""Inactive ownership receipts: accounting and replay evidence, never agent context."""

from copy import deepcopy

import pilot_state as state
import round as contracts
import work_item_execution as execution
import work_items as items


MAX_DROPS = 20


def history(ledger):
    return [ledger, *(entry["ledger"] for entry in ledger.get("retired", []))]


def subjects(ledger):
    result = {}
    for record in ledger.get("workItems", []):
        identity = record["control"]["issue"]
        result[identity["node_id"]] = dict(identity, kind="issues")
        for assignment in record["assignments"]:
            pull = assignment.get("execution", {}).get("pr")
            if pull is not None:
                result[pull["node_id"]] = {key: pull[key] for key in ("repository", "number", "node_id")}
                result[pull["node_id"]]["kind"] = "pull"
    for receipt in ledger.get("normalRuns", []):
        identity = receipt["context"]["issue"]["identity"]
        result[identity["node_id"]] = dict(identity, kind="issues")
    for chain in ledger["chains"]:
        for number, node, kind in ((chain["origin"], chain["node"], "pull" if chain["kind"] == "pr" else "issues"),
                                   (chain["child"], chain["childNode"], "pull")):
            if number is not None:
                result[node] = {"repository": ledger["repository"], "number": number, "node_id": node, "kind": kind}
    return list(result.values())


def blocked(ledger, node):
    return any(subject["node_id"] == node for entry in ledger.get("retired", []) for subject in entry["subjects"])


def admission_id(ledger, identity):
    base = f"issue-{identity['number']}-{identity['node_id']}"
    drops = [entry for entry in ledger.get("retired", [])
             if any(subject["node_id"] == identity["node_id"] for subject in entry["subjects"])]
    return base + "-" + drops[-1]["id"] if drops else base


def closure(ledger, subject):
    """Follow ownership edges only; candidate PR links do not confer ownership."""
    selected = {subject["node_id"]}
    while True:
        before = set(selected)
        for record in ledger.get("workItems", []):
            pulls = [assignment["execution"]["pr"]["node_id"] for assignment in record["assignments"]
                     if assignment.get("execution", {}).get("pr") is not None]
            if record["control"]["issue"]["node_id"] in selected or any(node in selected for node in pulls):
                selected.add(record["control"]["issue"]["node_id"])
                selected.update(pulls)
        item_ids = {record["id"] for record in ledger.get("workItems", [])
                    if record["control"]["issue"]["node_id"] in selected}
        for chain in ledger["chains"]:
            if (chain["node"] in selected or chain["childNode"] in selected
                    or chain.get("sourceWorkItem", {}).get("item_id") in item_ids):
                selected.add(chain["node"])
                if chain["childNode"] is not None:
                    selected.add(chain["childNode"])
                source = chain.get("sourceWorkItem")
                if source is not None:
                    record = next(record for record in ledger["workItems"] if record["id"] == source["item_id"])
                    selected.add(record["control"]["issue"]["node_id"])
        if before == selected:
            break
    result = state.new_ledger(ledger["repository"])
    result["workItems"] = [deepcopy(record) for record in ledger.get("workItems", [])
                           if record["control"]["issue"]["node_id"] in selected]
    result["normalRuns"] = [deepcopy(receipt) for receipt in ledger.get("normalRuns", [])
                            if receipt["context"]["issue"]["identity"]["node_id"] in selected]
    turns = {receipt["turnId"] for receipt in result["normalRuns"] if "turnId" in receipt}
    if any(receipt.get("turnId") in turns and receipt["state"] == "reserved"
           for receipt in ledger.get("normalRuns", [])):
        raise ValueError("drop blocked by an overlapping turn")
    result["chains"] = [deepcopy(chain) for chain in ledger["chains"]
                         if chain["node"] in selected or chain["childNode"] in selected]
    if not any(result[key] for key in ("workItems", "normalRuns", "chains")):
        raise ValueError("no active Shepherd ownership or intake receipts for subject")
    state.validate(result)
    for record in result["workItems"]:
        execution.predecessors(result, record)
    return result


def safe(ledger):
    """Unknown use is not zero use; terminal sends still require billing evidence."""
    for receipt in ledger.get("normalRuns", []):
        if receipt["state"] == "reserved" or receipt["nativeReserved"] or receipt["nativeActual"] is None:
            raise ValueError("drop blocked by pending inference or unknown billing")
    for record in ledger.get("workItems", []):
        for assignment in record["assignments"]:
            saved = assignment.get("execution")
            if saved is None:
                raise ValueError("drop blocked by unreconciled packet assignment")
            if saved["state"] not in {"sent", "no_send"} or (
                    saved["task_id"] is not None and saved["task_state"] not in state.TERMINAL):
                raise ValueError("drop blocked by active or uncertain worker")
            if saved["worker_reserved"] or saved["worker_actual"] is None:
                raise ValueError("drop blocked by unknown worker billing")
            if saved.get("followup", {}).get("status") == "collecting":
                raise ValueError("drop blocked by pending collection")
        comment = record["comment"]
        if comment is not None and comment["delivery"] in {"delivering", "uncertain"}:
            raise ValueError("drop blocked by unresolved report intent")
        for key in ("preview", "publication"):
            if record[key] is not None and record[key].get("delivery") == "delivering":
                raise ValueError("drop blocked by unresolved publication intent")
        for assignment in record["assignments"]:
            tracker = assignment.get("execution", {}).get("tracker_followup")
            if tracker is not None and tracker["delivery"] in {"delivering", "uncertain"}:
                raise ValueError("drop blocked by unresolved tracker publication")
        if any(notice["state"] != "delivered" for notice in record.get("candidate_fixes", {}).get("notices", [])):
            raise ValueError("drop blocked by unresolved candidate notice")
    for chain in ledger["chains"]:
        if state.pending(chain) or chain["childAdoption"] in {"reserved", "sent", "uncertain"}:
            raise ValueError("drop blocked by pending chain effects")
        if chain.get("reminder", {}).get("sendState") in {"sent", "uncertain"}:
            raise ValueError("drop blocked by unresolved reminder")
        handoff = chain.get("handoff")
        if handoff is not None and (handoff["sendState"] != "idle" or handoff.get("appTransfer") is not None
                                    or handoff["confirmedAt"] is not None):
            raise ValueError("drop blocked by external handoff ownership")
        for operation in chain["operations"]:
            if operation.get("result", {}).get("publication") in {"sent", "uncertain"}:
                raise ValueError("drop blocked by unresolved result publication")
            if (operation["nativeReserved"] or operation["workerReserved"]
                    or operation["nativeActual"] is None
                    or operation["taskId"] is not None and (
                        operation["workerState"] not in state.TERMINAL or operation["workerActual"] is None)):
                raise ValueError("drop blocked by chain worker or unknown billing")
        if any(review["reserved"] or review["actual"] is None for review in chain.get("reviews", [])):
            raise ValueError("drop blocked by unknown review billing")


def verify(api, ledger):
    safe(ledger)
    for record, assignment, saved in execution.executions(ledger):
        if saved["task_id"] is not None:
            current = deepcopy(assignment)
            observed = execution.task(api, record, current)
            if observed["state"] not in state.TERMINAL:
                raise ValueError("retired worker resumed; reconcile before new admission")
            if (current["execution"]["worker_reserved"]
                    or current["execution"]["worker_actual"] != saved["worker_actual"]):
                raise ValueError("worker billing changed or unavailable; reconcile before drop or admission")
    for chain in ledger["chains"]:
        for operation in chain["operations"]:
            if operation["taskId"] is not None:
                observed, usage = api.task_detail(operation["taskId"], chain, operation)
                if observed["state"] not in state.TERMINAL:
                    raise ValueError("retired chain worker resumed; reconcile before new admission")
                if usage is None or usage != operation["workerActual"]:
                    raise ValueError("chain billing changed or unavailable; reconcile before drop or admission")


def guard(api, node):
    for entry in api.ledger.get("retired", []):
        if any(subject["node_id"] == node for subject in entry["subjects"]):
            verify(api, entry["ledger"])


def drop(ledger, subject, identity):
    changed = deepcopy(ledger)
    saved = closure(changed, subject)
    safe(saved)
    item_ids = {record["id"] for record in saved["workItems"]}
    receipts = {receipt["id"] for receipt in saved["normalRuns"]}
    chains = {chain["id"] for chain in saved["chains"]}
    changed["workItems"] = [record for record in changed.get("workItems", []) if record["id"] not in item_ids]
    changed["normalRuns"] = [receipt for receipt in changed.get("normalRuns", []) if receipt["id"] not in receipts]
    changed["chains"] = [chain for chain in changed["chains"] if chain["id"] not in chains]
    changed.setdefault("retired", []).append({"id": identity, "subjects": subjects(saved), "ledger": saved})
    state.validate(changed)
    return changed


def validate(ledger):
    import normal_loop
    entries = ledger.get("retired", [])
    if not isinstance(entries, list) or len(entries) > MAX_DROPS:
        raise ValueError("inactive ownership bound exhausted")
    ids = set()
    for entry in entries:
        contracts.exact(entry, {"id", "subjects", "ledger"}, "inactive ownership")
        items.identifier(entry["id"], "drop identity")
        if entry["id"] in ids or "retired" in entry["ledger"]:
            raise ValueError("duplicate or nested drop identity")
        ids.add(entry["id"])
        state.validate(entry["ledger"])
        for record in entry["ledger"].get("workItems", []):
            execution.predecessors(entry["ledger"], record)
        safe(entry["ledger"])
        if entry["ledger"]["repository"] != ledger["repository"] or entry["subjects"] != subjects(entry["ledger"]):
            raise ValueError("inactive ownership subject mismatch")
    # Dropping is not a way around retained history or ID uniqueness bounds.
    for key, identity in (("workItems", "id"), ("normalRuns", "id"), ("chains", "id")):
        values = [value for part in history(ledger) for value in part.get(key, [])]
        identities = [value[identity] for value in values]
        if len(identities) != len(set(identities)):
            raise ValueError("active/inactive identity reused")
        limit = normal_loop.MAX_RUNS if key == "normalRuns" else items.MAX_ITEMS if key == "workItems" else 100
        if len(values) > limit:
            raise ValueError("retained ownership history bound exhausted")
    normal_loop.validate_receipts([value for part in history(ledger) for value in part.get("normalRuns", [])])
    tasks = [saved["task_id"] for part in history(ledger) for _, _, saved in execution.executions(part)
             if saved["task_id"] is not None]
    if len(tasks) != len(set(tasks)):
        raise ValueError("active/inactive task identity reused")


def prior_spend(ledger, node):
    import normal_loop
    return sum(normal_loop.native_spend(entry["ledger"]) + sum(
        (saved["worker_actual"] or 0) + saved["worker_reserved"]
        for _, _, saved in execution.executions(entry["ledger"])) + sum(
        state.chain_spend(chain) - chain.get("priorSpend", 0) for chain in entry["ledger"]["chains"])
        for entry in ledger.get("retired", [])
        if any(subject["node_id"] == node for subject in entry["subjects"]))
