"""Closed host-normalized observations, not a GitHub collector or policy engine."""

from copy import deepcopy
from datetime import datetime, timezone
import re

from round import exact


INVENTORIES = {
    "subjects", "feedback", "workers", "workersArchived", "workersUnarchived",
    "pullRequests", "history", "comments", "jobs",
}
TERMINAL_WORKERS = {"completed", "cancelled", "failed"}
POLICIES = {"drive-to-readiness", "handoff-after-first-checkpoint"}


def positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {label}")


def text(value, label, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(f"invalid {label}")


def choice(value, allowed, label):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid {label}")


def timestamp(value):
    text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("host clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_subject(value):
    exact(value, {"repository", "kind", "number"}, "subject")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ValueError("invalid repository")
    choice(value["kind"], {"issue", "pr"}, "subject kind")
    positive(value["number"], "subject number")


def unique(values, label):
    if not isinstance(values, list):
        raise ValueError(f"invalid {label} inventory")
    if len(values) != len({repr(value) for value in values}):
        raise ValueError(f"duplicate {label}")


def validate_feedback(values):
    if not isinstance(values, list):
        raise ValueError("invalid feedback inventory")
    ids = []
    for feedback in values:
        exact(feedback, {"id", "revision", "state"}, "feedback")
        text(feedback["id"], "feedback id")
        text(feedback["revision"], "feedback revision", 128 * 1024)
        choice(feedback["state"], {"open", "addressed", "declined", "needs-human"}, "feedback state")
        ids.append(feedback["id"])
    unique(ids, "feedback id")


def member(snapshot, subject):
    found = [value for value in snapshot["subjects"] if value["subject"] == subject]
    if len(found) != 1:
        raise ValueError("missing or ambiguous subject identity")
    return found[0]


def validate_snapshot(snapshot, root):
    exact(snapshot, {"schemaVersion", "root", "complete", "subjects", "workers", "managedPullRequests",
                     "history", "comments", "jobs"}, "observation")
    if type(snapshot["schemaVersion"]) is not int or snapshot["schemaVersion"] != 1:
        raise ValueError("unsupported observation schema")
    validate_subject(root)
    validate_subject(snapshot["root"])
    if snapshot["root"] != root:
        raise ValueError("observation root identity changed")
    exact(snapshot["complete"], INVENTORIES, "inventory completeness")
    # Query both archive lanes explicitly; the public preview has returned all
    # tasks when the documented default filter was omitted. Optional counts
    # in {"tasks": [...]} are not a proof that no
    # potentially nonterminal archived tasks exist. Both lanes need complete
    # host evidence; archive visibility never discards durable ownership.
    # https://github.com/github/rest-api-description/blob/main/descriptions/api.github.com/api.github.com.json
    if any(value is not True for value in snapshot["complete"].values()):
        raise ValueError("incomplete GitHub inventory")
    if not isinstance(snapshot["subjects"], list) or not snapshot["subjects"]:
        raise ValueError("missing subjects")
    ids, nodes = [], []
    for value in snapshot["subjects"]:
        exact(value, {"subject", "nodeId", "state", "managed", "labels", "revision", "feedback"}, "observed subject")
        validate_subject(value["subject"])
        if value["subject"]["repository"] != root["repository"]:
            raise ValueError("cross-repository chain is not supported")
        text(value["nodeId"], "node identity")
        text(value["revision"], "subject revision")
        if value["subject"]["kind"] == "pr" and not re.fullmatch(r"[0-9a-f]{40}", value["revision"]):
            raise ValueError("invalid PR head SHA")
        choice(value["state"], {"open", "closed"}, "subject state")
        if type(value["managed"]) is not bool:
            raise ValueError("invalid subject state")
        unique(value["labels"], "label")
        for label in value["labels"]:
            text(label, "label")
        validate_feedback(value["feedback"])
        ids.append(value["subject"])
        nodes.append(value["nodeId"])
    unique(ids, "subject")
    unique(nodes, "node identity")
    if member(snapshot, root)["managed"] is not True:
        raise ValueError("root must be managed")
    if not isinstance(snapshot["workers"], list):
        raise ValueError("invalid worker inventory")
    worker_ids = []
    for worker in snapshot["workers"]:
        exact(worker, {"id", "state", "root", "operationId"}, "worker")
        text(worker["id"], "worker id")
        text(worker["state"], "worker state")
        validate_subject(worker["root"])
        if worker["root"]["repository"] != root["repository"]:
            raise ValueError("worker inventory repository mismatch")
        if worker["operationId"] is not None:
            text(worker["operationId"], "worker operation identity")
        worker_ids.append(worker["id"])
    unique(worker_ids, "worker")
    unique(snapshot["managedPullRequests"], "managed PR")
    for number in snapshot["managedPullRequests"]:
        positive(number, "managed PR number")
    exact(snapshot["history"], {"recordIds", "publicationAttempts", "associatedOperationIds"}, "chain history")
    for key, values in snapshot["history"].items():
        unique(values, key)
        for value in values:
            positive(value, "prior comment id") if key == "recordIds" else text(value, "prior chain identity")
    if not isinstance(snapshot["comments"], list):
        raise ValueError("invalid comments inventory")
    comment_ids = []
    for comment in snapshot["comments"]:
        exact(comment, {"id", "user", "body"}, "normalized comment")
        positive(comment["id"], "comment id")
        if not isinstance(comment["body"], str):
            raise ValueError("unavailable comment body")
        if comment["user"] is not None:
            exact(comment["user"], {"id", "login"}, "normalized comment actor")
            positive(comment["user"]["id"], "comment actor id")
            text(comment["user"]["login"], "comment actor login")
        comment_ids.append(comment["id"])
    unique(comment_ids, "comment id")
    if not isinstance(snapshot["jobs"], list):
        raise ValueError("invalid jobs inventory")
    job_ids = []
    for job in snapshot["jobs"]:
        exact(job, {"subject", "headSha", "runId", "jobId", "logicalJob", "transient", "state"}, "job")
        validate_subject(job["subject"])
        target = member(snapshot, job["subject"])
        if job["subject"]["kind"] != "pr" or job["headSha"] != target["revision"]:
            raise ValueError("job does not belong to the current PR head")
        positive(job["runId"], "workflow run id")
        positive(job["jobId"], "workflow job id")
        text(job["logicalJob"], "stable logical job")
        choice(job["state"], {"queued", "in_progress", "completed", "unknown"}, "job state")
        if type(job["transient"]) is not bool:
            raise ValueError("invalid job state")
        job_ids.append(job["jobId"])
    unique(job_ids, "job id")
    normalized = deepcopy(snapshot)
    normalized["subjects"].sort(key=lambda value: (value["subject"]["kind"], value["subject"]["number"]))
    for value in normalized["subjects"]:
        value["labels"].sort()
        value["feedback"].sort(key=lambda feedback: feedback["id"])
    normalized["jobs"].sort(key=lambda job: job["jobId"])
    return normalized


def require_management(snapshot):
    # A linked PR takeover vetoes the root issue too. Check vetoes before stale
    # basis comparison so no status/progress publication follows a hard takeover.
    for value in snapshot["subjects"]:
        labels = {label.casefold() for label in value["labels"]}
        if "shepherd-hands-off" in labels:
            raise ValueError("shepherd-hands-off pauses the entire chain")
        if value["managed"]:
            if "shepherd-adopted" not in labels:
                raise ValueError("adoption removed; management paused")
            if value["state"] != "open":
                raise ValueError("managed subject is no longer open")


def freshness_basis(snapshot):
    # Full normalized feedback revisions/states, not just a head SHA or count.
    return deepcopy({"subjects": snapshot["subjects"], "jobs": snapshot["jobs"]})
