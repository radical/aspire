"""Explicit normal issue intake and result assessment on the existing authority."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re
import sys
import uuid

from github import IncompleteInventory, LostResponse
import issue_pr
import live
import pilot_state as state
import round as contracts
import work_item_execution as execution
import work_item_followup as followup
import work_item_receiver as receiver
import work_items as items


MAX_RUNS = 20
MAX_CONTEXT = 12000
MAX_INTAKE_CONTEXT = 32 * 1024
MAX_INTAKE_ISSUE_BODY = 800
MAX_RUN_REFERENCES = 100
MAX_FAILED_OCCURRENCES = 100
SCOPE = ("Investigate only the linked issue and the host-verified occurrence using the standard "
         "repository investigation instructions. Establish the cause before changing code. "
         "Do not rerun CI, change credentials or permissions, publish comments/issues, "
         "merge or close issues. Report an evidence-backed no-fix result when appropriate.")


def validate_feed(value, repository):
    contracts.exact(value, {"reported_kind", "issue_url"}, "minimal issue feed")
    if value["reported_kind"] not in items.ROUTES | {"unknown"}:
        raise ValueError("unsupported reported kind")
    if (not isinstance(value["issue_url"], str) or not re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/issues/[1-9][0-9]*", value["issue_url"])):
        raise ValueError("canonical issue URL must match the authority namespace")
    return int(value["issue_url"].rsplit("/", 1)[1])


def source(api, number):
    value = api.api.get(f"{api.prefix}/issues/{number}")
    if (value.get("number") != number or "pull_request" in value
            or value.get("repository_url") != "https://api.github.com/" + api.prefix):
        raise ValueError("exact source issue identity unavailable")
    items.identifier(value.get("node_id"), "source issue node")
    issue_pr.timestamp(value.get("updated_at"))
    issue_pr.choice(value.get("state"), {"open", "closed"}, "source state")
    for key, limit in (("title", 500), ("body", 6000)):
        items.text(value.get(key), "source " + key, limit)
    labels = value.get("labels")
    if not isinstance(labels, list) or len(labels) > 100:
        raise ValueError("bounded source labels unavailable")
    names = []
    for label in labels:
        items.text(label.get("name"), "source label", 100)
        names.append(label["name"])
    # Issue evidence contains canonical Actions links, possibly /attempts/N.
    # Extract only links for the selected repository; they are candidates, not
    # an instruction to select the newest run or a remotely supplied endpoint.
    matches = re.findall(rf"https://github\.com/{re.escape(api.repository)}/actions/runs/"
                         r"([1-9][0-9]*)(?:/attempts/([1-9][0-9]*))?(?![0-9])", value["body"])
    references = sorted({(int(run), int(attempt) if attempt else None) for run, attempt in matches},
                        key=lambda pair: (pair[0], pair[1] or 0))
    if len(references) > MAX_RUN_REFERENCES:
        raise ValueError("issue run reference budget exhausted")
    return {"identity": {"repository": api.repository, "number": number, "node_id": value["node_id"]},
            "state": value["state"], "labels": sorted(names), "updatedAt": value["updated_at"],
            "title": followup.redact(value["title"], api.token),
            "body": followup.redact(value["body"], api.token), "references": [list(pair) for pair in references]}


def eligible(observed):
    return observed["state"] == "open" and "shepherd-hands-off" not in observed["labels"]


def candidates(api, observed):
    values = []
    for run_id, attempt in observed["references"]:
        path = f"{api.prefix}/actions/runs/{run_id}"
        run = api.api.get(path if attempt is None else path + f"/attempts/{attempt}")
        if (run.get("id") != run_id or run.get("repository", {}).get("id") != api.repository_id
                or run.get("repository", {}).get("full_name") != api.repository):
            raise ValueError("referenced run repository/identity mismatch")
        issue_pr.positive(run.get("run_attempt"), "run attempt")
        items.sha(run.get("head_sha"))
        if attempt is not None and run["run_attempt"] != attempt:
            raise ValueError("referenced attempt mismatch")
        attempt = run["run_attempt"]
        jobs = api.api.pages(path + f"/attempts/{attempt}/jobs", key="jobs",
                             require_total_count=True, max_pages=live.MAX_PAGES,
                             max_bytes=live.MAX_API_JSON_BYTES)
        seen = set()
        for job in jobs:
            issue_pr.positive(job.get("id"), "job ID")
            if job["id"] in seen:
                raise ValueError("duplicate job identity")
            seen.add(job["id"])
            if (job.get("run_id") != run_id or job.get("run_attempt") != attempt
                    or job.get("head_sha") != run["head_sha"]):
                raise ValueError("job occurrence mismatch")
            if job.get("status") != "completed" or job.get("conclusion") not in {"failure", "timed_out"}:
                continue
            if len(values) >= MAX_FAILED_OCCURRENCES:
                raise ValueError("failed occurrence budget exhausted")
            items.text(job.get("name"), "job name", 300)
            steps = job.get("steps")
            if not isinstance(steps, list) or len(steps) > 50:
                raise ValueError("bounded job steps unavailable")
            failed = []
            for step in steps:
                if step.get("conclusion") not in {"failure", "timed_out"}:
                    continue
                items.text(step.get("name"), "step name", 300)
                failed.append(followup.safe(step["name"], api.token, 300))
            occurrence = {"repository": api.repository, "run_id": run_id, "run_attempt": attempt,
                          "head_sha": run["head_sha"], "job_id": job["id"], "artifact_id": None}
            values.append({"id": f"{run_id}:{attempt}:{job['id']}",
                           "occurrence": occurrence, "job": followup.safe(job["name"], api.token, 300),
                           "conclusion": job["conclusion"], "failedSteps": failed})
    return values


def native_spend(ledger, now=None, item=None):
    return sum(value["nativeReserved"] + (value["nativeActual"] or 0
               if now is None or issue_pr.timestamp(value["at"]) > now - timedelta(hours=24) else 0)
               for value in ledger.get("normalRuns", []) if item is None or value["item"] == item)


def item_spend(ledger, item):
    return native_spend(ledger, item=item) + sum((saved["worker_actual"] or 0) + saved["worker_reserved"]
        for record, _, saved in execution.executions(ledger) if record["id"] == item)


def digest(value):
    # Bind the complete untrusted claims across the packet/authority boundary,
    # including portions omitted from the model's bounded descriptive view.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def context_matches_issue(context, observed):
    if "issueDigest" in context:
        return digest(observed) == context["issueDigest"]
    return observed == context["issue"]


def intake_context(api, feed, observed, candidates):
    issue = deepcopy(observed)
    issue["body"] = followup.safe(issue["body"], api.token, limit=MAX_INTAKE_ISSUE_BODY)
    candidate_rows = [[candidate["id"], candidate["job"], candidate["conclusion"], candidate["failedSteps"]]
                      for candidate in candidates]
    return {"feed": deepcopy(feed), "issue": issue, "issueDigest": digest(observed),
            "candidates": candidate_rows, "candidateDigest": digest(candidates)}


def result_excerpt(value):
    if isinstance(value, dict):
        return {key: result_excerpt(entry) for key, entry in value.items()}
    if isinstance(value, list):
        return [result_excerpt(entry) for entry in value[:3]]
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + " [excerpted]"
    return deepcopy(value)


def assessment_context(observed, control, assignment, task, mapping):
    saved = assignment["execution"]
    context = {"issue": deepcopy(observed), "issueDigest": digest(observed), "control": deepcopy(control),
               "assignment": assignment["id"], "taskVersion": followup.version(task),
               "result": deepcopy(assignment["result"]), "resultDigest": digest(assignment["result"]),
               "followup": deepcopy(saved["followup"]), "mapping": mapping, "excerpted": False}
    context["followup"]["assessment"] = None
    if len(json.dumps(context).encode()) > MAX_CONTEXT:
        context["result"] = result_excerpt(assignment["result"])
        context["resultCounts"] = {key: len(value) for key, value in (assignment["result"] or {}).items()
                                   if isinstance(value, list)}
        context["followup"]["log_excerpt"] = followup.safe(context["followup"]["log_excerpt"], limit=200)
        context["followup"]["pinned_evidence"] = result_excerpt(context["followup"]["pinned_evidence"])
        context["issue"]["body"] = followup.safe(context["issue"]["body"], limit=1000)
        context["excerpted"] = True
        if len(json.dumps(context).encode()) > MAX_CONTEXT:
            context["result"] = {key: result_excerpt(value) for key, value in (assignment["result"] or {}).items()
                                 if key in {"schema_version", "item_id", "assignment_id", "evaluated_revision",
                                            "worker_id", "actual_classification", "transience", "outcome",
                                            "same_failure", "summary", "recommended_route"}}
            context["issue"]["labels"] = context["issue"]["labels"][:10]
            context["followup"]["pinned_evidence"] = []
    return context


def validate_receipts(values):
    if not isinstance(values, list) or len(values) > MAX_RUNS:
        raise ValueError("normal inference history bound exhausted")
    ids, packets, sessions = set(), set(), set()
    for value in values:
        contracts.exact(value, {"id", "packetId", "item", "phase", "at", "nativeActual", "nativeReserved",
                               "sessionId", "state", "context", "decision"}, "normal receipt")
        for key, seen in (("id", ids), ("packetId", packets)):
            items.identifier(value[key], key)
            if value[key] in seen:
                raise ValueError("duplicate normal receipt")
            seen.add(value[key])
        items.identifier(value["item"], "normal item")
        issue_pr.timestamp(value["at"])
        issue_pr.choice(value["phase"], {"issue-intake", "issue-assessment"}, "normal phase")
        issue_pr.choice(value["state"], {"reserved", "settled", "rejected"}, "normal state")
        state.amount(value["nativeReserved"])
        if value["nativeActual"] is not None:
            state.amount(value["nativeActual"])
        if value["sessionId"] is not None:
            items.identifier(value["sessionId"], "native session")
            if value["sessionId"] in sessions:
                raise ValueError("normal session replay")
            sessions.add(value["sessionId"])
        context_limit = MAX_INTAKE_CONTEXT if value["phase"] == "issue-intake" else MAX_CONTEXT
        if not isinstance(value["context"], dict) or len(json.dumps(value["context"]).encode()) > context_limit:
            raise ValueError("normal context exceeds bound")
        if value["decision"] is not None and not isinstance(value["decision"], dict):
            raise ValueError("invalid normal decision receipt")


def reserve(api, run, now, item, phase, context):
    receipts = api.ledger.setdefault("normalRuns", [])
    for value in receipts:
        if value["item"] == item and value["phase"] == phase and value["context"] == context:
            # Failed, expired or unknown inference is never silently retried.
            return None
    if any(value["item"] == item and value["state"] == "reserved" for value in receipts):
        return None
    context_limit = MAX_INTAKE_CONTEXT if phase == "issue-intake" else MAX_CONTEXT
    if len(receipts) >= MAX_RUNS or len(json.dumps(context).encode()) > context_limit:
        raise ValueError("normal inference/context bound exhausted")
    if (item_spend(api.ledger, item) + state.NATIVE_RESERVE > state.chain_allowance(api.ledger)
            or state.repository_spend(api.ledger, now) + state.NATIVE_RESERVE > state.REPOSITORY_ALLOWANCE):
        raise ValueError("normal inference credit allowance exhausted")
    value = {"id": str(uuid.uuid4()), "packetId": str(uuid.uuid4()), "item": item, "phase": phase,
             "at": issue_pr.stamp(now), "nativeActual": None, "nativeReserved": state.NATIVE_RESERVE,
             "sessionId": None, "state": "reserved", "context": deepcopy(context), "decision": None}
    receipts.append(value)
    if len(state.render(api.ledger).encode()) + 16000 > state.MAX_BODY:
        receipts.remove(value)
        raise ValueError("normal settlement capacity exhausted")
    api.persist()
    return {"schemaVersion": 1, "kind": "pilot", "normal": phase, "packetId": value["packetId"],
            "operation": value["id"], "item": item, "run": deepcopy(run), "preparedAt": value["at"],
            "target": api.binding.name, "context": deepcopy(context)}


def read_control(api, item):
    def read():
        latest = api.read_authority()
        matches = [value for value in latest.get("workItems", []) if value["id"] == item]
        if len(matches) != 1:
            raise ValueError("canonical normal work item unavailable")
        return deepcopy(matches[0]["control"])
    return read


def dispatch_reserved(api, item):
    read = read_control(api, item)
    control = read()
    if control["action"] != "run":
        return {"outcome": "paused"}
    record = next(value for value in api.ledger["workItems"] if value["id"] == item)
    assignment = record["assignments"][-1]
    if assignment["execution"]["state"] != "reserved":
        return {"outcome": assignment["execution"]["state"]}
    approval = api.normal_approval()
    if approval is None:
        return {"outcome": "approval-required", "approval_template": execution.approval_template(record, assignment),
                "request": execution.request(api, record, assignment), "control": deepcopy(control)}
    return execution.execute(api, read, api.normal_approval)


def prepare(api, run, now, feed=None):
    if not hasattr(api, "normal_approval"):
        if feed is not None:
            raise ValueError("normal hosted work-item adapter unavailable")
        return None
    # Tasks are discovered from the canonical ledger, never from feed or previews.
    for record in list(api.ledger.get("workItems", [])):
        item = record["id"]
        prior = [value for value in api.ledger.get("normalRuns", [])
                 if value["item"] == item and value["phase"] == "issue-intake" and value["state"] == "settled"]
        if not prior:
            continue
        control = record["control"]
        observed = source(api, control["issue"]["number"])
        if (not eligible(observed) or control["action"] == "pause"
                or not context_matches_issue(prior[-1]["context"], observed)):
            continue
        if not record["assignments"]:
            execution.prepare(api, read_control(api, item))
            record = next(value for value in api.ledger["workItems"] if value["id"] == item)
        assignment = record["assignments"][-1]
        saved = assignment.get("execution")
        if saved is None or saved["imported_chain_id"] is not None or control["revision"] != assignment["revision"]:
            continue
        if saved["state"] == "reserved":
            receipt = dispatch_reserved(api, item)
            print("CI Shepherd normal execution: " + json.dumps(followup.redact_fields(receipt, api.token)),
                  file=sys.stderr)
            return None
        if saved["state"] != "sent" or saved["task_state"] not in state.TERMINAL:
            continue
        read = read_control(api, item)
        followup.collect(api, read, api.normal_collector)
        record = next(value for value in api.ledger["workItems"] if value["id"] == item)
        assignment = record["assignments"][-1]
        saved = assignment["execution"]
        if saved["task_state"] not in state.TERMINAL:
            continue
        task = execution.refresh(api, record, assignment)
        mapping = {"pr": None, "reason": "No platform PR artifact."}
        if any(artifact.get("type") == "pull" for artifact in task["artifacts"]):
            try:
                mapping = {"pr": execution.mapped_pr(api, record, assignment, task),
                           "reason": "Exact platform mapping; not independent fix validation."}
            except (ValueError, KeyError, TypeError, AttributeError, IncompleteInventory, LostResponse) as error:
                mapping["reason"] = "PR mapping denied: " + followup.safe(error, api.token, 500)
        context = assessment_context(observed, control, assignment, task, mapping)
        packet = reserve(api, run, now, item, "issue-assessment", context)
        if packet is not None:
            return packet
    if feed is None:
        return None
    number = validate_feed(feed, api.repository)
    observed = source(api, number)
    if not eligible(observed):
        return None
    item = f"issue-{number}-{observed['identity']['node_id']}"
    if any(record["control"]["issue"] == observed["identity"] for record in api.ledger.get("workItems", [])):
        return None
    context = intake_context(api, feed, observed, candidates(api, observed))
    return reserve(api, run, now, item, "issue-intake", context)


def validate_decision(packet, decision):
    keys = {"schemaVersion", "packetId", "operation", "action"}
    if packet["normal"] == "issue-intake":
        contracts.exact(decision, keys | {"route", "evidenceId", "reason"}, "normal intake decision")
        if decision["action"] not in {"investigate", "human"}:
            raise ValueError("unsupported intake action")
        items.text(decision["reason"], "intake reason", 500)
        if decision["action"] == "human":
            if decision["route"] is not None or decision["evidenceId"] is not None:
                raise ValueError("human decision cannot select work")
        else:
            issue_pr.choice(decision["route"], items.ROUTES, "investigation route")
            if decision["evidenceId"] not in {value[0] for value in packet["context"]["candidates"]}:
                raise ValueError("selected occurrence absent from host evidence")
    else:
        contracts.exact(decision, keys | {"assessment"}, "normal assessment decision")
        if decision["action"] != "assess":
            raise ValueError("only nonauthorizing assessment is supported")
        followup.validate_assessment(decision["assessment"])
    if (type(decision["schemaVersion"]) is not int or decision["schemaVersion"] != 1
            or decision["packetId"] != packet["packetId"] or decision["operation"] != packet["operation"]):
        raise ValueError("normal decision packet binding mismatch")


def settle(api, packet, evidence, usage, now, *, billing_only=False):
    # Billing is independent of issue eligibility, packet expiry and model output.
    api.read_authority()
    value = next(value for value in api.ledger.get("normalRuns", []) if value["id"] == packet["operation"])
    if (packet["target"] != api.binding.name or packet["packetId"] != value["packetId"]
            or packet["item"] != value["item"] or packet["normal"] != value["phase"]
            or packet["context"] != value["context"] or packet["preparedAt"] != value["at"]):
        raise ValueError("normal packet differs from canonical reservation")
    if usage is not None or value["nativeActual"] is None:
        state.settle_native(value, usage)
    api.persist()
    if value["state"] != "reserved":
        return {"outcome": "replay"}
    try:
        if billing_only or evidence is None:
            raise ValueError("disabled or missing native decision evidence")
        import reasoning
        decision, _ = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
        if any(other.get("sessionId") == evidence["sessionId"] for other in api.ledger.get("normalRuns", [])
               if other is not value) or any(operation["sessionId"] == evidence["sessionId"]
               for chain in api.ledger["chains"] for operation in chain["operations"]):
            raise ValueError("native session reused")
        value["sessionId"] = evidence["sessionId"]
        api.persist()
        if not issue_pr.timestamp(value["at"]) <= now < issue_pr.timestamp(value["at"]) + timedelta(minutes=10):
            raise ValueError("normal packet expired")
        validate_decision(packet, decision)
        observed = source(api, packet["context"]["issue"]["identity"]["number"])
        source_matches = context_matches_issue(packet["context"], observed)
        if not source_matches or not eligible(observed):
            raise ValueError("source issue stale, closed or hands-off")
        if packet["normal"] == "issue-assessment":
            read = read_control(api, value["item"])
            control, record, assignment = followup.owned(api, read)
            if control != packet["context"]["control"] or assignment["id"] != packet["context"]["assignment"]:
                raise ValueError("assessment control or assignment changed")
            followup.stable(api, read, control, record, assignment, packet["context"]["taskVersion"])
            if (digest(assignment["result"]) != packet["context"]["resultDigest"]
                    or assignment["execution"]["followup"]["task_version"] != packet["context"]["taskVersion"]):
                raise ValueError("assessment result version changed")
            assignment["execution"]["followup"]["assessment"] = followup.redact_fields(
                deepcopy(decision["assessment"]), api.token)
            receiver.fresh(api, read, control)
            value = next(entry for entry in api.ledger["normalRuns"] if entry["id"] == packet["operation"])
            value.update(state="settled", decision=followup.redact_fields(deepcopy(decision), api.token))
            api.persist()
            return {"outcome": "assessed", "publications": [], "fixValidated": False}
        if decision["action"] == "human":
            value.update(state="settled", decision=followup.redact_fields(deepcopy(decision), api.token))
            api.persist()
            return {"outcome": "human", "reason": decision["reason"]}
        fresh = candidates(api, observed)
        if digest(fresh) != packet["context"]["candidateDigest"]:
            raise ValueError("occurrence evidence changed")
        selected = next(entry for entry in fresh if entry["id"] == decision["evidenceId"])
        control = {"schema_version": 1, "id": value["item"], "revision": 1, "issue": observed["identity"],
                   "reported_kind": packet["context"]["feed"]["reported_kind"], "requested_route": decision["route"],
                   "action": "run", "occurrence": selected["occurrence"], "scope": SCOPE,
                   "authority": {"local_edits": False, "issue_comment": False, "draft_pr": False,
                                 "issue_create": False, "destination": None}, "pr_review": None}
        api.guard_work_item(control)
        receiver.record_for(api, control)
        value.update(state="settled", decision=followup.redact_fields(deepcopy(decision), api.token))
        api.persist()
        read = read_control(api, value["item"])
        execution.prepare(api, read)
        return dispatch_reserved(api, value["item"])
    except (ValueError, KeyError, TypeError, StopIteration, IncompleteInventory, LostResponse) as error:
        # AuthorityUncertain propagates: it must never be compensated as a
        # known rejected action after a possibly successful authority write.
        import pilot_github
        if isinstance(error, pilot_github.AuthorityUncertain):
            raise
        value = next(entry for entry in api.ledger["normalRuns"] if entry["id"] == packet["operation"])
        if value["state"] == "reserved":
            value["state"] = "rejected"
        api.persist()
        return {"outcome": "rejected", "reason": followup.safe(error, api.token, 500)}
