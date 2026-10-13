"""Owned terminal-task result collection and separately authorized issue reporting."""

from copy import deepcopy
import argparse
import base64
import binascii
import json
from pathlib import Path
import re
import sys

import pilot_results
import pilot_state as state
import round as contracts
import work_items as items
import work_item_execution as execution
import work_item_receiver as receiver


BEGIN, END = "CSWORKITEMBEGIN", "CSWORKITEMEND"
MAX_LOG = pilot_results.MAX_LOG


def parse_result(text, record, assignment, session):
    if not isinstance(text, str) or len(text.encode()) > MAX_LOG:
        raise ValueError("bounded worker log unavailable")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError("missing or conflicting worker result JSON")
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    if end <= start:
        raise ValueError("reversed worker result envelope")
    # gh renders assistant Markdown at terminal width: raw JSON strings can
    # gain literal newlines. Canonical Base64 tolerates only renderer whitespace.
    # Raw shape: CSWORKITEMBEGINeyJzY2hlbWFfdmVyc2lvbiI6MX0=CSWORKITEMEND
    encoded = re.sub(r"\s+", "", text[start:end])
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > 16000 or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("noncanonical/bounded worker result encoding")
        value = contracts.loads(decoded.decode("utf-8"), max_bytes=16000)
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("invalid worker result encoding") from error
    items.validate_result(value)
    items.validate_result_version(value, assignment)
    if value["worker_id"] == "HOST_VERIFIED_SESSION_ID":
        value["worker_id"] = session
    if (value["item_id"] != record["id"] or value["assignment_id"] != assignment["id"]
            or value["evaluated_revision"] != assignment["revision"] or value["worker_id"] != session):
        raise ValueError("worker result assignment/session mismatch")
    if value["outcome"] == "fixed" and not value["changed_files"]:
        raise ValueError("fixed result requires actual changed files")
    return value


def validate(value, record, assignment):
    contracts.exact(value, {"task_id", "session_id", "attempts", "status", "reason",
                           "task_version", "log_excerpt", "pinned_evidence", "assessment"}, "cloud follow-up")
    for key in ("task_id", "session_id"):
        items.identifier(value[key], key)
        if value[key] != assignment["execution"][key]:
            raise ValueError("follow-up task/session mismatch")
    if type(value["attempts"]) is not int or not 0 < value["attempts"] <= 3:
        raise ValueError("invalid collection attempts")
    if value["status"] not in {"collecting", "unavailable", "checkpointed"}:
        raise ValueError("invalid follow-up state")
    for key in ("reason", "log_excerpt"):
        items.text(value[key], key, 2000)
    items.texts(value["pinned_evidence"], "pinned evidence", 10)
    if value["assessment"] is not None:
        validate_assessment(value["assessment"])
    if not isinstance(value["task_version"], dict):
        raise ValueError("invalid follow-up task version")


def validate_assessment(value):
    contracts.exact(value, {"summary", "evidence", "unknowns"}, "operator assessment")
    items.text(value["summary"], "assessment summary", 1500)
    items.texts(value["evidence"], "assessment evidence", 10)
    items.texts(value["unknowns"], "assessment unknowns", 10)
    return value


def safe(value, token=None, limit=2000):
    return pilot_results.safe_text(redact(str(value), token), limit)


def redact(text, token=None):
    if token:
        text = text.replace(token, "[credential omitted]")
    text = re.sub(r"(?i)\b(?:password|secret|token|authorization|api[_-]?key)"
                  r"\s*[:=]\s*(?:bearer\s+)?\S+", "[credential omitted]", text)
    text = re.sub(r"https?://\S+", "[URL omitted]", text, flags=re.I)
    return re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)",
                  "[credential omitted]", text)


def redact_fields(value, token):
    if isinstance(value, dict):
        return {key: redact_fields(entry, token) for key, entry in value.items()}
    if isinstance(value, list):
        return [redact_fields(entry, token) for entry in value]
    return redact(value, token) if isinstance(value, str) else value


