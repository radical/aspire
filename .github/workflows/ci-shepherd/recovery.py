"""Explicit host-only migration for one witnessed, unsent prepared intent."""

from copy import deepcopy
from dataclasses import dataclass
import zipfile
import io

import diagnostics
import issue_pr
import reasoning
import receipts
import round as contracts


OLD_SOURCE = "a9da7a1d90195c255bfd7d91349ff0bdbbb20356"
OBSERVE_RUN = "37175895217"
FAILED_RUN = "37176266114"
COMMENT_ID = 5976480777
OPERATION_ID = "1d681347-69c8-4fe5-ba17-24aa6cd7f238"
PACKET_ID = "90ed6de9-ece6-412e-bc7a-a2f46c5c23b3"
TRIAL = {"trialId": "8ac4f956-3cd7-42b9-bd69-546b220b128f",
         "trialStartedAt": "2026-10-04T04:13:32.988354Z", "expiresAt": "2026-10-05T04:13:32.988354Z"}
ACTOR = {"id": 1472, "login": "radical"}
JOBS = {
    OBSERVE_RUN: (111358307472, 111358357310, 111358399718, 111358682556, 111358753292),
    FAILED_RUN: (111359393542, 111359451202, 111359534609, 111359697457, 111359816249),
}
SESSIONS = {OBSERVE_RUN: "2c20f226-b2bd-4358-911d-92b8ca0ee6e4",
            FAILED_RUN: "846fcc95-05bd-4aab-9f14-a4f210ea0f62"}


def prepared_record():
    import live
    feedback_id = "ci-37170819379-111343238059"
    identity = {"root": live.ROOT, "subject": live.ROOT, "nodeId": live.PR_NODE, "revision": live.INITIAL_HEAD,
                "feedback": [{"id": feedback_id, "revision": live.INITIAL_HEAD + ":1", "state": "open"}],
                "policy": "drive-to-readiness", "action": "repair-pr", "arguments": {"feedbackIds": [feedback_id]}}
    return {"schemaVersion": 1, "root": deepcopy(live.ROOT), "rootNodeId": live.PR_NODE, **TRIAL,
            "repairBatches": 0, "reruns": [], "operations": [
                {"id": OPERATION_ID, "identity": deepcopy(identity), "action": "repair-pr", "state": "prepared",
                 "result": None, "run": {"repository": live.REPOSITORY, "runId": FAILED_RUN,
                                       "runAttempt": "1", "workflowSha": OLD_SOURCE}, "packetId": PACKET_ID},
            ]}


def refused(message):
    return ValueError("pinned recovery: " + message)


