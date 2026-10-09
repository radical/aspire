"""Explicit packet receiver/checkpoint. No worker launch, push or PR publisher."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from github import LostResponse
import local
import pilot_state as state
import pilot_results
import round as contracts
import work_items as items


def load(api, read_control):
    control = items.validate_control(read_control())
    latest = api.read_authority()
    api.expected, api.ledger = deepcopy(latest), deepcopy(latest)
    api.verify_issue(control["issue"])
    return control


def fresh(api, read_control, control):
    api.guard_work_item(control)
    unchanged(read_control, control)


def unchanged(read_control, control):
    if items.validate_control(read_control()) != control:
        raise ValueError("human control changed; no new effect")


def record_for(api, control):
    records = api.ledger.setdefault("workItems", [])
    record = next((entry for entry in records if entry["id"] == control["id"]), None)
    if record is None:
        if len(records) >= items.MAX_ITEMS:
            raise ValueError("work-item authority capacity exhausted")
        record = items.new_record(control)
        records.append(record)
    else:
        items.reconcile(record, control)
    return record


def deliver(api, read_control, control, boundary, packet, emit):
    # Reserve durably before delivery intent. An interruption after intent is
    # ambiguous even if the filesystem appears empty; never launch twice.
    api.persist()
    fresh(api, read_control, control)
    boundary["delivery"] = "delivering"
    api.persist()
    fresh(api, read_control, control)
    emit(deepcopy(packet))
    boundary["delivery"] = "delivered"
    api.persist()


def comment_body(control, assignment):
    result = assignment["result"]
    safe = pilot_results.safe_text
    issue = control["issue"]
    link = f"https://github.com/{issue['repository']}/issues/{issue['number']}"
    marker = f"<!-- ci-shepherd:work-item:{control['id']}:{assignment['id']} -->"
    lines = [
        "[automated] CI Shepherd work-item result; human direction required.",
        marker, f"Linked tracker: {link}",
        f"Evaluated revision: {assignment['revision']}; current control revision: {control['revision']}.",
        f"Worker-reported classification: {safe(result['actual_classification'])}; "
        f"transience: {safe(result['transience'])}; outcome: {safe(result['outcome'])}.",
        safe(result["summary"], 2000),
        "Worker claims are not independently verified diagnosis, tests or tracker resolution.",
    ]
    lines.extend("Evidence (worker-reported): " + safe(entry, 2000) for entry in result["evidence"])
    lines.extend("Test (worker-reported): " + safe(test["command"], 2000) + " - " + test["result"]
                 for test in result["tests"])
    checked = assignment["validation"]
    if checked is not None:
        lines.append("Host validation head: `" + checked["resulting_head"] + "`.")
        lines.extend("Test (host-attested): " + safe(test["command"], 2000) + " - " + test["result"]
                     for test in checked["tests"])
    occurrence = control["occurrence"]
    if occurrence is not None:
        root = f"https://github.com/{occurrence['repository']}/actions/runs/{occurrence['run_id']}"
        lines.append(f"Pinned occurrence: {root}/attempts/{occurrence['run_attempt']}; "
                     f"job: {root}/job/{occurrence['job_id']}; head `{occurrence['head_sha']}`.")
    lines.append("No appropriate authorized PR was published. Please update this same work item's revision "
                 "and route/action to continue or direct another action. Only a human closes the tracker.")
    body = "\n\n".join(lines)
    if len(body.encode()) > 14000:
        raise ValueError("issue report exceeds bound; human attention required")
    return body


def report(api, read_control, control, record):
    assignment = record["assignments"][-1]
    comment = record["comment"]
    if comment is not None and comment["assignment_id"] == assignment["id"]:
        if comment["delivery"] in {"delivering", "uncertain"}:
            fresh(api, read_control, control)
            matches = [entry for entry in api.issue_comments(control["issue"])
                       if entry["body"] == comment["body"] and entry["owned"]]
            if len(matches) == 1:
                comment.update(delivery="delivered", comment_id=matches[0]["id"])
                api.persist()
            else:
                return {"outcome": "comment_uncertain"}
        if comment["delivery"] == "delivered":
            return {"outcome": "human_wait"}
        if comment["delivery"] == "reserved":
            comment["body"] = comment_body(control, assignment)
    else:
        comment = {"assignment_id": assignment["id"], "body": comment_body(control, assignment),
                   "delivery": "reserved", "comment_id": None}
        record["comment"] = comment
    api.persist()
    if not control["authority"]["issue_comment"] or control["action"] == "pause":
        return {"outcome": "human_wait", "comment_preview": comment["body"]}
    fresh(api, read_control, control)
    comment["delivery"] = "delivering"
    api.persist()
    fresh(api, read_control, control)
    try:
        identity = api.post_comment(control["issue"], comment["body"], lambda: unchanged(read_control, control))
    except LostResponse:
        comment["delivery"] = "uncertain"
        api.persist()
        return {"outcome": "comment_uncertain"}
    comment.update(delivery="delivered", comment_id=identity)
    api.persist()
    return {"outcome": "commented", "comment_id": identity}


def receive(api, read_control, emit):
    control = load(api, read_control)
    fresh(api, read_control, control)
    record = record_for(api, control)
    api.persist()
    assignments = record["assignments"]
    active = assignments[-1] if assignments else None
    if active is not None and active["result"] is None and active["delivery"] != "abandoned":
        if active["delivery"] == "reserved":
            packet = items.worker_packet(record, active, {
                "repository": "radical/aspire", "tracker": api.tracker,
                "comment_id": api.authority_id, "tracker_node": api.tracker_node})
            deliver(api, read_control, control, active, packet, emit)
            return {"outcome": "dispatched", "assignment_id": active["id"]}
        return {"outcome": "delivery_uncertain" if active["delivery"] == "delivering" else "waiting"}
    request = items.pr_request(record, control)
    if request is not None:
        saved = record["publication"]
        if saved is not None and saved["delivery"] != "reserved":
            if saved["delivery"] == "delivering":
                return {"outcome": "pr_request_uncertain"}
            if saved["request"]["assignment_id"] == request["assignment_id"]:
                return {"outcome": "pr_request_delivered"}
            # A new evaluated assignment and new exact human preview approval
            # are a distinct intent; old uncertainty above never permits it.
            saved = None
        if saved is None:
            saved = {"request": deepcopy(request), "control_revision": control["revision"], "delivery": "reserved"}
            record["publication"] = saved
        if saved["request"] != request:
            raise ValueError("publication request changed")
        if saved["delivery"] == "reserved":
            deliver(api, read_control, control, saved, request, emit)
            return {"outcome": "pr_requested"}
        return {"outcome": "pr_request_delivered" if saved["delivery"] == "delivered" else "pr_request_uncertain"}
    # A review-bearing run is approval-only, even if approval is stale/revoked.
    if control["pr_review"] is not None:
        return {"outcome": "human_wait", "reason": "PR approval no longer matches the immutable preview"}
    publication = record["publication"]
    if publication is not None and publication["delivery"] == "delivering":
        return {"outcome": "pr_request_uncertain"}
    new_run = (publication is not None and publication["delivery"] == "delivered"
               and active is not None and publication["request"]["assignment_id"] == active["id"]
               and control["action"] == "run" and control["revision"] > publication["control_revision"])
    if new_run:
        record["preview"] = None
    preview = None if new_run else items.preview(record, control)
    if preview is not None:
        boundary = record["preview"]
        if boundary.get("delivery") is None:
            boundary["delivery"] = "reserved"
        if boundary["delivery"] == "reserved":
            deliver(api, read_control, control, boundary, preview, emit)
        return {"outcome": "preview"}
    assignment = items.claim(record, control)
    if assignment is not None:
        packet = items.worker_packet(record, assignment, {
            "repository": "radical/aspire", "tracker": api.tracker,
            "comment_id": api.authority_id, "tracker_node": api.tracker_node})
        deliver(api, read_control, control, assignment, packet, emit)
        return {"outcome": "dispatched", "assignment_id": assignment["id"]}
    if active is not None and active["result"] is not None:
        return report(api, read_control, control, record)
    return {"outcome": "human_wait"}


def accept(api, read_control, result, worker_id, validation=None):
    control = load(api, read_control)
    fresh(api, read_control, control)
    record = record_for(api, control)
    items.checkpoint(record, result, worker_id, validation)
    fresh(api, read_control, control)
    api.persist()
    return {"outcome": "checkpointed", "evaluated_revision": result["evaluated_revision"],
            "control_revision": control["revision"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["receive", "checkpoint"])
    parser.add_argument("--item", type=Path, required=True, help="Trusted human control JSON; never worker-owned")
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    parser.add_argument("--workdir", type=Path, required=True, help="Exclusive output directory outside worker workspace")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--worker-id", help="Host-verified session identity; not a worker narrative")
    parser.add_argument("--validation", type=Path, help="Trusted host evidence, not worker claims")
    args = parser.parse_args(argv)
    if args.mode == "checkpoint" and (args.result is None or args.worker_id is None):
        parser.error("checkpoint requires --result and --worker-id")
    if args.mode == "receive" and any(value is not None for value in (args.result, args.worker_id, args.validation)):
        parser.error("result/worker/validation fields belong only to checkpoint")
    try:
        from work_item_github import WorkItemGitHub
        read_control = lambda: contracts.read_json(args.item)
        control = items.validate_control(read_control())
        token = local.selected_token()
        revision = local.command(["git", "--no-pager", "-C", str(local.ROOT), "rev-parse", "HEAD"])
        local.require_source(revision)
        local.require_idle_actions(token)
        with local.authority_lock(Path.home() / ".copilot" / "ci-shepherd" / "locks", args.authority):
            api = WorkItemGitHub(token, args.tracker, args.authority, args.tracker_node,
                                revision=revision, control=control)
            args.workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
            emit = lambda packet: contracts.write_json(args.workdir / "packet.json", packet)
            if args.mode == "receive":
                outcome = receive(api, read_control, emit)
            else:
                outcome = accept(api, read_control, contracts.read_json(args.result), args.worker_id,
                                 contracts.read_json(args.validation) if args.validation else None)
            contracts.write_json(args.workdir / "receipt.json", outcome)
            print(json.dumps(outcome, allow_nan=False))
            return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"CI Shepherd work-item receiver failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
