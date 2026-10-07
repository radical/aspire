"""Bounded worker claims, durable settlement and controller-owned reports."""

import base64
import binascii
import hashlib
import json
import re
import html
from copy import deepcopy

import issue_pr
import pilot_state as state
import round as contracts

BEGIN, END = "CSRESULTBEGIN", "CSRESULTEND"
MAX_RESULT = 12000
MAX_LOG = 1000000
REPORT_MARKER = "<!-- ci-shepherd:worker-report:v1 -->"
RESULT_RESERVE = 4000


def reserved_capacity(ledger):
    return sum(max(0, RESULT_RESERVE - len(json.dumps(operation.get("result", {})).encode()))
               for chain in ledger["chains"] for operation in chain["operations"]
               if operation["lane"] == "cloud" and "attemptEvidence" in operation
               and (operation["taskId"] is not None or operation["state"] in {"reserved", "sent", "waiting", "uncertain"}))


class CollectionError(ValueError):
    def __init__(self, category):
        if category not in {"transient", "authentication", "identity", "unsupported", "bounded", "transport"}:
            raise ValueError("invalid collector category")
        self.category = category
        super().__init__("result collection " + category)


def safe_text(value, limit=600):
    value = re.sub(r"https?://\S+", "[URL omitted]", str(value), flags=re.IGNORECASE)
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[credential omitted]", value)
    value = html.escape(" ".join(value.split()), quote=False).replace("@", "@\u200b")
    for char in "\\`*_[]#":
        value = value.replace(char, "\\" + char)
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def compact(record):
    # Authority JSON escapes Unicode and backslashes. Enforce the serialized
    # byte budget, not just the lengths of human-readable strings.
    while len(json.dumps(record, separators=(",", ":")).encode()) > 3000:
        for key in ("details", "lastAction", "platformError", "summary", "reason"):
            value = record.get(key)
            if value is not None and len(value.encode()) > 80:
                record[key] = value.encode()[:len(value.encode()) // 2].decode("utf-8", errors="ignore")
                break
        else:
            host = record.get("hostRun", {})
            if any(host.get(key) and len(host[key].encode()) > 80 for key in ("lastAction", "platformError")):
                for key in ("lastAction", "platformError"):
                    if host.get(key) and len(host[key].encode()) > 80:
                        host[key] = host[key].encode()[:len(host[key].encode()) // 2].decode("utf-8", errors="ignore")
                        break
                continue
            reported = record.get("workerReport", {})
            for key in ("feedback", "evidence"):
                if reported.get(key):
                    reported[key].pop()
                    break
            else:
                raise ValueError("result settlement identity bound exhausted")
    return record


def validate_record(record):
    if record == {"status": "incomplete"}:
        return
    optional = {"auditStatus", "auditAttempts", "claimRevision", "workerReport", "hostRun"}
    contracts.exact(record, {"version", "status", "attempts", "session", "summary", "reason",
                             "publication", "commentId", "platformError", "platformState", "artifactState",
                             "observedHead", "lastAction", "details"} | (record.keys() & optional),
                    "worker result settlement")
    if any(key in record for key in ("auditStatus", "auditAttempts", "claimRevision")):
        if not all(key in record for key in ("auditStatus", "auditAttempts", "claimRevision")):
            raise ValueError("incomplete claim audit durability")
        if (not isinstance(record["auditStatus"], str) or record["auditStatus"] not in {"pending", "durable", "unavailable"}
                or type(record.get("auditAttempts")) is not int or not 0 <= record["auditAttempts"] <= 3
                or not isinstance(record.get("claimRevision"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["claimRevision"])):
            raise ValueError("invalid claim audit durability")
    if "workerReport" in record:
        reported = record["workerReport"]
        contracts.exact(reported, {"waitUntil", "evidence", "feedback", "evidenceCount", "feedbackCount"},
                        "bounded worker-reported evidence")
        if reported["waitUntil"] is not None:
            issue_pr.timestamp(reported["waitUntil"])
        for key in ("evidence", "feedback"):
            if (not isinstance(reported[key], list) or len(reported[key]) > 2
                    or type(reported[key + "Count"]) is not int
                    or not len(reported[key]) <= reported[key + "Count"] <= 30):
                raise ValueError("invalid bounded worker report count")
        for item in reported["evidence"]:
            issue_pr.text(item, "reported evidence", 120)
        for item in reported["feedback"]:
            contracts.exact(item, {"id", "disposition", "reason"}, "reported feedback mapping")
            issue_pr.text(item["id"], "reported feedback ID", 256)
            issue_pr.text(item["reason"], "reported feedback reason", 120)
            if not isinstance(item["disposition"], str) or item["disposition"] not in {
                    "addressed", "declined", "unresolved", "wait-or-rerun"}:
                raise ValueError("invalid reported disposition")
    if "hostRun" in record:
        host = record["hostRun"]
        contracts.exact(host, {"runId", "runAttempt", "conclusion", "lastAction", "platformError"}, "host run evidence")
        issue_pr.positive(host["runId"], "host run")
        issue_pr.positive(host["runAttempt"], "host run attempt")
        if not isinstance(host["conclusion"], str) or host["conclusion"] not in {
                "success", "failure", "cancelled", "timed_out", "neutral", "skipped"}:
            raise ValueError("invalid host run conclusion")
        for key in ("lastAction", "platformError"):
            if host[key] is not None:
                issue_pr.text(host[key], "host event", 250)
    if len(json.dumps(record, separators=(",", ":")).encode()) > 3000:
        raise ValueError("serialized result settlement bound")
    if not isinstance(record["version"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["version"]):
        raise ValueError("invalid result version")
    if record["status"] not in {"pending", "incomplete", "untrusted-task-log"}:
        raise ValueError("invalid result acquisition")
    if type(record["attempts"]) is not int or not 0 <= record["attempts"] <= 3:
        raise ValueError("invalid collection attempts")
    if record["session"] is not None:
        issue_pr.text(record["session"], "result session")
    for key in ("summary", "reason"):
        issue_pr.text(record[key], key, 600)
        if len(record[key].encode()) > 600:
            raise ValueError("result text byte bound")
    for key in ("platformError", "lastAction", "details"):
        if record[key] is not None:
            issue_pr.text(record[key], key, 600 if key == "details" else 500)
            if len(record[key].encode()) > (600 if key == "details" else 500):
                raise ValueError("result fallback byte bound")
    if record["artifactState"] not in {"matched", "reported", "not-reported"}:
        raise ValueError("invalid result artifact evidence")
    if record["platformState"] not in state.TERMINAL:
        raise ValueError("result requires terminal platform snapshot")
    if record["observedHead"] is not None and (
            not isinstance(record["observedHead"], str) or not re.fullmatch(r"[0-9a-f]{40}", record["observedHead"])):
        raise ValueError("invalid independently observed head")
    if record["publication"] not in {"preview", "pending", "sent", "uncertain"}:
        raise ValueError("invalid result publication")
    if record["commentId"] is not None:
        issue_pr.positive(record["commentId"], "result comment")
    if (record["publication"] == "sent") != (record["commentId"] is not None):
        raise ValueError("result publication receipt mismatch")


def task_version(task):
    # Billing is deliberately excluded: a usage update is not a new transcript.
    metadata = deepcopy(task)
    for session in metadata["sessions"]:
        session.pop("usage", None)
    return hashlib.sha256(json.dumps(
        metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def settle(api, chain, operation, task):
    """Persist collection intent before read; crashes consume an attempt, not a send."""
    version = task_version(task)
    previous = operation.get("result")
    if previous == {"status": "incomplete"}:
        return
    recovery = previous is not None and previous.get("auditStatus") == "pending"
    if recovery and (previous["version"] != version or previous["publication"] != "preview"
                     or previous["auditAttempts"] >= 3 or getattr(api, "result_audit", None) is None):
        return
    old_reports = deepcopy(operation.get("resultReports"))
    if previous is not None and previous["version"] != version:
        if previous["publication"] in {"pending", "uncertain"}:
            # Never discard an uncertain publication's immutable reconciliation
            # body merely because the task was resumed or its metadata changed.
            return
        if previous["commentId"] is not None:
            reports = operation.setdefault("resultReports", [])
            receipt = {"version": previous["version"], "commentId": previous["commentId"]}
            if receipt not in reports and len(reports) >= 3:
                return
            if receipt not in reports:
                reports.append(receipt)
    if (not recovery and previous is not None and previous["version"] == version and previous["status"] != "pending"
            and (previous["attempts"] or previous["session"] is None or api.result_collector is None)):
        return
    record = deepcopy(previous) if previous is not None and previous["version"] == version else {
        "version": version, "status": "pending", "attempts": 0,
        "session": task["sessions"][0]["id"] if len(task["sessions"]) == 1 else None,
        "summary": "Worker explanation unavailable; no repair or readiness conclusion.",
        "reason": "Result acquisition pending; platform lifecycle and billing are separate.",
        "publication": "preview", "commentId": None, "platformError": None, "platformState": task["state"],
        "artifactState": api.worker_results[operation["id"]]["artifactState"],
        "observedHead": api.worker_result_heads.get(operation["id"]), "lastAction": None, "details": None}
    if previous is not None and previous["version"] != version and previous["session"] == record["session"]:
        record["attempts"] = previous["attempts"]
        if previous["status"] == "incomplete" and previous["attempts"]:
            record.update(status="incomplete", reason=previous["reason"], summary=previous["summary"])
    errors = api.worker_results[operation["id"]]["errors"]
    record["platformError"] = safe_text("; ".join(error["message"] or "Unspecified platform error"
                                                for error in errors), 500) if errors else None
    operation["result"] = compact(record)
    try:
        state.render(api.ledger)
    except ValueError:
        # Old ledgers did not reserve settlement room. The saved attempt itself
        # still holds matching work; do not prevent independent billing writes.
        operation.pop("result")
        if previous is not None:
            operation["result"] = previous
        if old_reports is None:
            operation.pop("resultReports", None)
        else:
            operation["resultReports"] = old_reports
        if previous is None:
            operation["result"] = {"status": "incomplete"}
            try:
                state.render(api.ledger)
            except ValueError:
                operation.pop("result")
                print("CI Shepherd authority has no room for a result receipt; saved attempt still holds matching work.")
        return
    if len(task["sessions"]) != 1:
        record.update(status="incomplete", reason="Multiple sessions: v1 cannot prove one final session.")
        return
    collector = getattr(api, "result_collector", None)
    if collector is None or not api.write:
        record.update(status="incomplete", reason="No approved result collector available.")
        return
    if not recovery and record["status"] == "incomplete" and record["attempts"]:
        return
    if not recovery and record["attempts"] >= 3:
        record.update(status="incomplete", reason="Same-session collection attempt limit reached.")
        return
    # Legacy operations may fit a pending receipt but not the acquired claim,
    # audit intent and host evidence. Do not read data that cannot be settled.
    if (len(state.render(api.ledger).encode())
            + 3000 - len(json.dumps(record, separators=(",", ":")).encode()) > state.MAX_BODY):
        record.update(status="incomplete", reason="Full result settlement capacity unavailable; human verification required.")
        return
    if recovery:
        record["auditAttempts"] += 1
    else:
        record["attempts"] += 1
    api.persist()
    try:
        text = collector(api.repository, record["session"])
    except CollectionError as error:
        record["reason"] = str(error)
        record["status"] = "pending" if error.category == "transient" and record["attempts"] < 3 else "incomplete"
        if recovery and error.category != "transient":
            record["auditAttempts"] = 3
        compact(record)
        fallback(api, chain, operation, task, collector)
        return
    except (OSError, ValueError):
        record.update(status="incomplete", reason="Collector failed; explanation unavailable.")
        if recovery:
            record["auditAttempts"] = 3
        compact(record)
        fallback(api, chain, operation, task, collector)
        return
    try:
        fresh, _ = api.task_detail(operation["taskId"], chain, operation)
        if task_version(fresh) != version or fresh["state"] not in state.TERMINAL:
            raise ValueError("task changed during collection")
    except (ValueError, KeyError, TypeError, AttributeError):
        record.update(status="incomplete", reason="Task/session freshness changed or unavailable during collection.")
        operation.update(workerState="unknown", state="waiting")
        if not operation["workerReserved"]:
            operation["workerReserved"] = state.new_worker_reservation(api.ledger, chain, api.clock())
        api.worker_results.pop(operation["id"], None)
        compact(record)
        return
    if isinstance(text, str) and len(text.encode()) <= MAX_LOG:
        actions = re.findall(r"^Bash: ([^\n]+)", text, re.MULTILINE)
        if actions:
            record["lastAction"] = safe_text(actions[-1], 500)
    try:
        value = parse_claim(text, correlation(api.repository, chain, operation), basis(operation)["feedback"])
    except ValueError as error:
        record.update(status="incomplete", reason=safe_text(error))
        if recovery:
            record["auditAttempts"] = 3
        # No envelope is legacy narrative only; it never resolves feedback.
        if isinstance(text, str) and len(text.encode()) <= MAX_LOG and BEGIN not in text and END not in text:
            record["summary"] = "Rendered log excerpt (untrusted): " + safe_text(text, 500) if text.strip() else record["summary"]
        compact(record)
        fallback(api, chain, operation, task, collector)
        return
    claim = value["claim"]
    revision = hashlib.sha256(json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if recovery and record["claimRevision"] != revision:
        record.update(status="incomplete", reason="Claim changed during audit recovery; human verification required.",
                      auditAttempts=3)
        compact(record)
        return
    counts = {disposition: sum(item["disposition"] == disposition for item in claim["feedback"].values())
              for disposition in ("addressed", "declined", "unresolved", "wait-or-rerun")}
    record.update(status="untrusted-task-log", summary=safe_text(claim["summary"]),
                  reason="Worker-reported: " + safe_text(claim["why"], 500),
                  details=safe_text(
                      f"Outcome: {claim['outcome']}. Files: {', '.join(claim['changes'][:2]) or 'none reported'}. "
                      f"Tests: {'; '.join(claim['tests'][:2]) or 'none reported'}. "
                      f"Feedback: {json.dumps(counts, separators=(',', ':'))}. "
                      + "; ".join(item["reason"] for item in list(claim["feedback"].values())[:2])))
    record["workerReport"] = {
        "waitUntil": claim["waitUntil"], "evidenceCount": len(claim["evidence"]),
        "feedbackCount": len(claim["feedback"]),
        "evidence": [safe_text(item, 120) for item in claim["evidence"][:2]],
        "feedback": [{"id": safe_text(identity, 256), "disposition": item["disposition"],
                      "reason": safe_text(item["reason"], 120)}
                     for identity, item in list(claim["feedback"].items())[:2]]}
    compact(record)
    audit = getattr(api, "result_audit", None)
    record.update(auditStatus="pending" if audit is not None else "unavailable",
                  auditAttempts=record.get("auditAttempts", 0), claimRevision=revision)
    compact(record)
    # Save recovery intent before the independent local write. A crash or disk
    # error can re-read only this bound session, never create another worker.
    api.persist()
    if audit is not None:
        try:
            audit(operation["id"], version, value)
        except OSError:
            record["reason"] = "Worker claim retrieved; local audit write failed. Claims remain untrusted."
        else:
            record["auditStatus"] = "durable"
    compact(record)


def fallback(api, chain, operation, task, collector):
    read = getattr(collector, "fallback", None)
    record = operation["result"]
    try:
        evidence = read(api.repository, task, basis(operation), api.repository_id) if read is not None else None
        # Even unavailable retrieval can take a minute while the worker resumes;
        # absence of host evidence must not retain a stale terminal worker slot.
        fresh, _ = api.task_detail(operation["taskId"], chain, operation)
        if task_version(fresh) != record["version"] or fresh["state"] not in state.TERMINAL:
            raise ValueError("task changed during fallback")
        if evidence is None:
            return
        record["hostRun"] = evidence
        compact(record)
        validate_record(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        record.pop("hostRun", None)
        operation.update(workerState="unknown", state="waiting")
        if not operation["workerReserved"]:
            operation["workerReserved"] = state.new_worker_reservation(api.ledger, chain, api.clock())
        api.worker_results.pop(operation["id"], None)
        record["reason"] = "Fallback identity/freshness unavailable; human verification required."
        compact(record)


def fallback_report(repository, record):
    host = record.get("hostRun")
    if host is None:
        return ("Fallback: verified task/session metadata and reported artifacts were checked. "
                "No independently mapped worker-host run or authenticated final message is available; "
                "last executed action and complete explanation are unknown. "
                "Collection failure does not prove worker failure.\n\n")
    return (
        f"Fallback: independently mapped worker-host Actions run conclusion: {host['conclusion']}. "
        f"Last host-recorded tool event (not final explanation or PR readiness): {host['lastAction'] or 'unknown'}. "
        f"Host-reported error (log evidence): {host['platformError'] or 'none reported'}. "
        "Complete explanation and any relationship to PR CI remain unknown. "
        "Collection failure does not prove worker failure.\n"
        f"Worker host: https://github.com/{repository}/actions/runs/{host['runId']}/attempts/{host['runAttempt']}\n\n")


def reported_claim(record):
    reported = record.get("workerReport")
    if reported is None:
        return ""
    return (
        f"Claimed deadline (not authorization): {reported['waitUntil'] or 'none reported'}.\n"
        f"Worker-reported evidence ({len(reported['evidence'])}/{reported['evidenceCount']}, bounded): "
        + ("; ".join(reported["evidence"]) or "none retained") + ".\n"
        f"Worker-reported feedback mappings ({len(reported['feedback'])}/{reported['feedbackCount']}, bounded): "
        + ("; ".join(f"{item['id']} → {item['disposition']}: {item['reason']}"
                     for item in reported["feedback"]) or "none retained")
        + f". Full claim audit: {record.get('auditStatus', 'unavailable')}; clipped text is not complete evidence.\n\n")


def report(repository, chain, operation):
    record = operation["result"]
    source = basis(operation)
    return (
        "[automated] **CI Shepherd worker result: " + record["summary"] + "**\n\n"
        f"Acquisition: {record['status']}. {record['reason']}\n\n"
        f"Worker-reported details: {record['details'] or 'No structured worker claim available'}.\n\n"
        + reported_claim(record) +
        f"Platform snapshot: {record['platformState']}; source state: `{source['head']}`. "
        f"Reported platform error: {record['platformError'] or 'none reported'}. "
        f"Artifact mapping: {record['artifactState']}. "
        f"Independently observed PR head: {record['observedHead'] or 'unavailable'}; authorship unverified. "
        "Worker claims are untrusted log evidence, not verified fixes, scope decisions, tests, or green CI. "
        "Matching attempted work is held until substantive new evidence; unrelated feedback remains eligible.\n\n"
        + fallback_report(repository, record) +
        f"Last rendered tool label (untrusted, not execution proof): {record['lastAction'] or 'unavailable'}.\n\n"
        f"Evidence: https://github.com/{repository}/tasks/{operation['taskId']}\n"
        f"Session: {record['session'] or 'ambiguous'}.\n"
        f"{REPORT_MARKER}\nRepository: {repository}; subject: {source['number']}; "
        f"operation: {operation['id']}; result: {record['version']}"
    )


def valid_report(body, repository, number):
    if not isinstance(body, str) or len(body.encode()) > 6000 or body.count(REPORT_MARKER) != 1:
        return False
    prefix = "[automated] **CI Shepherd worker result: "
    base = re.escape(f"https://github.com/{repository}")
    suffix = re.escape(f"Repository: {repository}; subject: {number}; operation: ")
    # Claims can quote the disclaimers. Validate their controller-owned positions,
    # not occurrences in untrusted text; the author and persisted receipt are
    # checked separately by owned_report.
    return re.fullmatch(re.escape(prefix) + r"[^\n]{1,600}\*\*\n\nAcquisition: "
                     r"(?:incomplete|untrusted-task-log)\. [^\n]{1,600}\n\n"
                     r"Worker-reported details: [^\n]{1,601}\.\n\n"
                     r"(?:Claimed deadline \(not authorization\): [^\n]+\.\n"
                     r"Worker-reported evidence \([^\n]+\): [^\n]+\.\n"
                     r"Worker-reported feedback mappings \([^\n]+\): [^\n]+\.\n\n)?"
                     r"Platform snapshot: "
                     r"(?:completed|failed|timed_out|cancelled); source state: "
                     r"`(?:[0-9a-f]{40}|[0-9a-f]{64})`\. "
                     r"Reported platform error: [^\n]+\. "
                     r"Artifact mapping: (?:matched|reported|not-reported)\. "
                     r"Independently observed PR head: (?:[0-9a-f]{40}|unavailable); authorship unverified\. "
                     r"Worker claims are untrusted log evidence, not verified fixes, scope decisions, tests, or green CI\. "
                     r"Matching attempted work is held until substantive new evidence; unrelated feedback remains eligible\.\n\n"
                     r"(?:Fallback: verified task/session metadata and reported artifacts were checked\. "
                     r"No independently mapped worker-host run or authenticated final message is available; "
                     r"last executed action and complete explanation are unknown\. "
                     r"Collection failure does not prove worker failure\.\n\n"
                     r"|Fallback: independently mapped worker-host Actions run conclusion: "
                     r"(?:success|failure|cancelled|timed_out|neutral|skipped)\. "
                     r"Last host-recorded tool event \(not final explanation or PR readiness\): [^\n]+\. "
                     r"Host-reported error \(log evidence\): [^\n]+\. "
                     r"Complete explanation and any relationship to PR CI remain unknown\. "
                     r"Collection failure does not prove worker failure\.\nWorker host: "
                     + base + r"/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*\n\n)"
                     r"Last rendered tool label \(untrusted, not execution proof\): [^\n]+\.\n\n"
                     r"Evidence: " + base + r"/tasks/[A-Za-z0-9_-]+\n"
                     r"Session: [^\n]+\.\n"
                     + re.escape(REPORT_MARKER) + "\n" + suffix
                     + r"[A-Za-z0-9_-]+; result: [0-9a-f]{64}", body) is not None


def owned_report(api, chain, comment):
    if not api.owned(comment):
        return False
    for operation in chain["operations"]:
        if "result" not in operation:
            continue
        source = basis(operation)
        if not valid_report(comment.get("body"), api.repository, source["number"]):
            continue
        records = operation.get("resultReports", []) + (
            [operation["result"]] if "version" in operation["result"] else [])
        for record in records:
            suffix = (f"Repository: {api.repository}; subject: {source['number']}; "
                      f"operation: {operation['id']}; result: {record['version']}")
            if record["commentId"] == comment.get("id") and comment["body"].endswith(suffix):
                return True
    return False


def reconcile_publications(api):
    if not api.write:
        return
    for chain in api.ledger["chains"]:
        if "handoff" in chain:
            continue
        for operation in chain["operations"]:
            record = operation.get("result")
            if record is None or record.get("publication") not in {"pending", "uncertain"}:
                continue
            endpoint = f"{api.prefix}/issues/{basis(operation)['number']}/comments"
            body = report(api.repository, chain, operation)
            from github import IncompleteInventory
            try:
                matches = [comment for comment in api.api.pages(endpoint)
                           if api.owned(comment) and comment.get("body") == body]
            except IncompleteInventory:
                continue
            if len(matches) == 1:
                record.update(publication="sent", commentId=matches[0]["id"])
                api.persist()


def publish(api, chain, observation):
    if "handoff" in chain:
        return
    for operation in chain["operations"]:
        record = operation.get("result")
        if record is None or "version" not in record or record["status"] == "pending" or operation["taskId"] is None:
            continue
        source = basis(operation)
        if source["number"] not in {chain["origin"], chain["child"]}:
            continue
        saved = next((item for item in operation.get("resultReports", []) if item["version"] == record["version"]), None)
        if saved is not None:
            record.update(publication="sent", commentId=saved["commentId"])
            api.persist()
            continue
        body = report(api.repository, chain, operation)
        if not getattr(api, "publish_results", False):
            print("Preview only:\n" + body)
            continue
        if record.get("auditStatus") == "pending":
            # The report must not be published while full-claim durability is
            # unresolved: recovery would otherwise change its reconciliation body.
            continue
        if record["publication"] == "sent":
            continue
        if not api.write or chain["state"] not in {"open", "human"} or not observation["managed"]:
            continue
        endpoint = f"{api.prefix}/issues/{source['number']}/comments"
        if record["publication"] in {"pending", "uncertain"}:
            matches = [comment for comment in api.api.pages(endpoint) if api.owned(comment) and comment.get("body") == body]
            if len(matches) == 1:
                record.update(publication="sent", commentId=matches[0]["id"])
                api.persist()
            # An absent uncertain response is never permission to retry.
            continue
        api.guard(chain, observation, effect=False)
        record["publication"] = "pending"
        api.persist()
        api.guard(chain, observation, effect=False)
        from github import LostResponse, Response
        try:
            response = api.transport("POST", endpoint, {"body": body})
            if (not isinstance(response, Response) or response.status != 201
                    or not isinstance(response.payload, dict)
                    or not isinstance(response.payload.get("user"), dict)
                    or not api.owned(response.payload) or response.payload.get("body") != body
                    or type(response.payload.get("id")) is not int or response.payload["id"] <= 0):
                raise LostResponse("result report response unknown")
        except (LostResponse, ValueError):
            record["publication"] = "uncertain"
            api.persist()
            continue
        record.update(publication="sent", commentId=response.payload["id"])
        api.persist()


def correlation(repository, chain, operation):
    source = basis(operation)
    return {"repository": repository, "number": source["number"], "node": source["node"],
            "sourceHead": source["head"], "chain": chain["id"], "operation": operation["id"],
            "origin": chain["origin"]}


def parse_claim(text, expected, feedback):
    """The rendered CLI mixes tool and assistant output: binding is not provenance."""
    if not isinstance(text, str) or len(text.encode()) > MAX_LOG:
        raise ValueError("result log bound")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError("missing or ambiguous result envelope")
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    if end <= start:
        raise ValueError("reversed envelope")
    # CLI Markdown wraps Base64 across whitespace; no other normalization is safe.
    encoded = re.sub(r"[ \t\r\n\f\v]", "", text[start:end])
    if len(encoded) > 4 * ((MAX_RESULT + 2) // 3):
        raise ValueError("encoded result bound")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_RESULT or base64.b64encode(raw).decode() != encoded:
            raise ValueError("noncanonical result encoding")
        value = contracts.loads(raw.decode("utf-8"), max_bytes=MAX_RESULT)
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("invalid result encoding") from error
    contracts.exact(value, set(expected) | {"schemaVersion", "outcome", "summary", "why",
                    "feedback", "changes", "tests", "evidence", "waitUntil"}, "worker claim")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1 or any(
            type(value[key]) is not type(item) or value[key] != item for key, item in expected.items()):
        raise ValueError("worker claim binding mismatch")
    if not isinstance(value["outcome"], str) or value["outcome"] not in {
            "repair", "no-repair", "out-of-scope-with-evidence", "unresolved", "wait-or-rerun"}:
        raise ValueError("unsupported worker outcome")
    for key in ("summary", "why"):
        issue_pr.text(value[key], key, 2000)
        if not value[key].strip():
            raise ValueError("empty worker explanation")
    if not isinstance(value["feedback"], dict) or set(value["feedback"]) != set(feedback):
        raise ValueError("incomplete worker feedback")
    for item in value["feedback"].values():
        contracts.exact(item, {"disposition", "reason"}, "worker feedback entry")
        if not isinstance(item["disposition"], str) or item["disposition"] not in {
                "addressed", "declined", "unresolved", "wait-or-rerun"}:
            raise ValueError("unsupported feedback claim")
        issue_pr.text(item["reason"], "feedback reason", 600)
        if not item["reason"].strip():
            raise ValueError("empty feedback reason")
    for key in ("changes", "tests", "evidence"):
        if not isinstance(value[key], list) or len(value[key]) > 30:
            raise ValueError("worker list bound")
        for item in value[key]:
            issue_pr.text(item, key, 1000)
    if value["outcome"] != "repair" and (value["changes"] or any(
            item["disposition"] == "addressed" for item in value["feedback"].values())):
        raise ValueError("contradictory no-repair claim")
    if value["outcome"] == "repair" and not value["changes"]:
        raise ValueError("repair lacks changes")
    if value["outcome"] == "out-of-scope-with-evidence" and not value["evidence"]:
        raise ValueError("out-of-scope lacks evidence")
    if value["waitUntil"] is not None:
        if value["outcome"] != "wait-or-rerun" or issue_pr.stamp(issue_pr.timestamp(value["waitUntil"])) != value["waitUntil"]:
            raise ValueError("invalid claimed wait")
    return {"claim": value, "provenance": "untrusted-task-log", "authorizesResolution": False}


def attempt_keys(observation):
    """Only same-feedback substantive evidence can reopen an attempted batch."""
    keys = {}
    for item in observation["feedback"]:
        evidence = [observation.get("feedbackRevisions", {}).get(item["id"], item["body"])]
        if item["id"].startswith("check:"):
            check_id = int(item["id"].split(":")[1])
            # Names are display labels, not a new execution or failure. Raw
            # output/annotation revisions bind diagnostics beyond prompt limits.
            evidence = [item["id"]] + [
                entry for entry in observation["diagnostics"] if entry["checkId"] == check_id]
        keys[item["id"]] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return keys


def gate(chain, observation):
    keys = attempt_keys(observation)
    held = set()
    for operation in chain["operations"]:
        if operation["taskId"] is None or operation["state"] == "no-send":
            continue
        source = basis(operation)
        if any(source.get(key) != observation[key] for key in ("number", "node", "head")):
            continue
        for identity in source["feedback"]:
            for current in keys:
                same = current == identity
                if identity.startswith(("comment:", "review-comment:", "review:")):
                    same |= current.split(":")[:2] == identity.split(":")[:2]
                incomplete = (current.startswith("check:") and any(
                    entry["checkId"] == int(current.split(":")[1]) and not entry.get("complete")
                    for entry in observation["diagnostics"]))
                if same and ("attemptEvidence" not in operation
                             or incomplete
                             or operation["attemptEvidence"].get(identity) == keys[current]):
                    held.add(current)
        if observation["kind"] == "issue" and not observation["feedback"]:
            observation["actionable"] = False
    observation["feedback"] = [item for item in observation["feedback"] if item["id"] not in held]
    if held:
        observation["attemptHold"] = sorted(held)
    if observation["kind"] == "pr" and not observation["feedback"]:
        observation["actionable"] = False
    return held


def basis(operation):
    # Identities contain {"head": "...", "feedback": ["check:...", ...]}:round:N.
    return contracts.loads(operation["identity"].rsplit(":round:", 1)[0])


def summarize(task, operation, pr):
    """No narrative/log endpoint is exposed by the agent-tasks response schema."""
    # https://docs.github.com/en/rest/agent-tasks/agent-tasks#get-a-task-by-repo
    kinds, errors = set(), []
    for artifact in task["artifacts"]:
        if (not isinstance(artifact, dict) or artifact.get("provider") != "github"
                or artifact.get("type") not in {"pull", "branch"} or not isinstance(artifact.get("data"), dict)):
            raise ValueError("worker result artifact unavailable")
        kind, data = artifact["type"], artifact["data"]
        if pr is not None and kind in kinds:
            raise ValueError("ambiguous worker result artifact")
        kinds.add(kind)
        if pr is not None and (
                kind == "pull" and (data.get("id") != pr["id"]
                                    or data.get("global_id") not in {None, "", pr["node_id"]})
                or kind == "branch" and data != {"base_ref": "main", "head_ref": pr["head"]["ref"]}):
            raise ValueError("worker result artifact does not match tracked PR")
    for session in task["sessions"]:
        error = session.get("error")
        if error is not None:
            if not isinstance(error, dict) or "message" in error and not isinstance(error["message"], str):
                raise ValueError("worker session error evidence unavailable")
            errors.append({"sessionId": session["id"], "message": error["message"][:500] if "message" in error else None})
    return {"operation": operation["id"], "taskId": task["id"], "state": task["state"],
            "sessionCount": task["session_count"],
            "sessionIds": [session["id"] for session in task["sessions"]],
            "updatedAt": task.get("updated_at"),
            "sessionStates": sorted({session["state"] for session in task["sessions"]}),
            "sourceHead": basis(operation)["head"],
            "artifactState": "matched" if kinds == {"pull", "branch"} and pr is not None else
                             "reported" if kinds else "not-reported",
            "errors": errors[:4], "errorsTruncated": len(errors) > 4,
            "narrativeAvailable": False}


def eligible(chain, identity, receipts):
    disposition = chain["dispositions"].get(identity)
    if disposition is None:
        return True
    if disposition != "needs-human" or chain["state"] != "open":
        return False
    for operation in reversed(chain["operations"]):
        # Authority validation permits wait provenance only on completed,
        # taskless native operations. Deferral does not supersede the earlier
        # verified worker's eligibility evidence or resolve legacy feedback.
        if "wait" in operation:
            continue
        # The admitted decision must not change its own pre-send fingerprint.
        if operation["state"] in {"reserved", "sent"} or identity not in basis(operation)["feedback"]:
            continue
        decisions = operation.get("feedbackDecisions")
        if decisions is not None:
            return (decisions[identity] == "addressed" and operation["id"] in receipts
                    and receipts[operation["id"]]["state"] in state.TERMINAL)
        if operation["taskId"] is not None:
            return (operation["id"] in receipts and receipts[operation["id"]]["state"] == "completed")
        if operation["state"] == "completed":
            # An old native handoff has no provenance field. Do not guess.
            return False
    return False


def context(chain, receipts, observation, versions=None):
    result = []
    for operation in chain["operations"]:
        if operation["id"] not in receipts:
            continue
        value = dict(receipts[operation["id"]])
        if "result" in operation:
            value["resultSettlement"] = deepcopy(operation["result"])
            value["resultFresh"] = (versions or {}).get(operation["id"]) == operation["result"].get("version")
        value["currentHead"] = observation["head"]
        value["headChanged"] = (value["sourceHead"] != observation["head"]
                                if basis(operation)["number"] == observation["number"] else None)
        result.append(value)
    return result
