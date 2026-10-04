"""Authenticated remote root records, monotonic reservations and recovery."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
import uuid

import issue_pr
from round import exact, loads


MARKER = "<!-- ci-shepherd:root:v1 -->"
PREFIX = "[automated] CI Shepherd status\n" + MARKER + "\n"
MAX_RECORD_BYTES = 16 * 1024
ACTIONS = {"wait", "repair-pr", "assign-issue", "adopt-pr", "rerun-transient", "checkpoint"}
STATES = {"prepared", "reserved", "consumed", "confirmed", "failed", "uncertain"}
WORKER_ACTIONS = {"repair-pr", "assign-issue"}
TRIAL_FIELDS = ("trialId", "trialStartedAt", "expiresAt")


def trial_tuple(record):
    return {key: record[key] for key in TRIAL_FIELDS}


def validate_trial(value):
    exact(value, set(TRIAL_FIELDS), "trusted trial tuple")
    try:
        uuid.UUID(value["trialId"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid trial identity") from error
    if issue_pr.timestamp(value["expiresAt"]) != issue_pr.timestamp(value["trialStartedAt"]) + timedelta(hours=24):
        raise ValueError("trial expiry must be absolute and immutable")


@dataclass(frozen=True, init=False)
class TrialScope:
    """Trusted host authorization, never constructed from an agent decision."""
    _root: tuple
    _trial: tuple | None

    def __init__(self, root, trial):
        issue_pr.validate_subject(root)
        if trial is not None:
            validate_trial(trial)
        object.__setattr__(self, "_root", (root["repository"], root["kind"], root["number"]))
        object.__setattr__(self, "_trial", None if trial is None else tuple(trial[key] for key in TRIAL_FIELDS))

    @property
    def root(self):
        return dict(zip(("repository", "kind", "number"), self._root))

    @property
    def trial(self):
        return None if self._trial is None else dict(zip(TRIAL_FIELDS, self._trial))

    def require_root(self, root):
        issue_pr.validate_subject(root)
        if root != self.root:
            raise ValueError("root is outside the one authorized root")

    def check_record(self, root, record):
        self.require_root(root)
        if record is None:
            if self._trial is not None:
                raise ValueError("trusted trial record missing; needs-human")
        elif self._trial is None or trial_tuple(record) != self.trial:
            raise ValueError("remote trial does not match the immutable trusted trial binding")

    def start(self, root, now):
        self.check_record(root, None)
        value = {"trialId": str(uuid.uuid4()), "trialStartedAt": issue_pr.stamp(now),
                 "expiresAt": issue_pr.stamp(now + timedelta(hours=24))}
        # This one transition happens only at authorized live initialization.
        # Reconstruction supplies the pinned tuple from authenticated authority,
        # never a new clock or a newly selected root.
        object.__setattr__(self, "_trial", tuple(value[key] for key in TRIAL_FIELDS))
        return value


def require_scope(scope, root):
    if type(scope) is not TrialScope:
        raise ValueError("explicit trusted singleton trial scope required")
    scope.require_root(root)


def canonical(value):
    # Small typed identities can be compared directly, without hashing them.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_identity(identity):
    exact(identity, {"root", "subject", "nodeId", "revision", "feedback", "policy", "action", "arguments"}, "operation identity")
    issue_pr.validate_subject(identity["root"])
    issue_pr.validate_subject(identity["subject"])
    if identity["root"]["repository"] != identity["subject"]["repository"]:
        raise ValueError("operation repository mismatch")
    issue_pr.text(identity["nodeId"], "operation node identity")
    issue_pr.text(identity["revision"], "operation revision")
    if identity["subject"]["kind"] == "pr":
        import re
        if not re.fullmatch(r"[0-9a-f]{40}", identity["revision"]):
            raise ValueError("invalid operation PR head")
    issue_pr.validate_feedback(identity["feedback"])
    issue_pr.choice(identity["policy"], issue_pr.POLICIES, "operation policy")
    issue_pr.choice(identity["action"], ACTIONS - {"wait"}, "operation action")
    validate_arguments(identity["action"], identity["arguments"], identity["subject"])


def validate_arguments(action, arguments, subject):
    if action == "repair-pr":
        exact(arguments, {"feedbackIds"}, "repair arguments")
        issue_pr.unique(arguments["feedbackIds"], "repair feedback")
        if not arguments["feedbackIds"] or subject["kind"] != "pr":
            raise ValueError("repair requires PR feedback")
        for value in arguments["feedbackIds"]:
            issue_pr.text(value, "feedback id")
    elif action == "assign-issue":
        exact(arguments, set(), "assignment arguments")
        if subject["kind"] != "issue":
            raise ValueError("assignment requires an issue")
    elif action == "adopt-pr":
        exact(arguments, {"pullRequestNumber"}, "adoption arguments")
        issue_pr.positive(arguments["pullRequestNumber"], "adoption PR number")
        if subject["kind"] != "issue":
            raise ValueError("child PR adoption requires an issue root")
    elif action == "rerun-transient":
        exact(arguments, {"runId", "jobId", "logicalJob"}, "rerun arguments")
        issue_pr.positive(arguments["runId"], "rerun run id")
        issue_pr.positive(arguments["jobId"], "rerun job id")
        issue_pr.text(arguments["logicalJob"], "stable logical job")
        if subject["kind"] != "pr":
            raise ValueError("rerun requires a PR")
    elif action == "checkpoint":
        exact(arguments, set(), "checkpoint arguments")
    else:
        raise ValueError("unsupported effect action")


def rerun_key(identity):
    return {"subject": identity["subject"], "headSha": identity["revision"],
            "logicalJob": identity["arguments"]["logicalJob"]}


def validate_record(record, root=None, root_node=None):
    exact(record, {"schemaVersion", "root", "rootNodeId", "trialId", "trialStartedAt", "expiresAt",
                   "repairBatches", "reruns", "operations"}, "root record")
    if type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1:
        raise ValueError("unsupported root record schema")
    issue_pr.validate_subject(record["root"])
    issue_pr.text(record["rootNodeId"], "root node identity")
    if (root is not None and record["root"] != root) or (root_node is not None and record["rootNodeId"] != root_node):
        raise ValueError("root record identity mismatch")
    validate_trial(trial_tuple(record))
    if type(record["repairBatches"]) is not int or not 0 <= record["repairBatches"] <= 3:
        raise ValueError("invalid repair counter")
    if not isinstance(record["operations"], list) or not isinstance(record["reruns"], list):
        raise ValueError("invalid receipt inventories")
    ids, identities, repairs, expected_reruns = [], [], 0, {}
    for operation in record["operations"]:
        exact(operation, {"id", "identity", "action", "state", "result", "run", "packetId"}, "operation receipt")
        issue_pr.text(operation["id"], "operation id")
        issue_pr.text(operation["packetId"], "operation packet id")
        from round import validate_run
        validate_run(operation["run"])
        validate_identity(operation["identity"])
        identity = operation["identity"]
        if identity["root"] != record["root"] or operation["action"] != identity["action"]:
            raise ValueError("receipt identity mismatch")
        issue_pr.choice(operation["state"], STATES, "receipt state")
        if operation["state"] == "confirmed":
            exact(operation["result"], {"id", "kind"}, "effect result")
            issue_pr.text(operation["result"]["id"], "actual returned effect id")
            expected_kind = "worker" if operation["action"] in WORKER_ACTIONS else "effect"
            if operation["result"]["kind"] != expected_kind:
                raise ValueError("effect result kind mismatch")
        elif operation["result"] is not None:
            raise ValueError("unconfirmed receipt cannot claim returned IDs")
        if operation["state"] != "prepared":
            repairs += operation["action"] == "repair-pr"
            if operation["action"] == "rerun-transient":
                key = canonical(rerun_key(identity))
                expected_reruns[key] = expected_reruns.get(key, 0) + 1
        ids.append(operation["id"])
        identities.append(canonical(identity))
    issue_pr.unique(ids, "operation id")
    issue_pr.unique(identities, "operation basis")
    if repairs != record["repairBatches"]:
        raise ValueError("repair counter disagrees with durable reservations")
    actual_reruns = {}
    for value in record["reruns"]:
        exact(value, {"subject", "headSha", "logicalJob", "count"}, "rerun counter")
        issue_pr.validate_subject(value["subject"])
        issue_pr.text(value["headSha"], "rerun head")
        issue_pr.text(value["logicalJob"], "logical job")
        if type(value["count"]) is not int or not 1 <= value["count"] <= 2:
            raise ValueError("invalid rerun counter")
        key = canonical({key: value[key] for key in ("subject", "headSha", "logicalJob")})
        if key in actual_reruns:
            raise ValueError("duplicate rerun counter")
        actual_reruns[key] = value["count"]
    if actual_reruns != expected_reruns:
        raise ValueError("rerun counters disagree with durable reservations")
    if len((PREFIX + canonical(record)).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("bounded root record exceeds 16 KiB; needs-human")
    return record


def render_record(record):
    validate_record(record)
    return PREFIX + canonical(record)


def parse_body(body):
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RECORD_BYTES or not body.startswith(PREFIX):
        raise ValueError("malformed authoritative status body")
    return validate_record(loads(body[len(PREFIX):]))


def read_record(snapshot, actor):
    exact(actor, {"id", "login"}, "trusted actor")
    issue_pr.positive(actor["id"], "trusted actor id")
    issue_pr.text(actor["login"], "trusted actor login")
    owned = []
    for comment in snapshot["comments"]:
        body = comment["body"]
        if "<!-- ci-shepherd:root:" not in body:
            continue
        user = comment["user"]
        # Marker text alone is not authority. Both GitHub user ID and login
        # must match the trusted workflow actor returned by the host transport.
        if not isinstance(user, dict) or user.get("id") != actor["id"] or not isinstance(user.get("login"), str) or user["login"].casefold() != actor["login"].casefold():
            continue
        record = parse_body(body)
        validate_record(record, snapshot["root"], issue_pr.member(snapshot, snapshot["root"])["nodeId"])
        owned.append((comment["id"], record))
    if len(owned) > 1:
        raise ValueError("ambiguous authoritative status records; needs-human")
    if owned:
        comment_id, record = owned[0]
        if any(value != comment_id for value in snapshot["history"]["recordIds"]):
            raise ValueError("prior authoritative publication requires recovery")
        if any(value != record["trialId"] for value in snapshot["history"]["publicationAttempts"]):
            raise ValueError("prior trial identity requires recovery")
        known = {operation["id"] for operation in record["operations"]}
        for operation in record["operations"]:
            identity = operation["identity"]
            target = issue_pr.member(snapshot, identity["subject"])
            if target["nodeId"] != identity["nodeId"] or target["managed"] is not True:
                raise ValueError("prior managed subject identity/adoption is unverifiable; needs-human")
            if operation["action"] == "adopt-pr":
                child = {**identity["subject"], "kind": "pr", "number": identity["arguments"]["pullRequestNumber"]}
                observed_child = issue_pr.member(snapshot, child)
                if operation["state"] == "confirmed" and (
                    not observed_child["managed"] or "shepherd-adopted" not in {label.casefold() for label in observed_child["labels"]}
                ):
                    raise ValueError("prior adopted PR adoption removed; management paused")
        if set(snapshot["history"]["associatedOperationIds"]) - known:
            raise ValueError("prior operation missing from root record")
        if any(worker["root"] == snapshot["root"] and worker["operationId"] not in known for worker in snapshot["workers"]):
            raise ValueError("prior associated worker has no durable receipt; needs-human")
        return owned[0]
    if any(snapshot["history"].values()) or any(worker["root"] == snapshot["root"] for worker in snapshot["workers"]):
        raise ValueError("prior chain/status publication missing or unverifiable; needs-human")
    return None, None


def new_record(snapshot, now, scope):
    require_scope(scope, snapshot["root"])
    return {
        "schemaVersion": 1, "root": deepcopy(snapshot["root"]),
        "rootNodeId": issue_pr.member(snapshot, snapshot["root"])["nodeId"],
        **scope.start(snapshot["root"], now),
        "repairBatches": 0, "reruns": [], "operations": [],
    }


def operation_identity(packet, decision):
    target = issue_pr.member(packet["observation"], decision["subject"])
    identity = deepcopy({
        "root": packet["root"], "subject": decision["subject"], "nodeId": target["nodeId"],
        "revision": target["revision"], "feedback": target["feedback"], "policy": packet["policy"],
        "action": decision["action"], "arguments": decision["arguments"],
    })
    if identity["action"] == "repair-pr":
        identity["arguments"]["feedbackIds"].sort()
    return identity


def ensure_limits(snapshot, record, identity, now, own_id=None):
    if record is not None and now < issue_pr.timestamp(record["trialStartedAt"]):
        raise ValueError("host clock precedes trial start")
    if record is not None and now >= issue_pr.timestamp(record["expiresAt"]):
        raise ValueError("trial expired; existing reservations retain capacity")
    pending = [] if record is None else [
        value for value in record["operations"]
        if value["id"] != own_id and value["state"] in {"prepared", "reserved", "consumed", "uncertain"}
    ]
    if pending:
        raise ValueError("uncertain or nonterminal reservation holds capacity; needs-human")
    if record is not None:
        for operation in record["operations"]:
            if operation["id"] == own_id or operation["state"] != "confirmed" or operation["action"] not in WORKER_ACTIONS:
                continue
            known = [worker for worker in snapshot["workers"] if worker["id"] == operation["result"]["id"]
                     and worker["root"] == operation["identity"]["root"] and worker["operationId"] == operation["id"]]
            if len(known) != 1:
                raise ValueError("confirmed worker missing/unverifiable; unknown worker holds capacity")
    active = [value for value in snapshot["workers"] if value["state"] not in issue_pr.TERMINAL_WORKERS]
    unrelated = [value for value in active if own_id is None or value["operationId"] != own_id]
    if len(active) > 1 or unrelated:
        raise ValueError("active or unknown worker capacity exhausted")
    allocates_slot = identity["action"] == "assign-issue" or (
        identity["action"] == "adopt-pr" and identity["arguments"]["pullRequestNumber"] not in snapshot["managedPullRequests"]
    )
    if len(snapshot["managedPullRequests"]) >= 3 and allocates_slot:
        raise ValueError("managed PR capacity exhausted")
    if any(feedback["state"] == "needs-human" for value in snapshot["subjects"] for feedback in value["feedback"]):
        raise ValueError("feedback requires human resolution")
    if record is None:
        return
    if identity["action"] == "assign-issue" and any(
        operation["action"] == "assign-issue" and operation["id"] != own_id for operation in record["operations"]
    ):
        raise ValueError("issue already has an assignment intent; needs-human")
    own = next((value for value in record["operations"] if value["id"] == own_id), None)
    already_reserved = own is not None and own["state"] != "prepared"
    if identity["action"] == "repair-pr" and record["repairBatches"] - int(already_reserved) >= 3:
        raise ValueError("repair budget exhausted")
    if identity["action"] == "rerun-transient":
        key = rerun_key(identity)
        count = next((value["count"] for value in record["reruns"] if all(value[name] == key[name] for name in key)), 0)
        if count - int(already_reserved) >= 2:
            raise ValueError("rerun budget exhausted")


def reserve(record, operation):
    operation["state"] = "reserved"
    if operation["action"] == "repair-pr":
        record["repairBatches"] += 1
    elif operation["action"] == "rerun-transient":
        key = rerun_key(operation["identity"])
        existing = next((value for value in record["reruns"] if all(value[name] == key[name] for name in key)), None)
        if existing is None:
            record["reruns"].append({**key, "count": 1})
        else:
            existing["count"] += 1


def reconcile_effect(snapshot, operation):
    if operation["action"] not in WORKER_ACTIONS:
        return None
    matches = [worker for worker in snapshot["workers"] if worker["root"] == operation["identity"]["root"]
               and worker["operationId"] == operation["id"]]
    if len(matches) != 1:
        return None
    return {"id": matches[0]["id"], "kind": "worker"}
