"""Lifetime pilot authority. Logs and feedback bodies never enter this ledger."""

from datetime import timedelta
import json
import math
import uuid

import issue_pr
import round as contracts

MARKER = "<!-- ci-shepherd:pilot:v1 -->"
STATUS_MARKER = "<!-- ci-shepherd:pilot-status:v1 -->"
MAX_BODY = 60000
NATIVE_RESERVE = 30
CHAIN_ALLOWANCE = 500
REPOSITORY_ALLOWANCE = 1000
TERMINAL = {"completed", "failed", "timed_out", "cancelled"}
OP_STATES = {"reserved", "sent", "waiting", "completed", "failed", "uncertain", "no-send"}


def amount(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("invalid credit amount")
    return value


def new_ledger():
    return {"schemaVersion": 1, "repository": "radical/aspire", "cursor": 0, "chains": []}


def validate(ledger):
    contracts.exact(ledger, {"schemaVersion", "repository", "cursor", "chains"}, "pilot ledger")
    if ledger["schemaVersion"] != 1 or ledger["repository"] != "radical/aspire":
        raise ValueError("unsupported pilot authority")
    if type(ledger["cursor"]) is not int or ledger["cursor"] < 0 or not isinstance(ledger["chains"], list):
        raise ValueError("invalid pilot cursor/chains")
    nodes, subjects, operations, chains = set(), set(), set(), set()
    for chain in ledger["chains"]:
        contracts.exact(chain, {"id", "origin", "kind", "node", "child", "childNode", "state", "localAttempts",
                                "rounds", "escalated", "operations", "dispositions", "statusId", "statusPending",
                                "childAdoption"}, "chain")
        issue_pr.text(chain["id"], "chain id")
        if chain["id"] in chains:
            raise ValueError("duplicate chain identity")
        chains.add(chain["id"])
        issue_pr.text(chain["node"], "origin node")
        issue_pr.positive(chain["origin"], "origin")
        if chain["origin"] == 121 or chain["child"] == 121:
            raise ValueError("legacy authority is observation-only")
        if chain["kind"] not in {"pr", "issue"} or chain["state"] not in {"open", "closed", "hands-off", "human"}:
            raise ValueError("invalid chain kind/state")
        if chain["node"] in nodes:
            raise ValueError("duplicate origin node")
        nodes.add(chain["node"])
        for number in (chain["origin"], chain["child"]):
            if number is None:
                continue
            issue_pr.positive(number, "subject")
            if number in subjects:
                raise ValueError("subject already mapped")
            subjects.add(number)
        if (chain["child"] is None) != (chain["childNode"] is None) or (
                chain["child"] is not None and chain["kind"] != "issue"):
            raise ValueError("invalid child mapping")
        if chain["childNode"] is not None:
            issue_pr.text(chain["childNode"], "child node")
        if chain["statusId"] is not None:
            issue_pr.positive(chain["statusId"], "status id")
        if type(chain["statusPending"]) is not bool:
            raise ValueError("invalid status publication boundary")
        if chain["childAdoption"] not in {"none", "reserved", "sent", "confirmed", "uncertain"}:
            raise ValueError("invalid child adoption boundary")
        if (type(chain["localAttempts"]) is not int or not 0 <= chain["localAttempts"] <= 2
                or type(chain["rounds"]) is not int or not chain["localAttempts"] <= chain["rounds"] <= 10
                or type(chain["escalated"]) is not bool):
            raise ValueError("invalid lifetime counters")
        if not isinstance(chain["dispositions"], dict) or len(chain["dispositions"]) > 100:
            raise ValueError("feedback disposition bound")
        for key, disposition in chain["dispositions"].items():
            issue_pr.text(key, "feedback identity")
            if disposition not in {"addressed", "declined", "needs-human"}:
                raise ValueError("unsupported feedback disposition")
        if not isinstance(chain["operations"], list) or len(chain["operations"]) != chain["rounds"]:
            raise ValueError("round history mismatch")
        if chain["localAttempts"] != sum(operation.get("attemptedLocal") is True for operation in chain["operations"]):
            raise ValueError("local attempt history mismatch")
        identities = set()
        for operation in chain["operations"]:
            contracts.exact(operation, {"id", "identity", "lane", "state", "at", "nativeActual",
                                       "nativeReserved", "workerActual", "workerReserved", "taskId",
                                       "workerState", "sessionId", "workerAt", "attemptedLocal", "workerVersion"}, "pilot operation")
            issue_pr.text(operation["id"], "id")
            # Identity is a serialized head/description plus a bounded feedback
            # batch, not a single opaque ID. Real node/check IDs exceed 256 bytes.
            issue_pr.text(operation["identity"], "identity", 16384)
            if operation["id"] in operations:
                raise ValueError("duplicate operation")
            operations.add(operation["id"])
            if operation["identity"] in identities or type(operation["attemptedLocal"]) is not bool:
                raise ValueError("duplicate intent or invalid local attempt")
            identities.add(operation["identity"])
            issue_pr.timestamp(operation["at"])
            if operation["workerAt"] is not None:
                issue_pr.timestamp(operation["workerAt"])
            if operation["state"] not in OP_STATES or operation["lane"] not in {"local", "cloud"}:
                raise ValueError("invalid operation state")
            for key in ("nativeActual", "workerActual"):
                if operation[key] is not None:
                    amount(operation[key])
            for key in ("nativeReserved", "workerReserved"):
                amount(operation[key])
            for key in ("taskId", "sessionId", "workerState"):
                if operation[key] is not None:
                    issue_pr.text(operation[key], key)
            if operation["workerVersion"] is not None:
                contracts.exact(operation["workerVersion"], {"state", "session_count", "updated_at"}, "worker version")
                version = operation["workerVersion"]
                issue_pr.text(version["state"], "worker version state")
                if version["session_count"] is not None and (
                        type(version["session_count"]) is not int or version["session_count"] < 0):
                    raise ValueError("invalid worker version session count")
                if version["updated_at"] is not None:
                    issue_pr.timestamp(version["updated_at"])
    return ledger


def render(ledger):
    validate(ledger)
    body = "[automated] CI Shepherd repository authority. Do not edit.\n" + MARKER + "\n" + json.dumps(
        ledger, separators=(",", ":"), allow_nan=False)
    if len(body.encode()) > MAX_BODY:
        raise ValueError("pilot authority body bound exhausted; needs-human")
    return body


def parse(body):
    if not isinstance(body, str) or len(body.encode()) > MAX_BODY or body.count(MARKER) != 1:
        raise ValueError("missing/ambiguous/bounded pilot authority")
    value = validate(contracts.loads(body.split(MARKER + "\n", 1)[1]))
    if render(value) != body:
        raise ValueError("authority is not canonical")
    return value


def find_chain(ledger, number):
    return next((chain for chain in ledger["chains"] if number in {chain["origin"], chain["child"]}), None)


def adopt(ledger, number, kind, node):
    if number == 121:
        raise ValueError("legacy root cannot enter the pilot")
    previous = find_chain(ledger, number)
    if previous is not None:
        if previous["origin"] != number or previous["node"] != node or previous["kind"] != kind:
            raise ValueError("subject already mapped")
        return previous
    chain = {"id": str(uuid.uuid4()), "origin": number, "kind": kind, "node": node, "child": None,
             "childNode": None, "state": "open", "localAttempts": 0, "rounds": 0, "escalated": False,
             "operations": [], "dispositions": {}, "statusId": None, "statusPending": False, "childAdoption": "none"}
    ledger["chains"].append(chain)
    validate(ledger)
    return chain


def bind_child(ledger, chain, number, node):
    existing = find_chain(ledger, number)
    if number == 121 or existing is not None and existing is not chain:
        raise ValueError("child already mapped or legacy")
    if chain["kind"] != "issue" or chain["child"] not in {None, number}:
        raise ValueError("invalid child mapping")
    chain.update(child=number, childNode=node)
    validate(ledger)


def operation_spend(operation):
    return sum((operation[key] or 0) + operation[reserve]
               for key, reserve in (("nativeActual", "nativeReserved"), ("workerActual", "workerReserved")))


def chain_spend(chain):
    return sum(operation_spend(operation) for operation in chain["operations"])


def repository_spend(ledger, now):
    total = 0
    for chain in ledger["chains"]:
        for operation in chain["operations"]:
            recent = issue_pr.timestamp(operation["at"]) > now - timedelta(hours=24)
            for actual, reserve in (("nativeActual", "nativeReserved"), ("workerActual", "workerReserved")):
                # Unknown/outstanding use survives the window: age is not billing evidence.
                actual_recent = recent if actual == "nativeActual" else (
                    operation["workerAt"] is not None and issue_pr.timestamp(operation["workerAt"]) > now - timedelta(hours=24))
                total += (operation[actual] if actual_recent and operation[actual] is not None else 0) + operation[reserve]
    return total


def pending(chain):
    return chain["statusPending"] or any(operation["state"] in {"reserved", "sent", "waiting", "uncertain"}
               for operation in chain["operations"])


def worker_slots(ledger):
    return sum(operation["lane"] == "cloud" and (
        operation["workerReserved"] > 0 or operation["taskId"] is not None
    ) and operation["state"] != "no-send" and operation["workerState"] not in TERMINAL
               for chain in ledger["chains"] for operation in chain["operations"])


def reserve(ledger, chain, identity, now, *, local):
    issue_pr.text(identity, "identity", 16384)
    previous = next((operation for operation in chain["operations"] if operation["identity"] == identity), None)
    if previous is not None:
        return previous
    if pending(chain) or chain["state"] != "open":
        raise ValueError("chain has pending work or is handed off")
    if chain["rounds"] >= 10:
        raise ValueError("ten lifetime action rounds exhausted")
    if chain_spend(chain) + NATIVE_RESERVE > CHAIN_ALLOWANCE:
        raise ValueError("chain credit allowance exhausted")
    if repository_spend(ledger, now) + NATIVE_RESERVE > REPOSITORY_ALLOWANCE:
        raise ValueError("repository rolling credit allowance exhausted")
    if not local or chain["localAttempts"] >= 2:
        chain["escalated"] = True
    lane = "cloud" if chain["escalated"] else "local"
    if lane == "cloud" and worker_slots(ledger) >= 2:
        raise ValueError("cloud worker capacity exhausted")
    operation = {"id": str(uuid.uuid4()), "identity": identity, "lane": lane, "state": "reserved",
                 "at": issue_pr.stamp(now), "nativeActual": None, "nativeReserved": NATIVE_RESERVE,
                 "workerActual": None, "workerReserved": 0, "taskId": None, "workerState": None,
                 "sessionId": None, "workerAt": None, "attemptedLocal": lane == "local", "workerVersion": None}
    chain["rounds"] += 1
    chain["localAttempts"] += lane == "local"
    chain["operations"].append(operation)
    return operation


def settle_native(operation, usage):
    if usage is not None:
        amount(usage)
    if operation["nativeActual"] is not None and operation["nativeActual"] != usage:
        raise ValueError("native billing cannot be replaced")
    operation["nativeActual"] = usage
    if usage is not None:
        operation["nativeReserved"] = 0


def reserve_worker(ledger, chain, operation, now):
    if operation["lane"] != "cloud" or operation["state"] != "reserved" or worker_slots(ledger) >= 2:
        raise ValueError("worker reservation not admissible")
    room = CHAIN_ALLOWANCE - chain_spend(chain)
    if room <= 0 or repository_spend(ledger, now) + room > REPOSITORY_ALLOWANCE:
        raise ValueError("worker credit allowance exhausted")
    operation["workerReserved"] = room


def sent(operation):
    if operation["state"] != "reserved":
        raise ValueError("send boundary already consumed")
    operation["state"] = "sent"


def finish(operation, outcome):
    if outcome not in {"failed", "completed", "uncertain", "no-send", "waiting"}:
        raise ValueError("invalid outcome")
    operation["state"] = outcome
    if outcome == "no-send":
        operation.update(workerReserved=0, workerActual=0, workerState="failed")


def select(ledger, observations):
    chains = ledger["chains"]
    for offset in range(len(chains)):
        index = (ledger["cursor"] + offset) % len(chains)
        chain = chains[index]
        if chain["state"] == "open" and not pending(chain) and observations.get(
                chain["child"] or chain["origin"], {}).get("actionable") is True:
            ledger["cursor"] = (index + 1) % len(chains)
            return chain
    return None
