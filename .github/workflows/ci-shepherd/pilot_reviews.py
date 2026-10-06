"""Bounded, restart-safe Copilot review requests, separate from repair rounds."""

import re
import sys
import uuid

from github import IncompleteInventory, LostResponse, RejectedEffect, Response
import issue_pr
import pilot_authors as authors
import round as contracts

MAX_REQUESTS = 10
PENDING = {"sent", "waiting", "uncertain"}


def reviewer(user):
    return authors.copilot(user) and user["id"] == authors.REVIEWER_ID


def receipt_matches(response, api, observation, record):
    if not isinstance(response, Response) or response.status != 201 or not isinstance(response.payload, dict):
        return False
    value = response.payload
    head, base, requested = value.get("head"), value.get("base"), value.get("requested_reviewers")
    if not isinstance(head, dict) or not isinstance(base, dict) or not isinstance(requested, list):
        return False
    repository = base.get("repo")
    head_repository = head.get("repo")
    return (isinstance(repository, dict) and isinstance(head_repository, dict) and type(value.get("number")) is int
            and value["number"] == observation["number"] and value.get("node_id") == observation["node"]
            and head.get("sha") == record["head"] and repository.get("id") == api.repository_id
            and repository.get("full_name") == api.repository and head.get("ref") == observation["headRef"]
            and head_repository.get("id") == api.repository_id and head_repository.get("full_name") == api.repository
            and any(reviewer(user) for user in requested))


def validate(records):
    import pilot_state as state

    if not isinstance(records, list) or not 1 <= len(records) <= MAX_REQUESTS:
        raise ValueError("invalid review request history bound")
    heads, identities = set(), set()
    for record in records:
        contracts.exact(record, {"id", "head", "at", "state", "reserved", "actual", "reviewId"}, "review request")
        issue_pr.text(record["id"], "review request id")
        issue_pr.timestamp(record["at"])
        if (not isinstance(record["head"], str) or re.fullmatch(r"[0-9a-f]{40}", record["head"]) is None
                or record["head"] in heads or record["id"] in identities):
            raise ValueError("duplicate/invalid review request identity")
        heads.add(record["head"])
        identities.add(record["id"])
        if record["state"] not in PENDING | {"completed", "rejected", "no-send"}:
            raise ValueError("invalid review request state")
        # No per-review billing receipt exists in the documented request API.
        # Only a definitive rejection/no-send proves zero; completion does not.
        if record["state"] in {"rejected", "no-send"}:
            if (type(record["actual"]) is not int or record["actual"] != 0
                    or type(record["reserved"]) is not int or record["reserved"] != 0 or record["reviewId"] is not None):
                raise ValueError("invalid rejected review accounting")
        elif (record["actual"] is not None or type(record["reserved"]) is not int
              or record["reserved"] != state.NATIVE_RESERVE):
            raise ValueError("unknown review billing must retain admission reservation")
        if record["state"] == "completed":
            issue_pr.positive(record["reviewId"], "published review id")
        elif record["reviewId"] is not None:
            raise ValueError("unverified published review id")


def evidence(chain, value, reviews, green):
    receipts = []
    requested = any(reviewer(user) for user in value["requested_reviewers"])
    in_progress = any(reviewer(review.get("user")) and review.get("state") == "PENDING" for review in reviews)
    heads = {value["head"]["sha"]} | {record["head"] for record in chain.get("reviews", [])}
    for head in sorted(heads):
        minimum = next((issue_pr.timestamp(record["at"]) for record in chain.get("reviews", [])
                        if record["head"] == head), None)
        candidates = []
        for review in reviews:
            if (not reviewer(review.get("user")) or review.get("commit_id") != head
                    or review.get("state") not in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}):
                continue
            try:
                issue_pr.positive(review.get("id"), "published review id")
                submitted = issue_pr.timestamp(review.get("submitted_at"))
            except ValueError as error:
                raise IncompleteInventory(f"Copilot review receipt incomplete: {error}") from error
            if minimum is None or submitted >= minimum:
                candidates.append((submitted, review["id"], review["submitted_at"]))
        if candidates:
            _, identity, at = max(candidates)
            receipts.append({"head": head, "id": identity, "at": at})
    pending = [record for record in chain.get("reviews", []) if record["state"] in PENDING
               and not any(receipt["head"] == record["head"] for receipt in receipts)]
    if pending:
        # A vanished request with no published result does not establish
        # completion/failure. Keep its receipt and hold; ask for confirmation.
        status = ("uncertain" if not requested and not in_progress or any(
            record["state"] in {"sent", "uncertain"} for record in pending) else "waiting")
    elif requested or in_progress:
        status = "waiting"
    elif any(receipt["head"] == value["head"]["sha"] for receipt in receipts):
        status = "completed"
    elif any(record["head"] == value["head"]["sha"] for record in chain.get("reviews", [])):
        status = "blocked"
    elif len(chain.get("reviews", [])) >= MAX_REQUESTS:
        status = "limit"
    else:
        status = "due"
    return {"state": status, "green": green, "draft": value["draft"], "receipts": receipts,
            "requested": requested, "inProgress": in_progress}


