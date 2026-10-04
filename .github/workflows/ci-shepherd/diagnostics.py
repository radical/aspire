"""Offline inspection of a failed initialization; never authorizes recovery."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import issue_pr
import live
import receipts
import round as contracts


def diagnose_failed(audit, comment, trial, operation_id, packet_id, run):
    receipts.validate_trial(trial)
    contracts.validate_run(run)
    contracts.exact(audit, {"schemaVersion", "run", "root", "mode", "phase", "attempts", "record", "commentId"}, "failed audit")
    if (type(audit["schemaVersion"]) is not int or audit["schemaVersion"] != 1 or audit["root"] != live.ROOT
            or audit["mode"] != "live" or audit["phase"] != "failed"):
        raise ValueError("expected a failed fixed-fixture live audit")
    if audit["run"] != run:
        raise ValueError("audit run/source differs from expected provenance")
    actor = comment["user"]
    if actor["id"] != 1472 or actor["login"].casefold() != "radical":
        raise ValueError("downloaded comment actor differs from the selected fixture user")
    issue_pr.positive(comment["id"], "downloaded comment id")
    canonical = receipts.parse_body(comment["body"])
    receipts.validate_record(canonical, live.ROOT, live.PR_NODE)
    if receipts.trial_tuple(canonical) != trial:
        raise ValueError("immutable canonical trial differs from expected tuple")
    if canonical["repairBatches"] != 0 or canonical["reruns"] != [] or len(canonical["operations"]) != 1:
        raise ValueError("canonical record is not one unreserved operation")
    operation = canonical["operations"][0]
    if (operation["id"] != operation_id or operation["packetId"] != packet_id or operation["run"] != run
            or operation["state"] != "prepared" or operation["result"] is not None
            or operation["action"] != "repair-pr" or operation["identity"]["subject"] != live.ROOT
            or operation["identity"]["revision"] != live.INITIAL_HEAD):
        raise ValueError("canonical prepared operation differs from expected identity")
    attempts = audit["attempts"]
    if not isinstance(attempts, list) or len(attempts) != 3 or any(value["kind"] != "status" for value in attempts):
        raise ValueError("task/unknown attempts or incomplete status audit require investigation")
    initial = deepcopy(canonical)
    initial["operations"] = []
    reserved = deepcopy(canonical)
    receipts.reserve(reserved, reserved["operations"][0])
    expected = [
        {"kind": "status", "record": initial, "commentId": None},
        {"kind": "status", "record": canonical, "commentId": comment["id"]},
        {"kind": "status", "record": reserved, "commentId": comment["id"]},
    ]
    if attempts != expected or audit["record"] != reserved or audit["commentId"] != comment["id"]:
        raise ValueError("audit candidates do not match initialization/prepared/attempted-reserved sequence")
    # Audit.record stores the candidate before the second publication guard.
    # An attempted reserved candidate is not an authenticated published budget.
    return {"offlineOnly": True, "mayResume": False, "receiptPresent": False,
            "root": live.ROOT, "commentId": comment["id"], "trial": trial,
            "operationId": operation_id, "packetId": packet_id, "operationState": "prepared",
            "canonicalRepairBatches": 0, "attemptedRepairBatches": 1, "taskSendAudits": 0,
            "run": run, "historyDisposition": "failed run and source migration remain blocked",
            "authority": "downloaded-file consistency only; no live authentication or recovery authorization"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("comment", type=Path)
    for option in ("trial-id", "trial-started-at", "expires-at", "operation-id", "packet-id", "run-id", "workflow-sha"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    try:
        if (args.audit.parent / "receipt.json").exists():
            raise ValueError("unexpected receipt; do not infer the failed state")
        trial = {"trialId": args.trial_id, "trialStartedAt": args.trial_started_at, "expiresAt": args.expires_at}
        run = {"repository": live.REPOSITORY, "runId": args.run_id, "runAttempt": "1", "workflowSha": args.workflow_sha}
        value = diagnose_failed(contracts.read_json(args.audit), contracts.read_json(args.comment),
                                trial, args.operation_id, args.packet_id, run)
        print(json.dumps(value, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print(f"Failed-state diagnostic refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
