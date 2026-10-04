"""Authenticated recognition of completed GET-only prepare / unexecuted writer."""

import io
import zipfile

import issue_pr
import receipts
import recovery
import round as contracts
from github import IncompleteInventory


# Source registration attests reviewed GET-only prepare and the sole gated writer,
# not arbitrary historical code or an artifact's own claim of read-only behavior.
# The current immutable source is separately authenticated by the hosted caller.
READ_ONLY_PREPARE_SOURCES = frozenset({"f16d43ba3de8ac7d299301d763d49236f1954843"})
JOB_CONCLUSIONS = {"prepare": "success", "activation": "success", "agent": "failure",
                   "submit_decision": "skipped", "conclusion": "success"}


def collect(api, run, current_run):
    """Return neutral witnessed prepare records, never native decisions/receipts."""
    import live

    contracts.validate_run(current_run)
    if (run["head_sha"] not in READ_ONLY_PREPARE_SOURCES | {current_run["workflowSha"]}
            or run["path"] != live.WORKFLOW or run["workflow_id"] != live.WORKFLOW_ID
            or run["event"] != "workflow_dispatch" or run["actor"]["id"] != recovery.ACTOR["id"]
            or run["actor"]["login"] != recovery.ACTOR["login"]
            or run["repository"]["id"] != live.REPOSITORY_ID
            or run["repository"]["full_name"] != live.REPOSITORY
            or run["head_repository"]["id"] != live.REPOSITORY_ID
            or run["head_repository"]["full_name"] != live.REPOSITORY
            or run["status"] != "completed" or run["conclusion"] != "failure"):
        raise IncompleteInventory("pre-apply abort source/run/actor is not trusted")
    if type(run["run_attempt"]) is not int or run["run_attempt"] != 1:
        raise IncompleteInventory("pre-apply abort recognition requires attempt 1; reruns blocked")
    prefix = f"repos/{live.REPOSITORY}/actions"
    artifacts = api.pages(prefix + f"/runs/{run['id']}/artifacts", key="artifacts")
    names = {f"ci-shepherd-{kind}-{run['id']}-1" for kind in ("prepare", "prepare-audit", "evidence")}
    if any(item["name"].startswith("ci-shepherd-") and item["name"] not in names for item in artifacts):
        raise IncompleteInventory("pre-apply abort has writer receipt or unknown privileged artifact")

    def download(kind, members):
        found = [item for item in artifacts if item["name"] == f"ci-shepherd-{kind}-{run['id']}-1"]
        if len(found) != 1 or found[0]["expired"] is not False:
            raise IncompleteInventory("pre-apply abort artifact missing/expired/ambiguous")
        provenance = found[0]["workflow_run"]
        if any(provenance.get(key) != value for key, value in {
            "id": run["id"], "repository_id": live.REPOSITORY_ID, "head_repository_id": live.REPOSITORY_ID,
            "head_sha": run["head_sha"], "head_branch": "main",
        }.items()):
            raise IncompleteInventory("pre-apply abort artifact source/provenance mismatch")
        raw = api.get(prefix + f"/artifacts/{found[0]['id']}/zip")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = [item.filename for item in archive.infolist()]
                if set(entries) != members or len(entries) != len(members):
                    raise IncompleteInventory("pre-apply abort artifact has missing/extra/duplicate members")
        except (OSError, TypeError, zipfile.BadZipFile) as error:
            raise IncompleteInventory("pre-apply abort artifact unreadable") from error
        return {name: live.archive_json(raw, name) for name in members}

    jobs = api.pages(prefix + f"/runs/{run['id']}/attempts/1/jobs", key="jobs")
    if len(jobs) != 5 or {job["name"] for job in jobs} != set(JOB_CONCLUSIONS):
        raise IncompleteInventory("pre-apply abort job inventory incomplete or contains extra credential jobs")
    job_ids = set()
    for job in jobs:
        issue_pr.positive(job["id"], "abort job id")
        if (job["id"] in job_ids or job["run_id"] != run["id"] or job["run_attempt"] != 1
                or type(job["run_attempt"]) is not int or job["head_sha"] != run["head_sha"]
                or job["status"] != "completed" or job["conclusion"] != JOB_CONCLUSIONS[job["name"]]
                or not isinstance(job.get("steps"), list)
                or job["name"] == "submit_decision" and job["steps"] != []):
            raise IncompleteInventory("pre-apply abort job/attempt/source or unexecuted writer proof mismatch")
        job_ids.add(job["id"])
    expected_run = {"repository": live.REPOSITORY, "runId": str(run["id"]),
                    "runAttempt": "1", "workflowSha": run["head_sha"]}
    prepared = download("prepare", {"packet.json", "envelope.json"})
    packet, envelope = prepared["packet.json"], prepared["envelope.json"]
    contracts.validate_reconciliation_packet(packet, expected_run)
    keys = {"schemaVersion", "packet", "sessionId", "mode", "scope", "context"}
    if "recovery" in envelope:
        keys.add("recovery")
        if envelope["recovery"] != recovery.PinnedRecovery(expected_run).descriptor():
            raise IncompleteInventory("pre-apply abort recovery descriptor mismatch")
    contracts.exact(envelope, keys, "aborted host envelope")
    contracts.exact(envelope["scope"], {"root", "trial"}, "aborted host scope")
    record = packet["record"]["value"]
    if (type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1
            or envelope["packet"] != packet or envelope["sessionId"] is not None
            or envelope["mode"] not in {"live", "observe"} or not isinstance(envelope["context"], dict)
            or packet["root"] != live.ROOT
            or packet["subject"] != live.ROOT or envelope["scope"]["root"] != live.ROOT
            or envelope["scope"]["trial"] != (None if record is None else receipts.trial_tuple(record))):
        raise IncompleteInventory("pre-apply abort packet/scope binding mismatch")
    comment_id, observed_record = receipts.read_record(packet["observation"], recovery.ACTOR)
    if packet["record"] != {"commentId": comment_id, "value": observed_record}:
        raise IncompleteInventory("pre-apply abort prepared authority is not canonical")
    if record is not None:
        receipts.validate_record(record, live.ROOT, live.PR_NODE)
    audit = download("prepare-audit", {"audit.json"})["audit.json"]
    contracts.validate_run(audit["run"])
    if audit["record"] is not None:
        receipts.validate_record(audit["record"], live.ROOT, live.PR_NODE)
        issue_pr.positive(audit["commentId"], "aborted audit comment id")
    if (type(audit["schemaVersion"]) is not int
            or audit != {"schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": envelope["mode"],
                         "phase": "complete", "attempts": [], "record": record, "commentId": comment_id}):
        raise IncompleteInventory("pre-apply abort prepare audit incomplete, attempted effect or authority mismatch")
    # A failed native run's output is never a decision. Require its collector
    # failure artifact, not evidence.json or a fabricated successful receipt.
    failure = download("evidence", {"failure.json"})["failure.json"]
    if (type(failure["schemaVersion"]) is not int
            or failure != {"schemaVersion": 1, "stage": "collect", "error": "host engine step did not succeed"}):
        raise IncompleteInventory("pre-apply abort native collection did not fail closed")
    return [{"run": expected_run, "record": record, "commentId": comment_id, "disposition": "pre-apply-abort"}]