def version(task):
    # Transcript freshness excludes billing; metadata changes still invalidate
    # collection. Persist identities, not the large untrusted prompt/transcript.
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > 10:
        raise ValueError("bounded task artifacts unavailable")
    normalized = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("provider") != "github":
            raise ValueError("unknown task artifact provenance")
        kind, data = artifact.get("type"), artifact.get("data")
        if not isinstance(data, dict):
            raise ValueError("invalid artifact identity")
        if kind == "pull":
            items.issue_pr.positive(data.get("id"), "task pull artifact")
            if data.get("global_id"):
                items.identifier(data["global_id"], "pull artifact node")
            identity = {key: data.get(key) for key in ("id", "global_id")}
        elif kind == "branch":
            for key in ("head_ref", "base_ref"):
                items.ref(data.get(key))
            identity = {key: data[key] for key in ("head_ref", "base_ref")}
        else:
            raise ValueError("unsupported task artifact kind")
        normalized.append({"provider": "github", "type": kind, "data": identity})
    for value in (task, *task["sessions"]):
        for key in ("updated_at", "completed_at"):
            if value.get(key) is not None:
                items.issue_pr.timestamp(value[key])
    return {key: deepcopy(task.get(key)) for key in ("id", "state", "updated_at")} | {
        "artifacts": normalized,
        "sessions": [{key: deepcopy(session.get(key)) for key in (
            "id", "state", "updated_at", "completed_at", "head_ref", "base_ref", "task_id")}
            for session in task["sessions"]]}


def owned(api, read_control):
    control, record = execution.locate(api, read_control)
    if control["action"] == "pause" or not record["assignments"]:
        raise ValueError("active owned-task follow-up required")
    assignment = record["assignments"][-1]
    saved = assignment.get("execution")
    if saved is None or saved["state"] != "sent" or saved["imported_chain_id"] is not None:
        raise ValueError("follow-up requires an exact unimported sent task")
    if (control["issue"] != assignment["basis"]["issue"]
            or control["occurrence"] != assignment["basis"]["occurrence"]):
        raise ValueError("follow-up occurrence changed")
    if hasattr(api.transport, "task_id"):
        api.transport.task_id = saved["task_id"]
    if hasattr(api.transport, "saved_pull"):
        api.transport.saved_pull = saved["pr"]["number"] if saved["pr"] is not None else None
    return control, record, assignment


def stable(api, read_control, control, record, assignment, expected):
    receiver.fresh(api, read_control, control)
    observed = execution.refresh(api, record, assignment)
    if observed["state"] not in state.TERMINAL or version(observed) != expected:
        raise ValueError("task lifecycle/session changed during collection")
    return observed


def pinned(api, occurrence, collector):
    if occurrence is None:
        return ["No pinned workflow occurrence supplied."]
    prefix = f"repos/{occurrence['repository']}/actions"
    run_path = f"{prefix}/runs/{occurrence['run_id']}/attempts/{occurrence['run_attempt']}"
    job_path = f"{prefix}/jobs/{occurrence['job_id']}"
    run, job = api.api.get(run_path), api.api.get(job_path)
    if (run.get("id") != occurrence["run_id"] or job.get("id") != occurrence["job_id"]
            or job.get("run_id") != occurrence["run_id"]
            or any(value.get("run_attempt") != occurrence["run_attempt"] for value in (run, job))
            or any(value.get("head_sha") != occurrence["head_sha"] for value in (run, job))):
        raise ValueError("pinned occurrence identity unavailable")
    evidence = [f"Verified pinned run {occurrence['run_id']} attempt {occurrence['run_attempt']} "
                f"job {occurrence['job_id']} head {occurrence['head_sha']}; "
                f"job conclusion {safe(job.get('conclusion'), limit=80)}."]
    if hasattr(collector, "pinned"):
        try:
            log = collector.pinned(occurrence)
            if api.api.get(run_path) != run or api.api.get(job_path) != job:
                raise ValueError("pinned occurrence changed while reading logs")
            evidence.append("Pinned job log excerpt (untrusted): " + safe(log, api.token, limit=900))
        except (pilot_results.CollectionError, OSError):
            evidence.append("Pinned job logs unavailable; diagnosis and transience unknown.")
    else:
        evidence.append("Pinned job logs unavailable; diagnosis and transience unknown.")
    return evidence


