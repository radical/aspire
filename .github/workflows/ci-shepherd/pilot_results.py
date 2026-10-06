"""Verified worker facts and conservative legacy completion re-evaluation."""

import pilot_state as state
import round as contracts


def basis(operation):
    # Identities contain {"head": "...", "feedback": ["check:...", ...]}:round:N.
    return contracts.loads(operation["identity"].rsplit(":round:", 1)[0])


def summarize(task, operation, pr):
    """No narrative/log endpoint is exposed by the agent-tasks response schema."""
    # https://docs.github.com/en/rest/agent-tasks/agent-tasks#get-a-task-by-repo
    kinds, errors = set(), []
    for artifact in task["artifacts"]:
        if (not isinstance(artifact, dict) or artifact.get("provider") != "github"
                or artifact.get("type") not in {"pull", "branch"} or not isinstance(artifact.get("data"), dict)):
            raise ValueError("worker result artifact unavailable")
        kind, data = artifact["type"], artifact["data"]
        if pr is not None and kind in kinds:
            raise ValueError("ambiguous worker result artifact")
        kinds.add(kind)
        if pr is not None and (
                kind == "pull" and (data.get("id") != pr["id"]
                                    or data.get("global_id") not in {None, "", pr["node_id"]})
                or kind == "branch" and data != {"base_ref": "main", "head_ref": pr["head"]["ref"]}):
            raise ValueError("worker result artifact does not match tracked PR")
    for session in task["sessions"]:
        error = session.get("error")
        if error is not None:
            if not isinstance(error, dict) or "message" in error and not isinstance(error["message"], str):
                raise ValueError("worker session error evidence unavailable")
            errors.append({"sessionId": session["id"], "message": error["message"][:500] if "message" in error else None})
    return {"operation": operation["id"], "taskId": task["id"], "state": task["state"],
            "sessionCount": task["session_count"],
            "sessionIds": [session["id"] for session in task["sessions"]],
            "updatedAt": task.get("updated_at"),
            "sessionStates": sorted({session["state"] for session in task["sessions"]}),
            "sourceHead": basis(operation)["head"],
            "artifactState": "matched" if kinds == {"pull", "branch"} and pr is not None else
                             "reported" if kinds else "not-reported",
            "errors": errors[:4], "errorsTruncated": len(errors) > 4,
            "narrativeAvailable": False}


def eligible(chain, identity, receipts):
    disposition = chain["dispositions"].get(identity)
    if disposition is None:
        return True
    if disposition != "needs-human" or chain["state"] != "open":
        return False
    for operation in reversed(chain["operations"]):
        # Authority validation permits wait provenance only on completed,
        # taskless native operations. Deferral does not supersede the earlier
        # verified worker's eligibility evidence or resolve legacy feedback.
        if "wait" in operation:
            continue
        # The admitted decision must not change its own pre-send fingerprint.
        if operation["state"] in {"reserved", "sent"} or identity not in basis(operation)["feedback"]:
            continue
        decisions = operation.get("feedbackDecisions")
        if decisions is not None:
            return (decisions[identity] == "addressed" and operation["id"] in receipts
                    and receipts[operation["id"]]["state"] in state.TERMINAL)
        if operation["taskId"] is not None:
            return (operation["id"] in receipts and receipts[operation["id"]]["state"] == "completed")
        if operation["state"] == "completed":
            # An old native handoff has no provenance field. Do not guess.
            return False
    return False


def context(chain, receipts, observation):
    result = []
    for operation in chain["operations"]:
        if operation["id"] not in receipts:
            continue
        value = dict(receipts[operation["id"]])
        value["currentHead"] = observation["head"]
        value["headChanged"] = (value["sourceHead"] != observation["head"]
                                if basis(operation)["number"] == observation["number"] else None)
        result.append(value)
    return result
