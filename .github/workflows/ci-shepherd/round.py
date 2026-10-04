"""Closed transport proof and separately guarded reconciliation contracts."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid
from copy import deepcopy
from datetime import timedelta


MAX_JSON_BYTES = 256 * 1024
PACKET_KEYS = {"schemaVersion", "run", "packetId", "nonce", "kind"}
RUN_KEYS = {"repository", "runId", "runAttempt", "workflowSha"}


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must have exactly {sorted(keys)}")
    return value


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds size limit")

    def constant(value):
        raise ValueError("invalid JSON constant")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("malformed JSON") from error


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("invalid JSON file")
    return loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def validate_run(run):
    exact(run, RUN_KEYS, "run identity")
    for key, value in run.items():
        if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
            raise ValueError(f"invalid run identity field: {key}")
    if not re.fullmatch(r"[1-9][0-9]*", run["runAttempt"]):
        raise ValueError("invalid run attempt")
    if not re.fullmatch(r"[0-9a-f]{40}", run["workflowSha"]):
        raise ValueError("workflow revision must be an immutable SHA")


def validate_packet(packet):
    exact(packet, PACKET_KEYS, "packet")
    if type(packet["schemaVersion"]) is not int or packet["schemaVersion"] != 1:
        raise ValueError("unsupported schemaVersion")
    validate_run(packet["run"])
    if packet["kind"] != "transport-proof":
        raise ValueError("unsupported packet kind")
    for key, pattern in [("packetId", r"[0-9a-f-]{36}"), ("nonce", r"[0-9a-f]{32}")]:
        if not isinstance(packet[key], str) or not re.fullmatch(pattern, packet[key]):
            raise ValueError(f"invalid {key}")


def prepare(directory, run, *, native_session=False):
    validate_run(run)
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    trusted = directory / "trusted"
    trusted.mkdir(mode=0o700)
    packet = {
        "schemaVersion": 1, "run": dict(run), "packetId": str(uuid.uuid4()),
        "nonce": secrets.token_hex(16), "kind": "transport-proof",
    }
    envelope = {"schemaVersion": 1, "packet": packet, "sessionId": None if native_session else str(uuid.uuid4())}
    write_json(trusted / "packet.json", packet)
    write_json(trusted / "envelope.json", envelope)
    return packet, envelope


def safe_output(value):
    # v0.89.17 collect_ndjson_output.cjs emits {"items": [...], "errors": [...]};
    # a filtered valid item must not hide a rejected extra item or ingestion error.
    exact(value, {"items", "errors"}, "safe output")
    if value["errors"] != []:
        raise ValueError("safe output ingestion reported errors")
    if not isinstance(value["items"], list) or len(value["items"]) != 1:
        raise ValueError("safe output must contain exactly one decision")
    item = exact(value["items"][0], {"type", "decision"}, "safe output item")
    if item["type"] != "submit_decision" or not isinstance(item["decision"], str):
        raise ValueError("unexpected safe output")
    return loads(item["decision"])


def validate_decision(envelope, decision, run):
    exact(envelope, {"schemaVersion", "packet", "sessionId"}, "trusted envelope")
    if type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1:
        raise ValueError("unsupported envelope schemaVersion")
    validate_packet(envelope["packet"])
    validate_run(run)
    if envelope["packet"]["run"] != run:
        raise ValueError("trusted envelope belongs to a different host run")
    if not isinstance(envelope["sessionId"], str):
        raise ValueError("invalid host session")
    uuid.UUID(envelope["sessionId"])
    exact(decision, PACKET_KEYS | {"outcome"}, "decision")
    validate_packet({key: decision[key] for key in PACKET_KEYS})
    if any(decision[key] != envelope["packet"][key] for key in PACKET_KEYS):
        raise ValueError("decision does not match the trusted prepare packet")
    if decision["outcome"] != "wait":
        raise ValueError("only the no-effect wait outcome is supported")


def apply(envelope, decision, run, receipt_path):
    validate_decision(envelope, decision, run)
    receipt = {
        "schemaVersion": 1, "run": dict(run), "packetId": decision["packetId"],
        "nonce": decision["nonce"], "sessionId": envelope["sessionId"],
        "outcome": "wait", "effects": [],
    }
    write_json(receipt_path, receipt)
    return receipt


def prepare_reconciliation(github, root, subject, run, clock, scope, *, policy="drive-to-readiness"):
    import issue_pr
    import receipts

    validate_run(run)
    issue_pr.validate_subject(root)
    receipts.require_scope(scope, root)
    issue_pr.validate_subject(subject)
    issue_pr.choice(policy, issue_pr.POLICIES, "policy")
    if run["repository"] != root["repository"]:
        raise ValueError("host run repository or policy mismatch")
    snapshot = issue_pr.validate_snapshot(github.refresh(root), root)
    issue_pr.require_management(snapshot)
    target = issue_pr.member(snapshot, subject)
    if not target["managed"]:
        raise ValueError("decision subject must already be adopted")
    comment_id, record = receipts.read_record(snapshot, github.actor)
    scope.check_record(root, record)
    now = clock()
    packet_id = str(uuid.uuid4())
    basis = {
        "packetId": packet_id, "run": deepcopy(run), "policy": policy, "root": deepcopy(root),
        "nodeId": target["nodeId"], "revision": target["revision"], "feedback": deepcopy(target["feedback"]),
    }
    packet = {
        "schemaVersion": 1, "kind": "reconciliation", "packetId": packet_id, "run": deepcopy(run),
        "preparedAt": issue_pr.stamp(now), "validUntil": issue_pr.stamp(now + timedelta(minutes=10)),
        "root": deepcopy(root), "subject": deepcopy(subject), "policy": policy, "basis": basis,
        "observation": deepcopy(snapshot), "record": {"commentId": comment_id, "value": deepcopy(record)},
        "evidence": [{"id": f"subject-{subject['number']}", "subject": deepcopy(subject),
                      "revision": target["revision"]}],
    }
    validate_reconciliation_packet(packet, run)
    return packet


def validate_reconciliation_packet(packet, run):
    import issue_pr
    import receipts

    exact(packet, {"schemaVersion", "kind", "packetId", "run", "preparedAt", "validUntil", "root", "subject",
                   "policy", "basis", "observation", "record", "evidence"}, "reconciliation packet")
    if len(json.dumps(packet, allow_nan=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("reconciliation packet exceeds size limit")
    if type(packet["schemaVersion"]) is not int or packet["schemaVersion"] != 1 or packet["kind"] != "reconciliation":
        raise ValueError("unsupported reconciliation packet")
    validate_run(run)
    validate_run(packet["run"])
    issue_pr.validate_subject(packet["root"])
    if packet["run"] != run or packet["root"]["repository"] != run["repository"]:
        raise ValueError("packet belongs to a different host run")
    try:
        uuid.UUID(packet["packetId"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid reconciliation packet id") from error
    if issue_pr.timestamp(packet["validUntil"]) != issue_pr.timestamp(packet["preparedAt"]) + timedelta(minutes=10):
        raise ValueError("invalid packet validity window")
    issue_pr.validate_subject(packet["subject"])
    issue_pr.validate_snapshot(packet["observation"], packet["root"])
    issue_pr.require_management(packet["observation"])
    target = issue_pr.member(packet["observation"], packet["subject"])
    issue_pr.choice(packet["policy"], issue_pr.POLICIES, "policy")
    if not target["managed"]:
        raise ValueError("unmanaged target or unsupported policy")
    expected = {
        "packetId": packet["packetId"], "run": packet["run"], "policy": packet["policy"], "root": packet["root"],
        "nodeId": target["nodeId"], "revision": target["revision"], "feedback": target["feedback"],
    }
    if packet["basis"] != expected:
        raise ValueError("packet basis does not match host observations")
    exact(packet["record"], {"commentId", "value"}, "prepared record")
    # Actor authentication is performed against refreshed GitHub observations by
    # prepare/apply. Here only the closed packet's internal shapes are checked.
    if packet["record"]["value"] is not None:
        issue_pr.positive(packet["record"]["commentId"], "prepared comment id")
        receipts.validate_record(packet["record"]["value"], packet["root"],
                                 issue_pr.member(packet["observation"], packet["root"])["nodeId"])
    elif packet["record"]["commentId"] is not None:
        raise ValueError("prepared comment identity lacks a record")
    if not isinstance(packet["evidence"], list) or not packet["evidence"]:
        raise ValueError("missing host evidence")
    ids = []
    for evidence in packet["evidence"]:
        exact(evidence, {"id", "subject", "revision"}, "host evidence reference")
        issue_pr.text(evidence["id"], "evidence id")
        observed = issue_pr.member(packet["observation"], evidence["subject"])
        if evidence["revision"] != observed["revision"]:
            raise ValueError("evidence revision mismatch")
        ids.append(evidence["id"])
    issue_pr.unique(ids, "evidence id")


def validate_reconciliation_decision(packet, decision, run):
    import issue_pr
    import receipts

    validate_reconciliation_packet(packet, run)
    if not isinstance(decision, dict) or not isinstance(decision.get("action"), str) or decision["action"] not in receipts.ACTIONS:
        raise ValueError("unsupported reconciliation action")
    keys = {"schemaVersion", "subject", "basis", "action", "reason", "evidenceIds"}
    exact(decision, keys if decision["action"] == "wait" else keys | {"arguments"}, "reconciliation decision")
    if type(decision["schemaVersion"]) is not int or decision["schemaVersion"] != 1:
        raise ValueError("unsupported decision schema")
    issue_pr.validate_subject(decision["subject"])
    exact(decision["basis"], {"packetId", "run", "policy", "root", "nodeId", "revision", "feedback"}, "decision basis")
    issue_pr.validate_subject(decision["basis"]["root"])
    validate_run(decision["basis"]["run"])
    if decision["subject"] != packet["subject"] or decision["basis"] != packet["basis"]:
        raise ValueError("decision subject/basis does not match the host packet")
    issue_pr.text(decision["reason"], "decision reason", 1024)
    issue_pr.unique(decision["evidenceIds"], "decision evidence id")
    for value in decision["evidenceIds"]:
        issue_pr.text(value, "decision evidence id")
    available = {evidence["id"] for evidence in packet["evidence"] if evidence["subject"] == decision["subject"]}
    if not decision["evidenceIds"] or any(value not in available for value in decision["evidenceIds"]):
        raise ValueError("decision evidence not drawn from host prepare")
    if decision["action"] == "wait":
        return
    action, args = decision["action"], decision["arguments"]
    receipts.validate_arguments(action, args, decision["subject"])
    if action in {"assign-issue", "adopt-pr"} and decision["subject"] != packet["root"]:
        raise ValueError("issue lifecycle action must target the originating root")
    target = issue_pr.member(packet["observation"], decision["subject"])
    if action == "repair-pr":
        available_feedback = {value["id"] for value in target["feedback"] if value["state"] == "open"}
        if set(args["feedbackIds"]) - available_feedback:
            raise ValueError("repair feedback not drawn from host prepare")
    elif action == "adopt-pr":
        child = {**decision["subject"], "kind": "pr", "number": args["pullRequestNumber"]}
        observed = issue_pr.member(packet["observation"], child)
        if observed["managed"] or observed["state"] != "open":
            raise ValueError("adoption requires a linked open unmanaged PR")
    elif action == "rerun-transient":
        matches = [job for job in packet["observation"]["jobs"] if job["subject"] == decision["subject"]
                   and all(job[key] == args[key] for key in args)]
        if len(matches) != 1 or matches[0]["transient"] is not True or matches[0]["state"] != "completed":
            raise ValueError("rerun requires a host-verified current-head transient job")


def apply_reconciliation(packet, decision, run, github, clock, scope, *, executor=None, dry_run=True, evidence=None, resume=None):
    """Shared host-only mechanism with explicitly injected effect capabilities."""
    import issue_pr
    import receipts
    from github import LostResponse, RejectedEffect

    validate_reconciliation_decision(packet, decision, run)
    root = packet["root"]
    receipts.require_scope(scope, root)
    high_water = issue_pr.timestamp(packet["preparedAt"])
    valid_until = issue_pr.timestamp(packet["validUntil"])

    def checked_clock():
        nonlocal high_water
        now = issue_pr.timestamp(issue_pr.stamp(clock()))
        # Compare every read, including initialization and limit checks, not
        # only refreshes against prepare time. Rollback cannot renew authority.
        if now < high_water:
            raise ValueError("host clock moved backwards")
        if now >= valid_until:
            raise ValueError("packet expired")
        trial = scope.trial
        if trial is not None and now < issue_pr.timestamp(trial["trialStartedAt"]):
            raise ValueError("host clock precedes trial start")
        high_water = now
        return now

    def observe():
        snapshot = issue_pr.validate_snapshot(github.refresh(root), root)
        issue_pr.require_management(snapshot)
        if issue_pr.freshness_basis(snapshot) != issue_pr.freshness_basis(packet["observation"]):
            raise ValueError("head, identity or full feedback basis changed")
        checked_clock()
        return snapshot

    snapshot = observe()
    comment_id, record = receipts.read_record(snapshot, github.actor)
    scope.check_record(root, record)
    prepared_record = packet["record"]["value"]
    if prepared_record is not None and (record is None or record["trialId"] != prepared_record["trialId"]
                                        or comment_id != packet["record"]["commentId"]):
        raise ValueError("prepared prior root record is missing or replaced; needs-human")
    if prepared_record is not None:
        if any(record[key] != prepared_record[key] for key in ("rootNodeId", "trialStartedAt", "expiresAt")):
            raise ValueError("immutable trial identity changed; needs-human")
        current_operations = {value["id"]: value for value in record["operations"]}
        for old in prepared_record["operations"]:
            current = current_operations.get(old["id"])
            if current is None or any(current[key] != old[key] for key in ("identity", "action", "run", "packetId")):
                raise ValueError("durable operation history changed; needs-human")
        if record["repairBatches"] < prepared_record["repairBatches"]:
            raise ValueError("durable budget moved backwards; needs-human")
    if decision["action"] == "wait":
        return {"outcome": "wait", "effects": []}
    identity = receipts.operation_identity(packet, decision)
    if resume is not None:
        from recovery import PreparedResume
        if type(resume) is not PreparedResume:
            raise ValueError("explicit typed prepared recovery required")
    previous, recovery_result = None, None
    if record is not None:
        previous = next((value for value in record["operations"] if value["identity"] == identity), None)
        if previous is not None:
            # A reservation proves neither execution nor failure. Replays never
            # POST again, including after a lost process or deleted local cache.
            if previous["state"] in {"consumed", "uncertain"}:
                recovery_result = receipts.reconcile_effect(snapshot, previous)
            resuming = resume is not None and previous["state"] == "prepared" and not dry_run
            if resuming:
                resume.check(github, packet, snapshot, record)
            elif recovery_result is None or dry_run:
                return {"outcome": "replay" if previous["state"] in {"confirmed", "failed"} else "needs-human",
                        "effects": [], "operation": deepcopy(previous)}
        if previous is None:
            recoverable = [
                (value, receipts.reconcile_effect(snapshot, value)) for value in record["operations"]
                if value["state"] in {"consumed", "uncertain"}
            ]
            recoverable = [(value, result) for value, result in recoverable if result is not None]
            if len(recoverable) > 1:
                raise ValueError("ambiguous pending effect recovery; needs-human")
            if recoverable:
                # Record an established past outcome before considering a new
                # head/action. This does not retry the effect or spend a budget.
                previous, recovery_result = recoverable[0]
                identity = previous["identity"]
                if dry_run:
                    return {"outcome": "needs-human", "effects": [], "operation": deepcopy(previous)}
    receipts.ensure_limits(snapshot, record, identity, checked_clock(), previous["id"] if previous is not None else None)
    if dry_run:
        return {"outcome": "dry-run", "effects": [], "operation": identity}
    if executor is None:
        raise ValueError("effect handler is not installed")
    if github.write_enabled is not True:
        raise ValueError("local mode has no hosted writer capability")
    import reasoning
    reasoning.validate_reconciliation_evidence(packet, decision, run, evidence)
    if recovery_result is None and callable(getattr(executor, "validate", None)):
        executor.validate(receipts.operation_identity(packet, decision))
    # expected is the last authenticated canonical remote value, never a local
    # cache. Sole hosted concurrency provides serialization, not a fake CAS.
    expected = deepcopy(record)
    own_id = previous["id"] if previous is not None else None
    initializing = False

    def guard():
        current = observe()
        current_id, current_record = receipts.read_record(current, github.actor)
        scope.check_record(root, record if initializing else current_record)
        if current_id != comment_id or current_record != expected:
            raise ValueError("remote authority changed before mutation; needs-human")
        receipts.ensure_limits(current, record if initializing else current_record, identity, checked_clock(), own_id)
        if resume is not None:
            resume.check(github, packet, current, record)
        return current

    def persist(candidate):
        nonlocal comment_id, expected
        body = receipts.render_record(candidate)
        try:
            returned = github.publish_status(root, body, comment_id, guard)
        except LostResponse as error:
            # Read-only recovery is allowed even during takeover/expiry. It
            # authorizes no subsequent mutation without a fresh full guard.
            recovered = issue_pr.validate_snapshot(github.refresh(root), root)
            found_id, found = receipts.read_record(recovered, github.actor)
            scope.check_record(root, found)
            if found != candidate or (comment_id is not None and found_id != comment_id):
                raise ValueError("status publication uncertain; needs-human; zero retry") from error
            returned = {"id": found_id}
        if type(returned.get("id")) is not int or returned["id"] <= 0:
            raise ValueError("status publication identity uncertain; needs-human")
        comment_id = returned["id"]
        expected = deepcopy(candidate)

    if recovery_result is not None:
        record = deepcopy(record)
        recovered_operation = next(value for value in record["operations"] if value["id"] == own_id)
        recovered_operation.update(state="confirmed", result=recovery_result)
        persist(record)
        return {"outcome": "confirmed", "effects": [], "recovered": True, "operation": deepcopy(recovered_operation)}
    if record is None:
        # Initialization requires complete independent history, not merely an
        # empty comment list. A deleted/unverifiable publication cannot restart.
        record = receipts.new_record(snapshot, checked_clock(), scope)
        initializing = True
        try:
            persist(record)
        finally:
            initializing = False
    else:
        record = deepcopy(record)
    if previous is not None:
        operation = next(value for value in record["operations"] if value["id"] == previous["id"])
    else:
        operation = {
            "id": str(uuid.uuid4()), "identity": identity, "action": decision["action"],
            "state": "prepared", "result": None, "run": deepcopy(run), "packetId": packet["packetId"],
        }
        record["operations"].append(operation)
        own_id = operation["id"]
        persist(record)
    own_id = operation["id"]
    receipts.reserve(record, operation)
    persist(record)
    # Persist the send boundary before the effect. Process loss here is
    # indistinguishable from a lost POST response and must hold capacity.
    operation["state"] = "consumed"
    persist(record)
    dispatch_snapshot = guard()
    rejected = False
    try:
        if callable(getattr(executor, "bind", None)):
            executor.bind(deepcopy(operation), receipts.trial_tuple(record), dispatch_snapshot, guard)
        result = executor.execute(deepcopy(operation))
    except RejectedEffect:
        rejected, result = True, None
    except LostResponse:
        recovery = issue_pr.validate_snapshot(github.refresh(root), root)
        result = receipts.reconcile_effect(recovery, operation)
    if result is None:
        operation["state"] = "failed" if rejected else "uncertain"
    else:
        exact(result, {"id", "kind"}, "actual effect response")
        issue_pr.text(result["id"], "returned effect ID")
        if result["kind"] != ("worker" if operation["action"] in receipts.WORKER_ACTIONS else "effect"):
            raise ValueError("actual effect response has an uncertain identity")
        operation.update(state="confirmed", result=deepcopy(result))
    persist(record)
    return {"outcome": "confirmed" if result is not None else ("failed" if rejected else "needs-human"),
            "effects": [] if result is None else [deepcopy(result)], "operation": deepcopy(operation)}


def smoke(directory, run, executable, *, process=None, provider_env=None):
    import reasoning

    packet, envelope = prepare(directory, run)
    directory = Path(directory)
    try:
        decision, report = reasoning.execute(directory / "agent", packet, envelope["sessionId"],
                                             executable, process=process, provider_env=provider_env)
        trusted = read_json(directory / "trusted" / "envelope.json")
        validate_decision(trusted, decision, run)
        write_json(directory / "reasoning-report.json", report)
        write_json(directory / "decision.json", decision)
        # Saved decisions are diagnostic; apply consumes the validated in-memory result.
        return apply(trusted, decision, run, directory / "receipt.json")
    except (ValueError, OSError) as error:
        write_json(directory / "failure.json", {"schemaVersion": 1, "stage": "reason-or-validate", "error": str(error)})
        raise


def host_run():
    return {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "runId": os.environ["GITHUB_RUN_ID"],
        "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "workflowSha": os.environ["GITHUB_WORKFLOW_SHA"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("smoke")
    local.add_argument("--workdir", type=Path, required=True)
    local.add_argument("--copilot", default="copilot")
    local.add_argument("--workflow-sha", required=True)
    pre = commands.add_parser("prepare")
    pre.add_argument("--workdir", type=Path, required=True)
    post = commands.add_parser("apply")
    post.add_argument("--trusted", type=Path, required=True)
    post.add_argument("--evidence", type=Path, required=True)
    post.add_argument("--decision", type=Path, required=True)
    post.add_argument("--receipt", type=Path, required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--trusted", type=Path, required=True)
    collect.add_argument("--session-root", type=Path, required=True)
    collect.add_argument("--logs", type=Path, required=True)
    collect.add_argument("--out", type=Path, required=True)
    collect.add_argument("--outcome", choices=["success", "failure", "cancelled", "skipped"], required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            if args.workdir.is_absolute() or ".." in args.workdir.parts:
                raise ValueError("workdir must be a new relative workspace directory")
            run = {"repository": "local/ci-shepherd", "runId": str(uuid.uuid4()),
                   "runAttempt": "1", "workflowSha": args.workflow_sha}
            smoke(args.workdir, run, args.copilot)
        elif args.command == "prepare":
            packet, _ = prepare(args.workdir, host_run(), native_session=True)
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"packet={json.dumps(packet, separators=(',', ':'))}\n")
        elif args.command == "collect":
            import reasoning
            envelope = read_json(args.trusted / "envelope.json")
            if envelope["packet"]["run"] != host_run():
                raise ValueError("prepare envelope belongs to another run")
            evidence = reasoning.collect(args.session_root, args.logs, args.outcome, args.out)
            observed, _ = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
            if envelope["packet"]["kind"] == "reconciliation":
                validate_reconciliation_decision(envelope["packet"], observed, host_run())
            else:
                validate_decision({**envelope, "sessionId": evidence["sessionId"]}, observed, host_run())
        else:
            import reasoning
            envelope = read_json(args.trusted / "envelope.json")
            if read_json(args.trusted / "packet.json") != envelope["packet"]:
                raise ValueError("trusted prepare artifact packet substitution")
            decision = safe_output(read_json(args.decision))
            evidence = read_json(args.evidence)
            if envelope["sessionId"] is not None:
                raise ValueError("native hosted transport requires an engine-created fresh session")
            envelope = {**envelope, "sessionId": evidence["sessionId"]}
            observed, _ = reasoning.validate_evidence(evidence, envelope["sessionId"], hosted=True)
            if observed != decision:
                raise ValueError("safe output does not match the actual final decision")
            validate_decision(envelope, decision, host_run())
            apply(envelope, decision, host_run(), args.receipt)
        return 0
    except (ValueError, OSError, KeyError) as error:
        if args.command in {"collect", "apply"}:
            failure_path = (args.out if args.command == "collect" else args.receipt).parent / "failure.json"
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            if not failure_path.exists():
                write_json(failure_path, {"schemaVersion": 1, "stage": args.command, "error": str(error)})
        print(f"CI Shepherd failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