def collect(api, read_control, collector, *, assessment=None):
    control, record, assignment = owned(api, read_control)
    saved = assignment["execution"]
    old = saved.get("followup")
    comment = record["comment"]
    if old is not None and (old["status"] == "checkpointed" or old["attempts"] == 3
                            or comment is not None and comment["delivery"] != "reserved"):
        stable(api, read_control, control, record, assignment, old["task_version"])
        if old["status"] == "collecting" and old["attempts"] == 3:
            old.update(status="unavailable", reason="Collection attempt budget exhausted after interruption.")
            receiver.fresh(api, read_control, control)
            api.persist()
        return {"outcome": old["status"], "reason": old["reason"]}
    observed = execution.refresh(api, record, assignment)
    if observed["state"] not in state.TERMINAL:
        api.persist()
        raise ValueError("terminal task required for collection")
    snapshot = version(observed)
    # An unsent preview can be replaced by a subsequent bounded collection.
    # An escaped notification's exact body is immutable for recovery/dedup.
    if comment is not None:
        record["comment"] = None
    # Persist attempt before external log reads. Crashes consume a bounded
    # collection attempt, never grant another worker send.
    value = {"task_id": saved["task_id"], "session_id": saved["session_id"],
             "attempts": 1 if old is None else old["attempts"] + 1,
             "status": "collecting", "reason": "Collection interrupted or incomplete.",
             "task_version": snapshot, "log_excerpt": "Worker logs unavailable.",
             "pinned_evidence": [], "assessment": None}
    saved["followup"] = value
    receiver.fresh(api, read_control, control)
    api.persist()
    parsed = None
    try:
        value["pinned_evidence"] = pinned(api, control["occurrence"], collector)
        text = collector(api.repository, saved["session_id"])
        if not isinstance(text, str) or len(text.encode()) > MAX_LOG:
            raise ValueError("bounded task log unavailable")
        if hasattr(api, "normal_log_audit"):
            api.normal_log_audit(saved["session_id"], redact(text, api.token))
        # Rendered logs begin with setup/tool activity. Include the end as well
        # so a missing structured envelope does not hide the worker's ending.
        excerpt = text if len(text) <= 1500 else (
            text[:500] + "\n[Middle of session excerpt omitted]\n" + text[-1000:])
        value["log_excerpt"] = safe(excerpt, api.token) or "Empty exact-session transcript."
        parsed = parse_result(text, record, assignment, saved["session_id"])
        parsed = redact_fields(parsed, api.token)
        items.validate_result(parsed)
        value["reason"] = "Exact session final JSON schema and assignment correlation validated."
    except (pilot_results.CollectionError, OSError) as error:
        value["reason"] = "Task logs unavailable: " + (
            error.category if isinstance(error, pilot_results.CollectionError) else "local transport")
    except ValueError as error:
        value["reason"] = safe(error, api.token, 900)
    try:
        stable(api, read_control, control, record, assignment, snapshot)
        if assessment is not None:
            value["assessment"] = redact_fields(deepcopy(validate_assessment(assessment)), api.token)
        if parsed is not None:
            items.checkpoint(record, parsed, saved["session_id"], None)
            value["status"] = "checkpointed"
        else:
            value["status"] = "unavailable"
    except ValueError as error:
        parsed = None
        value.update(status="unavailable", reason=safe(error, api.token, 900), assessment=None)
    # A stale source/pause/hands-off must not persist newly accepted claims.
    receiver.fresh(api, read_control, control)
    api.persist()
    return {"outcome": value["status"], "reason": value["reason"]}