@dataclass(frozen=True, init=False)
class PinnedRecovery:
    """Trusted configuration, not a decision field or arbitrary history waiver."""
    _run: tuple

    def __init__(self, run):
        import live
        contracts.validate_run(run)
        if run["repository"] != live.REPOSITORY or run["runId"] in JOBS or run["workflowSha"] == OLD_SOURCE:
            raise refused("fresh current-source run required")
        object.__setattr__(self, "_run", tuple(run[key] for key in ("repository", "runId", "runAttempt", "workflowSha")))

    @property
    def run(self):
        return dict(zip(("repository", "runId", "runAttempt", "workflowSha"), self._run))

    def descriptor(self):
        return {"kind": "pinned-prepared-5976480777", "run": self.run}

    def witness(self, api, run, artifacts):
        import live
        run_id = str(run["id"])
        if run_id not in JOBS:
            raise refused("run is outside the two named prior attempts")
        number, conclusion = (3, "success") if run_id == OBSERVE_RUN else (4, "failure")
        if (run["run_number"] != number or run["run_attempt"] != 1 or run["workflow_id"] != live.WORKFLOW_ID
                or run["head_sha"] != OLD_SOURCE or run["actor"]["id"] != ACTOR["id"] or run["actor"]["login"] != ACTOR["login"]
                or run["status"] != "completed" or run["conclusion"] != conclusion
                or run["head_repository"]["id"] != live.REPOSITORY_ID):
            raise refused("prior run/source/actor identity mismatch")
        prefix = f"repos/{live.REPOSITORY}/actions"
        jobs = api.pages(prefix + f"/runs/{run_id}/attempts/1/jobs", key="jobs")
        if len(jobs) != 5 or {job["name"] for job in jobs} != {"prepare", "activation", "agent", "submit_decision", "conclusion"}:
            raise refused("prior job inventory incomplete")
        for name, job_id in zip(("prepare", "activation", "agent", "submit_decision", "conclusion"), JOBS[run_id]):
            job = next(value for value in jobs if value["name"] == name)
            expected = "failure" if run_id == FAILED_RUN and name == "submit_decision" else "success"
            if (job["id"] != job_id or job["run_id"] != int(run_id) or job["run_attempt"] != 1
                    or job["head_sha"] != OLD_SOURCE or job["status"] != "completed" or job["conclusion"] != expected):
                raise refused("prior job/source/status identity mismatch")

        def download(kind, members):
            name = f"ci-shepherd-{kind}-{run_id}-1"
            found = [item for item in artifacts if item["name"] == name]
            if len(found) != 1 or found[0]["expired"] is not False:
                raise refused("required artifact missing/expired/ambiguous")
            provenance = found[0]["workflow_run"]
            if any(provenance.get(key) != value for key, value in {
                "id": int(run_id), "repository_id": live.REPOSITORY_ID, "head_repository_id": live.REPOSITORY_ID,
                "head_sha": OLD_SOURCE, "head_branch": "main",
            }.items()):
                raise refused("artifact run/source identity mismatch")
            raw = api.get(prefix + f"/artifacts/{found[0]['id']}/zip")
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = [item.filename for item in archive.infolist()]
                    if set(names) != members or len(names) != len(members):
                        raise refused("artifact members incomplete or unexpected; no fabricated receipt")
            except (OSError, zipfile.BadZipFile, TypeError) as error:
                raise refused("artifact unreadable") from error
            return {name: live.archive_json(raw, name) for name in members}

        prepared = download("prepare", {"packet.json", "envelope.json"})
        packet, envelope = prepared["packet.json"], prepared["envelope.json"]
        expected_run = {"repository": live.REPOSITORY, "runId": run_id, "runAttempt": "1", "workflowSha": OLD_SOURCE}
        contracts.validate_reconciliation_packet(packet, expected_run)
        contracts.exact(envelope, {"schemaVersion", "packet", "sessionId", "mode", "scope", "context"}, "prior envelope")
        if (envelope["schemaVersion"] != 1 or envelope["packet"] != packet or envelope["sessionId"] is not None
                or envelope["mode"] != ("live" if run_id == FAILED_RUN else "observe")
                or envelope["scope"] != {"root": live.ROOT, "trial": None}
                or packet["root"] != live.ROOT or packet["record"] != {"commentId": None, "value": None}):
            raise refused("prior host prepare binding mismatch")
        expected_packet = PACKET_ID if run_id == FAILED_RUN else "7872141c-34e3-423a-98fb-a7833ed307da"
        if packet["packetId"] != expected_packet:
            raise refused("prior packet identity mismatch")
        native = download("evidence", {"evidence.json"})["evidence.json"]
        if native["sessionId"] != SESSIONS[run_id]:
            raise refused("native session differs from the independently verified prior run")
        decision, _ = reasoning.validate_evidence(native, native["sessionId"], hosted=True)
        reasoning.validate_reconciliation_evidence(packet, decision, expected_run, native)
        identity = receipts.operation_identity(packet, decision)
        baseline = prepared_record()
        if identity != baseline["operations"][0]["identity"]:
            raise refused("prior native decision differs from the exact prepared intent")
        if run_id == FAILED_RUN:
            result = download("receipt", {"audit.json", "observation.json", "failure.json"})
            if result["failure.json"] != {"schemaVersion": 1, "stage": "apply", "error": "GET unavailable: HTTP 403"}:
                raise refused("failed attempt differs from the witnessed pre-send boundary")
            # The reserved audit candidate was written before a second guard.
            # Only attempts[1] equals the actual authenticated prepared record.
            comment = {"id": COMMENT_ID, "user": ACTOR, "body": receipts.render_record(baseline)}
            diagnostics.diagnose_failed(result["audit.json"], comment, TRIAL, OPERATION_ID, PACKET_ID, expected_run)
            return baseline
        result = download("receipt", {"audit.json", "receipt.json"})
        audit, receipt = result["audit.json"], result["receipt.json"]
        if audit != {"schemaVersion": 1, "run": expected_run, "root": live.ROOT, "mode": "observe", "phase": "complete",
                     "attempts": [], "record": None, "commentId": None}:
            raise refused("observe audit contains an attempt or authority")
        contracts.exact(receipt, {"schemaVersion", "run", "mode", "root", "packetId", "sessionId", "outcome", "effects",
                                 "operation", "currentHead", "tasks", "gate", "push"}, "prior observe receipt")
        if (receipt["schemaVersion"] != 1 or receipt["run"] != expected_run or receipt["mode"] != "observe"
                or receipt["root"] != live.ROOT or receipt["packetId"] != packet["packetId"]
                or receipt["sessionId"] != native["sessionId"] or receipt["outcome"] != "dry-run" or receipt["effects"] != []
                or receipt["operation"] != identity or receipt["currentHead"] != live.INITIAL_HEAD
                or receipt["tasks"] != [] or receipt["push"] is not None):
            raise refused("observe receipt is not the pinned no-effect observation")
        return None

    def check_canonical(self, snapshot, actor):
        comment_id, record = receipts.read_record(snapshot, actor)
        if actor != ACTOR or comment_id != COMMENT_ID or record is None or receipts.trial_tuple(record) != TRIAL:
            raise refused("canonical actor/comment/trial mismatch")
        expected = prepared_record()["operations"][0]
        found = [op for op in record["operations"] if op["id"] == OPERATION_ID]
        if len(found) != 1 or any(found[0][key] != expected[key] for key in ("identity", "action", "run", "packetId")):
            raise refused("canonical intent provenance changed")
        return record

    def check_durable(self, record, prior_records):
        # Only complete current-source audits establish this floor. The failed
        # old run's unpersisted reserved candidate must never spend a repair.
        current = {op["id"]: op for op in record["operations"]}
        for prior in prior_records:
            if record["repairBatches"] < prior["repairBatches"]:
                raise refused("durable repair budget moved backwards")
            for old in prior["operations"]:
                operation = current.get(old["id"])
                if operation is None or any(operation[key] != old[key] for key in ("identity", "action", "run", "packetId")):
                    raise refused("durable operation provenance moved backwards")
                if (old["state"] != "prepared" and operation["state"] == "prepared"
                        or old["state"] in {"confirmed", "failed"} and operation != old):
                    raise refused("durable outcome moved backwards")

    def authorize(self, github, packet, snapshot):
        record = self.check_canonical(snapshot, github.actor)
        if next(op for op in record["operations"] if op["id"] == OPERATION_ID)["state"] != "prepared":
            return None
        authorization = PreparedResume(self, packet["packetId"])
        authorization.check(github, packet, snapshot, record)
        return authorization


