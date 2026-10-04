"""Closed no-effect prepare, reason, and apply transport."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid


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