def process(api, chain, observation, now, *, allow_request=True):
    import pilot_state as state
    from pilot_github import AuthorityUncertain

    summary = observation.get("copilotReview")
    if api.write and summary is not None:
        changed = False
        for record in chain.get("reviews", []):
            receipt = next((item for item in summary["receipts"] if item["head"] == record["head"]), None)
            if record["state"] in PENDING and receipt is not None:
                record.update(state="completed", reviewId=receipt["id"])
                changed = True
        if changed:
            api.persist()
    if (not api.write or not allow_request or summary is None or summary["state"] != "due" or not summary["green"]
            or summary["draft"] or not observation["managed"] or chain["state"] != "open"
            or observation["feedback"] or observation["attention"] is not None
            or observation["workflowAttention"] is not None or observation["approval"] is not None
            or state.pending(chain)):
        return
    if (state.chain_spend(chain) + state.NATIVE_RESERVE > state.chain_allowance(api.ledger)
            or state.repository_spend(api.ledger, now) + state.NATIVE_RESERVE > state.REPOSITORY_ALLOWANCE):
        api.admission_reasons[chain["id"]] = "Credit headroom cannot cover Copilot review admission; no request."
        return
    try:
        fresh = api.guard(chain, observation)
        if (fresh["copilotReview"]["state"] != "due" or not fresh["copilotReview"]["green"]
                or fresh["copilotReview"]["draft"]):
            raise ValueError("Copilot review CI/draft basis changed")
    except AuthorityUncertain:
        raise
    except (ValueError, IncompleteInventory) as error:
        api.admission_reasons[chain["id"]] = f"Copilot review admission paused: {error}."
        print(api.admission_reasons[chain["id"]], file=sys.stderr)
        return
    record = {"id": str(uuid.uuid4()), "head": observation["head"], "at": issue_pr.stamp(now),
              "state": "sent", "reserved": state.NATIVE_RESERVE, "actual": None, "reviewId": None}
    chain.setdefault("reviews", []).append(record)
    api.persist()
    try:
        # Reconcile saved workers again after publishing the send boundary.
        # A terminal task can have resumed since the cheap sweep.
        api.reconcile_workers(adopt_children=False)
        api.persist()
        fresh = api.guard(chain, observation)
        if (state.pending(chain, review_id=record["id"]) or not fresh["copilotReview"]["green"]
                or fresh["copilotReview"]["draft"] or fresh["copilotReview"]["requested"]
                or fresh["copilotReview"]["inProgress"]
                or any(item["head"] == record["head"] for item in fresh["copilotReview"]["receipts"])):
            raise ValueError("Copilot review work/CI/draft basis changed")
    except AuthorityUncertain:
        raise
    except (ValueError, IncompleteInventory) as error:
        record.update(state="no-send", actual=0, reserved=0)
        api.persist()
        # Releasing this reservation restores credit headroom to a resumed
        # worker. Refresh its hold before another chain can spend that room.
        api.reconcile_workers(adopt_children=False)
        api.persist()
        print(f"Copilot review not sent: {error}", file=sys.stderr)
        return
    try:
        response = api.transport("POST", f"{api.prefix}/pulls/{observation['number']}/requested_reviewers",
                                 {"reviewers": [authors.REVIEWER]})
        # GitHub returns a PR object, not the requested-reviewers GET shape.
        # https://docs.github.com/en/rest/pulls/review-requests#request-reviewers-for-a-pull-request
        if not receipt_matches(response, api, observation, record):
            raise LostResponse("Copilot review request receipt unavailable/foreign; no retry")
        record["state"] = "waiting"
    except RejectedEffect as error:
        record.update(state="rejected", actual=0, reserved=0)
        print(f"Copilot review request rejected: {error}", file=sys.stderr)
    except LostResponse as error:
        record["state"] = "uncertain"
        print(f"Copilot review request uncertain: {error}", file=sys.stderr)
    api.persist()