@dataclass(frozen=True)
class PreparedResume:
    policy: PinnedRecovery
    packet_id: str

    def check(self, github, packet, snapshot, record):
        import live
        if (type(self.policy) is not PinnedRecovery or github.recovery != self.policy or packet["run"] != self.policy.run
                or github.run != self.policy.run or packet["packetId"] != self.packet_id
                or github.history.recovery_record != prepared_record() or not github.history.resume_allowed):
            raise refused("fresh independent host witness required; prior spending cannot be refunded")
        canonical = self.policy.check_canonical(snapshot, github.actor)
        expected = prepared_record()
        if packet["record"] != {"commentId": COMMENT_ID, "value": expected}:
            raise refused("fresh packet must bind the exact persisted prepared record")
        if receipts.operation_identity(packet, {"subject": packet["subject"], "action": "repair-pr",
                                               "arguments": expected["operations"][0]["identity"]["arguments"]}) != expected["operations"][0]["identity"]:
            raise refused("current head/feedback differs from the prepared intent")
        # Every pre-send guard sees the complete newly read task inventory.
        # Even terminal evidence of an outcome forbids retrying this intent.
        own = next(value for value in record["operations"] if value["id"] == OPERATION_ID)
        if own["state"] in {"prepared", "reserved", "consumed"} and any(
            value["operationId"] == OPERATION_ID for value in snapshot["workers"]
        ):
            raise refused("correlated task outcome holds capacity; never retry")
        if own["state"] == "prepared" and canonical != expected:
            raise refused("canonical current record is not the witnessed prepared record")
        if canonical["root"] != live.ROOT:
            raise refused("fixed root changed")