def comment_body(control, assignment):
    value = assignment["execution"]["followup"]
    result = assignment["result"]
    lines = [
        "[automated] CI Shepherd work-item result; human direction required.",
        f"<!-- ci-shepherd:work-item:{control['id']}:{assignment['id']} -->",
        f"Owned task `{value['task_id']}`; verified session `{value['session_id']}`.",
        f"Evaluated revision {assignment['revision']}; current control revision {control['revision']}.",
        "Host observation: completed task does not establish a fix. No independently verified "
        "fix is available; this assessment is inconclusive. Empty/closed drafts are not fixes.",
    ]
    if result is None:
        lines.append("Worker final JSON unavailable: " + value["reason"])
    else:
        lines.extend([
            f"Worker-reported classification: {safe(result['actual_classification'])}; "
            f"transience: {safe(result['transience'])}; outcome: {safe(result['outcome'])}.",
            "Worker claim: " + safe(result["summary"]),
            "Worker claims are not independently verified diagnosis, tests or issue resolution.",
        ])
        lines.extend("Worker evidence: " + safe(entry) for entry in result["evidence"])
        lines.extend("Worker test: " + safe(test["command"]) + " - " + test["result"]
                     for test in result["tests"])
        if "recommended_route" in result:
            lines.append("Worker-proposed next route (not automatic dispatch): " + result["recommended_route"])
        if "investigation" in result:
            import work_item_investigation
            investigation = result["investigation"]
            lines.append("Worker-reported comparable-run status: " + work_item_investigation.status(investigation)
                         + "; not independent recovery/root-cause proof.")
            lines.append("Worker comparison: " + (
                safe(json.dumps(investigation["compare"], sort_keys=True))
                if investigation["compare"] is not None else "unavailable; candidate causes unknown."))
        for action in result.get("proposed_actions", []):
            allowed_target = action["target"] == control["issue"] if action["kind"] == "comment_issue" else (
                action["target"]["repository"] == control["issue"]["repository"])
            lines.append("Worker action proposal (no grant): " + action["kind"] + "; "
                         + ("target eligible for separate policy/dedup checks." if allowed_target
                            else "denied: target outside owned item/repository."))
            lines.append("Proposed title: " + safe(action["title"], limit=200))
            lines.append("Proposed body: " + safe(action["body"], limit=1500))
            lines.append("Justification (worker-reported): " + safe(action["justification"]))
    lines.extend(value["pinned_evidence"])
    lines.append("Exact session log excerpt (untrusted worker/tool text): " + value["log_excerpt"])
    if value["assessment"] is not None:
        assessment = value["assessment"]
        lines.append("Operator-attributed assessment, not worker JSON: " + safe(assessment["summary"]))
        lines.extend("Operator-attested evidence: " + safe(entry) for entry in assessment["evidence"])
        lines.extend("Unknown: " + safe(entry) for entry in assessment["unknowns"])
    lines.append("Unknown: root cause and transience are not independently established by task "
                 "completion or a green run at another SHA.")
    tracker = assignment["execution"].get("tracker_followup")
    if tracker is not None and tracker["linked_issue"] is not None:
        linked = tracker["linked_issue"]
        lines.append(f"Separately verified test tracker: https://github.com/{linked['repository']}"
                     f"/issues/{linked['number']}; state {linked['state']}. "
                     "Original occurrence ownership is unchanged; no automatic reopen.")
    lines.append("@radical, should we investigate the missing evidence, link a matching test tracker, "
                 "or stop work on this occurrence? The original issue stays open; no replacement "
                 "task, PR import or Agent Merge is authorized by this report.")
    if len("\n\n".join(lines).encode()) > 14000:
        kept, omitted = [], 0
        for line in lines[:-1]:
            if len("\n\n".join([*kept, line]).encode()) <= 13000:
                kept.append(line)
            else:
                omitted += 1
        lines = [*kept, f"{omitted} bounded excerpts omitted from this notification; "
                 "the full redacted claims/assessment remain in canonical authority.", lines[-1]]
    body = "\n\n".join(lines)
    if len(body.encode()) > 14000:
        raise ValueError("follow-up report exceeds bounded comment size")
    return body


