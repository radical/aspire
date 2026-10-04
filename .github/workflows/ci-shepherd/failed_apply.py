"""Witness one source-qualified consumed repair failure, never a new send grant."""

from copy import deepcopy
import io
import zipfile

from github import IncompleteInventory
import issue_pr
import reasoning
import receipts
import recovery
import round as contracts


# Review attests publication-before-send and isolated credential-bearing jobs.
# Protocol-shaped artifacts or a shared branch cannot register another source.
SOURCES = frozenset({"d1cb65eb4fd8b3efe456b6573c7eb04010439e36"})
JOBS = {"prepare": "success", "activation": "success", "agent": "success",
        "submit_decision": "failure", "conclusion": "success"}


def collect(api, run, policy):
    import live

    if type(policy) is not recovery.PinnedRecovery:
        raise IncompleteInventory("failed apply requires independent typed prepared recovery")
    for value in (run["id"], run["workflow_id"], run["actor"]["id"],
                  run["repository"]["id"], run["head_repository"]["id"]):
        issue_pr.positive(value, "failed apply remote identity")
    if (run["head_sha"] not in SOURCES or run["path"] != live.WORKFLOW or run["workflow_id"] != live.WORKFLOW_ID
            or run["event"] != "workflow_dispatch" or run["actor"]["id"] != recovery.ACTOR["id"]
            or run["actor"]["login"] != recovery.ACTOR["login"]
            or run["repository"]["id"] != live.REPOSITORY_ID or run["repository"]["full_name"] != live.REPOSITORY
            or run["head_repository"]["id"] != live.REPOSITORY_ID or run["head_repository"]["full_name"] != live.REPOSITORY
            or run["status"] != "completed" or run["conclusion"] != "failure"
            or type(run["run_attempt"]) is not int or run["run_attempt"] != 1):
        raise IncompleteInventory("failed apply source/run/actor/attempt is not trusted")
    prefix = f"repos/{live.REPOSITORY}/actions"
    jobs = api.pages(prefix + f"/runs/{run['id']}/attempts/1/jobs", key="jobs")
    if len(jobs) != 5 or {job["name"] for job in jobs} != set(JOBS):
        raise IncompleteInventory("failed apply job inventory incomplete or has extra credential jobs")
    for job in jobs:
        issue_pr.positive(job["id"], "failed apply job id")
        issue_pr.positive(job["run_id"], "failed apply job run id")
        if (job["run_id"] != run["id"] or type(job["run_attempt"]) is not int or job["run_attempt"] != 1
                or job["head_sha"] != run["head_sha"] or job["status"] != "completed"
                or job["conclusion"] != JOBS[job["name"]] or not isinstance(job.get("steps"), list)):
            raise IncompleteInventory("failed apply job/source/attempt mismatch")
    writer = next(job for job in jobs if job["name"] == "submit_decision")
    guarded = [step for step in writer["steps"] if step["name"] == "Guarded host apply"]
    if len(guarded) != 1 or guarded[0]["status"] != "completed" or guarded[0]["conclusion"] != "failure":
        raise IncompleteInventory("failed apply lacks the failed guarded writer step")
    artifacts = api.pages(prefix + f"/runs/{run['id']}/artifacts", key="artifacts")
    names = {f"ci-shepherd-{kind}-{run['id']}-1" for kind in ("prepare", "prepare-audit", "evidence", "receipt")}
    if any(item["name"].startswith("ci-shepherd-") and item["name"] not in names for item in artifacts):
        raise IncompleteInventory("failed apply has unknown privileged artifacts")

    def download(kind, members):
        found = [item for item in artifacts if item["name"] == f"ci-shepherd-{kind}-{run['id']}-1"]
        if len(found) != 1 or found[0]["expired"] is not False:
            raise IncompleteInventory("failed apply artifact missing/expired/ambiguous")
        issue_pr.positive(found[0]["id"], "failed apply artifact id")
        provenance = found[0]["workflow_run"]
        for key in ("id", "repository_id", "head_repository_id"):
            issue_pr.positive(provenance[key], "failed apply artifact identity")
        if any(provenance.get(key) != value for key, value in {
            "id": run["id"], "repository_id": live.REPOSITORY_ID, "head_repository_id": live.REPOSITORY_ID,
            "head_sha": run["head_sha"], "head_branch": "main",
        }.items()):
            raise IncompleteInventory("failed apply artifact source/provenance mismatch")
        raw = api.get(prefix + f"/artifacts/{found[0]['id']}/zip")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = [item.filename for item in archive.infolist()]
                if set(entries) != members or len(entries) != len(members):
                    raise IncompleteInventory("failed apply archive has missing/extra/duplicate members")
        except (OSError, TypeError, zipfile.BadZipFile) as error:
            raise IncompleteInventory("failed apply artifact unreadable") from error
        return {name: live.archive_json(raw, name) for name in members}

    expected_run = {"repository": live.REPOSITORY, "runId": str(run["id"]),
                    "runAttempt": "1", "workflowSha": run["head_sha"]}
    prepared = download("prepare", {"packet.json", "envelope.json"})
    packet, envelope = prepared["packet.json"], prepared["envelope.json"]
    contracts.validate_reconciliation_packet(packet, expected_run)
    contracts.exact(envelope, {"schemaVersion", "packet", "sessionId", "mode", "scope", "context", "recovery"},
                    "failed apply prepare envelope")
    contracts.exact(envelope["scope"], {"root", "trial"}, "failed apply prepare scope")
    issue_pr.validate_subject(envelope["scope"]["root"])
    receipts.validate_trial(envelope["scope"]["trial"])
    baseline = recovery.prepared_record()
    if (type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1
            or envelope["packet"] != packet or envelope["sessionId"] is not None or envelope["mode"] != "live"
            or envelope["scope"] != {"root": live.ROOT, "trial": recovery.TRIAL}
            or envelope["recovery"] != recovery.PinnedRecovery(expected_run).descriptor()
            or not isinstance(envelope["context"], dict) or packet["root"] != live.ROOT or packet["subject"] != live.ROOT
            or packet["record"] != {"commentId": recovery.COMMENT_ID, "value": baseline}):
        raise IncompleteInventory("failed apply prepared scope/authority/descriptor mismatch")
    if receipts.read_record(packet["observation"], recovery.ACTOR) != (recovery.COMMENT_ID, baseline):
        raise IncompleteInventory("failed apply prepared record is not authenticated canonical authority")
    prepare_audit = download("prepare-audit", {"audit.json"})["audit.json"]
    contracts.validate_run(prepare_audit["run"])
    issue_pr.validate_subject(prepare_audit["root"])
    receipts.validate_record(prepare_audit["record"], live.ROOT, live.PR_NODE)
    issue_pr.positive(prepare_audit["commentId"], "failed apply prepare comment id")
    if (type(prepare_audit["schemaVersion"]) is not int
            or prepare_audit != {"schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": "live",
                                 "phase": "complete", "attempts": [], "record": baseline, "commentId": recovery.COMMENT_ID}):
        raise IncompleteInventory("failed apply prepare audit is not complete GET-only observation")
    native = download("evidence", {"evidence.json"})["evidence.json"]
    decision, _ = reasoning.validate_evidence(native, native["sessionId"], hosted=True)
    reasoning.validate_reconciliation_evidence(packet, decision, expected_run, native)
    if (decision["action"] != "repair-pr"
            or receipts.operation_identity(packet, decision) != baseline["operations"][0]["identity"]):
        raise IncompleteInventory("failed apply native intent differs from the prepared repair")
    reserved = deepcopy(baseline)
    receipts.reserve(reserved, reserved["operations"][0])
    consumed = deepcopy(reserved)
    consumed["operations"][0]["state"] = "consumed"
    result = download("receipt", {"audit.json", "observation.json", "failure.json"})
    audit = result["audit.json"]
    contracts.exact(audit, {"schemaVersion", "run", "root", "mode", "phase", "attempts", "record", "commentId"},
                    "failed apply audit")
    contracts.validate_run(audit["run"])
    issue_pr.validate_subject(audit["root"])
    issue_pr.positive(audit["commentId"], "failed apply comment id")
    receipts.validate_record(audit["record"], live.ROOT, live.PR_NODE)
    if not isinstance(audit["attempts"], list):
        raise IncompleteInventory("failed apply attempt audit malformed")
    for attempt in audit["attempts"]:
        if not isinstance(attempt, dict):
            raise IncompleteInventory("failed apply attempt audit malformed")
        if attempt["kind"] == "status":
            contracts.exact(attempt, {"kind", "record", "commentId"}, "failed apply status attempt")
            receipts.validate_record(attempt["record"], live.ROOT, live.PR_NODE)
            issue_pr.positive(attempt["commentId"], "failed apply status comment id")
    expected_attempts = [{"kind": "status", "record": reserved, "commentId": recovery.COMMENT_ID},
                         {"kind": "status", "record": consumed, "commentId": recovery.COMMENT_ID},
                         {"kind": "task", "operationId": recovery.OPERATION_ID, "sourceHead": live.INITIAL_HEAD}]
    if (type(audit["schemaVersion"]) is not int
            or audit != {"schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": "live", "phase": "failed",
                         "attempts": expected_attempts, "record": consumed, "commentId": recovery.COMMENT_ID}):
        raise IncompleteInventory("failed apply audit differs from one reserved/consumed send boundary")
    failure = result["failure.json"]
    if (type(failure["schemaVersion"]) is not int
            or failure != {"schemaVersion": 1, "stage": "apply", "error": "task PR artifact mismatch"}
            or result["observation.json"].get("authority") is not False or result["observation.json"].get("effects") != []):
        raise IncompleteInventory("failed apply outcome is not the supported uncertain validation boundary")
    return {"run": expected_run, "record": consumed, "commentId": recovery.COMMENT_ID, "disposition": "effect-possible"}


def check_floor(snapshot, canonical, prior):
    """An executed writer cannot be demoted to no effect or confirmed by a claim."""
    old = prior["operations"][0]
    operation = next((value for value in canonical["operations"] if value["id"] == old["id"]), None)
    if operation is None or operation["state"] not in {"consumed", "uncertain", "confirmed"}:
        raise IncompleteInventory("failed apply consumed capacity moved backwards")
    if operation["state"] == "confirmed" and receipts.reconcile_effect(snapshot, old) != operation["result"]:
        raise IncompleteInventory("failed apply confirmation lacks a unique authenticated current task")
