"""Witness a reviewed source's successful WAIT confirmation, not a send grant."""

from copy import deepcopy
import io
import zipfile

from github import IncompleteInventory
import issue_pr
import reasoning
import receipts
import recovery
import round as contracts


# The registry attests the immutable implementation and credential-job boundary.
# An artifact's protocol claims, branch name or successful conclusion cannot
# register a source. Only the reviewed consumed-to-confirmed WAIT protocol applies.
SOURCES = frozenset({"9ee8070c8d5e1f6bb8a8324ad386e0e17b7fa7d5"})
JOBS = {"prepare", "activation", "agent", "submit_decision", "conclusion"}


def collect(api, run, policy):
    import live

    if type(policy) is not recovery.PinnedRecovery:
        raise IncompleteInventory("successful history requires independent typed prepared recovery")
    for value in (run["id"], run["workflow_id"], run["actor"]["id"],
                  run["repository"]["id"], run["head_repository"]["id"]):
        issue_pr.positive(value, "successful history remote identity")
    if (run["head_sha"] not in SOURCES or run["path"] != live.WORKFLOW or run["workflow_id"] != live.WORKFLOW_ID
            or run["event"] != "workflow_dispatch" or run["actor"]["id"] != recovery.ACTOR["id"]
            or run["actor"]["login"] != recovery.ACTOR["login"]
            or run["repository"]["id"] != live.REPOSITORY_ID or run["repository"]["full_name"] != live.REPOSITORY
            or run["head_repository"]["id"] != live.REPOSITORY_ID or run["head_repository"]["full_name"] != live.REPOSITORY
            or run.get("head_branch") != "main"
            or run["status"] != "completed" or run["conclusion"] != "success"
            or type(run["run_attempt"]) is not int or run["run_attempt"] != 1):
        raise IncompleteInventory("successful history source/run/actor/attempt is not trusted")
    prefix = f"repos/{live.REPOSITORY}/actions"
    jobs = api.pages(prefix + f"/runs/{run['id']}/attempts/1/jobs", key="jobs", require_total_count=True)
    if len(jobs) != 5 or {job["name"] for job in jobs} != JOBS:
        raise IncompleteInventory("successful history job inventory incomplete or has extra credential jobs")
    for job in jobs:
        issue_pr.positive(job["id"], "successful history job id")
        issue_pr.positive(job["run_id"], "successful history job run id")
        if (job["run_id"] != run["id"] or type(job["run_attempt"]) is not int or job["run_attempt"] != 1
                or job["head_sha"] != run["head_sha"] or job["status"] != "completed"
                or job["conclusion"] != "success" or not isinstance(job.get("steps"), list)):
            raise IncompleteInventory("successful history job/source/attempt mismatch")
    writer = next(job for job in jobs if job["name"] == "submit_decision")
    guarded = [step for step in writer["steps"] if step["name"] == "Guarded host apply"]
    if len(guarded) != 1 or guarded[0]["status"] != "completed" or guarded[0]["conclusion"] != "success":
        raise IncompleteInventory("successful history lacks the successful guarded writer step")
    artifacts = api.pages(prefix + f"/runs/{run['id']}/artifacts", key="artifacts", require_total_count=True)
    names = {f"ci-shepherd-{kind}-{run['id']}-1" for kind in ("prepare", "prepare-audit", "evidence", "receipt")}
    if any(item["name"].startswith("ci-shepherd-") and item["name"] not in names for item in artifacts):
        raise IncompleteInventory("successful history has unknown privileged artifacts")

    def download(kind, members):
        found = [item for item in artifacts if item["name"] == f"ci-shepherd-{kind}-{run['id']}-1"]
        if len(found) != 1 or found[0]["expired"] is not False:
            raise IncompleteInventory("successful history artifact missing/expired/ambiguous")
        issue_pr.positive(found[0]["id"], "successful history artifact id")
        provenance = found[0]["workflow_run"]
        for key in ("id", "repository_id", "head_repository_id"):
            issue_pr.positive(provenance[key], "successful history artifact identity")
        if any(provenance.get(key) != value for key, value in {
            "id": run["id"], "repository_id": live.REPOSITORY_ID, "head_repository_id": live.REPOSITORY_ID,
            "head_sha": run["head_sha"], "head_branch": "main",
        }.items()):
            raise IncompleteInventory("successful history artifact source/provenance mismatch")
        raw = api.get(prefix + f"/artifacts/{found[0]['id']}/zip")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = [item.filename for item in archive.infolist()]
                if set(entries) != members or len(entries) != len(members):
                    raise IncompleteInventory("successful history archive has missing/extra/duplicate members")
        except (OSError, TypeError, zipfile.BadZipFile) as error:
            raise IncompleteInventory("successful history artifact unreadable") from error
        return {name: live.archive_json(raw, name) for name in members}

    expected_run = {"repository": live.REPOSITORY, "runId": str(run["id"]),
                    "runAttempt": "1", "workflowSha": run["head_sha"]}
    prepared = download("prepare", {"packet.json", "envelope.json"})
    packet, envelope = prepared["packet.json"], prepared["envelope.json"]
    contracts.validate_reconciliation_packet(packet, expected_run)
    contracts.exact(envelope, {"schemaVersion", "packet", "sessionId", "mode", "scope", "context", "recovery"},
                    "successful history prepare envelope")
    contracts.exact(envelope["scope"], {"root", "trial"}, "successful history prepare scope")
    issue_pr.validate_subject(envelope["scope"]["root"])
    receipts.validate_trial(envelope["scope"]["trial"])
    before = recovery.prepared_record()
    receipts.reserve(before, before["operations"][0])
    before["operations"][0]["state"] = "consumed"
    if (type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1
            or receipts.canonical(envelope["packet"]) != receipts.canonical(packet)
            or envelope["sessionId"] is not None or envelope["mode"] != "live"
            or envelope["scope"] != {"root": live.ROOT, "trial": recovery.TRIAL}
            or envelope["recovery"] != recovery.PinnedRecovery(expected_run).descriptor()
            or not isinstance(envelope["context"], dict) or packet["root"] != live.ROOT or packet["subject"] != live.ROOT
            or packet["record"] != {"commentId": recovery.COMMENT_ID, "value": before}
            or receipts.read_record(packet["observation"], recovery.ACTOR) != (recovery.COMMENT_ID, before)):
        raise IncompleteInventory("successful history prepare authority/scope/descriptor mismatch")
    prepare_audit = download("prepare-audit", {"audit.json"})["audit.json"]
    receipts.validate_record(prepare_audit["record"], live.ROOT, live.PR_NODE)
    issue_pr.positive(prepare_audit["commentId"], "successful history prepare comment id")
    if (type(prepare_audit["schemaVersion"]) is not int or receipts.canonical(prepare_audit) != receipts.canonical({
        "schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": "live", "phase": "complete",
        "attempts": [], "record": before, "commentId": recovery.COMMENT_ID,
    })):
        raise IncompleteInventory("successful history prepare audit is not complete GET-only observation")
    native = download("evidence", {"evidence.json"})["evidence.json"]
    decision, _ = reasoning.validate_evidence(native, native["sessionId"], hosted=True)
    reasoning.validate_reconciliation_evidence(packet, decision, expected_run, native)
    if decision["action"] != "wait":
        raise IncompleteInventory("successful history requires a real successful native WAIT")
    result = receipts.reconcile_effect(packet["observation"], before["operations"][0])
    if result is None:
        raise IncompleteInventory("successful history lacks unique prepared task association")
    confirmed = deepcopy(before)
    confirmed["operations"][0].update(state="confirmed", result=result)
    completed = download("receipt", {"audit.json", "receipt.json"})
    audit, receipt = completed["audit.json"], completed["receipt.json"]
    receipts.validate_record(audit["record"], live.ROOT, live.PR_NODE)
    issue_pr.positive(audit["commentId"], "successful history confirmed comment id")
    if (type(audit["schemaVersion"]) is not int or receipts.canonical(audit) != receipts.canonical({
        "schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": "live", "phase": "complete",
        "attempts": [{"kind": "status", "record": confirmed, "commentId": recovery.COMMENT_ID}],
        "record": confirmed, "commentId": recovery.COMMENT_ID,
    })):
        raise IncompleteInventory("successful history differs from one confirmed publication without a send")
    contracts.exact(receipt, {"schemaVersion", "run", "mode", "root", "packetId", "sessionId", "outcome", "effects",
                             "recovered", "operation", "currentHead", "tasks", "gate", "push"}, "successful WAIT receipt")
    context = envelope["context"]
    issue_pr.validate_subject(receipt["root"])
    contracts.validate_run(receipt["run"])
    if (type(receipt["schemaVersion"]) is not int or receipt["schemaVersion"] != 1 or receipt["run"] != expected_run
            or receipt["mode"] != "live" or receipt["root"] != live.ROOT or receipt["packetId"] != packet["packetId"]
            or receipt["sessionId"] != native["sessionId"] or receipt["outcome"] != "confirmed"
            or receipt["effects"] != [] or receipt["recovered"] is not True
            or receipts.canonical(receipt["operation"]) != receipts.canonical(confirmed["operations"][0])
            or receipt["currentHead"] != issue_pr.member(packet["observation"], live.ROOT)["revision"]
            or receipt["currentHead"] != context.get("sourceHead")
            or any(receipt[key] != context.get(key) for key in ("tasks", "gate", "push"))):
        raise IncompleteInventory("successful WAIT receipt/native/prepare/result binding mismatch")
    return {"run": expected_run, "record": confirmed, "commentId": recovery.COMMENT_ID}


def check_floor(snapshot, canonical, prior):
    old = prior["operations"][0]
    current = next((operation for operation in canonical["operations"] if operation["id"] == old["id"]), None)
    if current != old or receipts.reconcile_effect(snapshot, old) != old["result"]:
        raise IncompleteInventory("successful history confirmation lacks its unique authenticated current task")