def normal_comment_body(control, assignment, *, context=None):
    value = assignment["execution"]["followup"]
    lines = [
        "[automated] CI Shepherd work-item result; human direction required.",
        f"<!-- ci-shepherd:work-item:{control['id']}:{assignment['id']} -->",
        f"Owned task `{value['task_id']}`; verified session `{value['session_id']}`.",
        f"Platform task state: {assignment['execution']['task_state']}. "
        "No usable PR was imported; task completion and empty drafts do not establish a fix.",
    ]
    if context is not None:
        mapping = context["mapping"]
        observed = mapping.get("observation")
        if observed is not None:
            lines.append(f"Observed PR #{observed['number']}: state {observed['state']}; "
                         f"draft {str(observed['draft']).lower()}; merged {str(observed['merged']).lower()}; "
                         f"changed files {observed['changedFiles']}.")
            if observed["changedFiles"] > 0:
                lines.append("Substantive PR observation requires human reconciliation; not import authorization.")
        else:
            lines.append("No PR artifact observed." if mapping["reason"] == "No platform PR artifact."
                         else "PR observation unavailable.")
        if context.get("sourceChangedSinceDispatch"):
            lines.append("The issue changed since this assignment was dispatched. Session claims describe "
                         "the original assignment, not validation of the current issue.")
    assessment = value["assessment"]
    details = ["Result collection: " + safe(value["reason"], limit=400)]
    if assessment is None:
        lines.append("Assessment outcome unrecorded/unknown; no replacement inference was started.")
        lines.append("Unknown: root cause and transience are not independently established.")
    result = assignment["result"]
    if result is not None:
        details.append(f"Worker-reported outcome (original assignment; untrusted): {result['outcome']}. "
                       + safe(result["summary"], limit=350))
    if assessment is not None:
        details.append("Shepherd assessment (not independent fix validation): "
                       + safe(assessment["summary"], limit=800))
        details.extend("Evidence: " + safe(entry, limit=250) for entry in assessment["evidence"])
        details.extend("Unknown: " + safe(entry, limit=250) for entry in assessment["unknowns"])
    details.extend(safe(entry, limit=250) for entry in value["pinned_evidence"])
    details.append("Session excerpt (untrusted): " + safe(value["log_excerpt"], limit=500))
    footer = "The source issue stays open. No merge or issue resolution is implied."
    omitted = 0
    for detail in details:
        # Authority stores the body as JSON, where escapes can expand Unicode.
        # Keep report capacity bounded in the same representation as admission.
        candidate = "\n\n".join([*lines, detail, "Some descriptive excerpts omitted.", footer])
        if len(json.dumps(candidate).encode()) <= 4000:
            lines.append(detail)
        else:
            omitted += 1
    if omitted:
        lines.append("Some descriptive excerpts omitted.")
    return "\n\n".join([*lines, footer])


def report(api, read_control, *, publish=False, compact=False, render_body=None, freshness=None):
    control, record, assignment = owned(api, read_control)
    value = assignment["execution"].get("followup")
    if value is None or value["status"] == "collecting":
        raise ValueError("complete attributed collection required for follow-up")
    stable(api, read_control, control, record, assignment, value["task_version"])
    def before_send():
        observed = stable(api, read_control, control, record, assignment, value["task_version"])
        if freshness is not None:
            freshness(observed)

    return receiver.report(
        api, read_control, control, record,
        render_body=render_body or (normal_comment_body if compact else comment_body), publish=publish,
        before_send=before_send)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["collect", "preview", "report"])
    parser.add_argument("--item", type=Path, required=True)
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--assessment", type=Path, help="Trusted operator assessment, never worker JSON")
    parser.add_argument("--read-only", action="store_true",
                        help="Local simulated receipts and preview only; no GitHub or authority writes")
    args = parser.parse_args(argv)
    if args.assessment is not None and args.mode != "collect":
        parser.error("--assessment belongs only to collect")
    if args.read_only and args.mode == "report":
        parser.error("--read-only cannot publish reports")
    try:
        import local
        from result_collector import LocalCollector
        from work_item_github import FollowupGitHub, ReadOnlyFollowupGitHub
        read_control = lambda: contracts.read_json(args.item)
        control = items.validate_control(read_control())
        token = local.selected_token()
        revision = local.command(["git", "--no-pager", "-C", str(local.ROOT), "rev-parse", "HEAD"])
        local.require_source(revision)
        local.require_idle_actions(token)
        with local.authority_lock(Path.home() / ".copilot" / "ci-shepherd" / "locks", args.authority):
            args.workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
            cls = ReadOnlyFollowupGitHub if args.read_only else FollowupGitHub
            api = cls(token, args.tracker, args.authority, args.tracker_node,
                      revision=revision, control=control)
            if args.mode == "collect":
                receipt = collect(api, read_control, LocalCollector(token),
                                  assessment=contracts.read_json(args.assessment) if args.assessment else None)
                if args.read_only:
                    receipt["read_only"] = True
                    receipt["comment_preview"] = report(api, read_control)["comment_preview"]
            else:
                receipt = report(api, read_control, publish=args.mode == "report")
            if args.read_only:
                contracts.write_json(args.workdir / "authority-preview.json", api.ledger)
            contracts.write_json(args.workdir / "receipt.json", receipt)
            print(json.dumps(receipt, allow_nan=False))
            return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"CI Shepherd owned follow-up failed closed: {safe(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
