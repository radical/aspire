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
MAX_ASSESSMENT_BYTES = 2500
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


def policy_for(api, issue):
    value = api.normal_policy() if hasattr(api, "normal_policy") else None
    if value is None:
        return None
    if isinstance(value, list):
        matches = []
        for entry in value:
            policy = validate_policy(entry, issue)
            if policy is not None:
                matches.append(policy)
        if len(matches) > 1:
            raise ValueError("duplicate issue policy")
        return matches[0] if matches else None
    return validate_policy(value, issue)


def validate_policy(value, issue):
    contracts.exact(value, {"schema_version", "issue", "allow_cloud_task", "allow_branch_writes",
                            "allow_draft_pr", "track_linked_pr", "issue_comment"}, "normal policy")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported normal policy")
    contracts.exact(value["issue"], {"repository", "number", "node_id"}, "normal policy issue")
    items.repository(value["issue"]["repository"])
    issue_pr.positive(value["issue"]["number"], "normal policy issue number")
    items.identifier(value["issue"]["node_id"], "normal policy issue node")
    for key in ("allow_cloud_task", "allow_branch_writes", "allow_draft_pr", "track_linked_pr", "issue_comment"):
        items.boolean(value[key], "normal policy " + key)
    if value["issue"] != issue:
        return None
    return value


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


def pr_observation(api, task):
    pr = execution.task_pr(api, task)
    return {"number": pr["number"], "head": pr["head"]["sha"], "changedFiles": pr["changed_files"],
            "title": followup.safe(pr["title"], api.token, limit=200),
            "body": followup.safe(pr["body"], api.token, limit=800), "bodyDigest": digest(pr["body"])}


def assessment_version(task_version, mapping, result_digest):
    return digest({"task": task_version, "mapping": mapping, "result": result_digest})


def assessment_context(observed, control, assignment, task, mapping):
    saved = assignment["execution"]
    issue = deepcopy(observed)
    issue["body"] = followup.safe(issue["body"], limit=MAX_INTAKE_ISSUE_BODY)
    followup_data = deepcopy(saved["followup"])
    followup_data["assessment"] = None
    context = {"issue": issue, "issueDigest": digest(observed), "control": deepcopy(control),
               "assignment": assignment["id"], "taskVersion": followup.version(task),
               "result": deepcopy(assignment["result"]), "resultDigest": digest(assignment["result"]),
               "followup": followup_data, "mapping": mapping,
               "excerpted": issue["body"] != observed["body"]}
    if len(json.dumps(context).encode()) > MAX_CONTEXT:
        context["result"] = result_excerpt(assignment["result"])
        context["resultCounts"] = {key: len(value) for key, value in (assignment["result"] or {}).items()
                                   if isinstance(value, list)}
        context["followup"]["log_excerpt"] = followup.safe(context["followup"]["log_excerpt"], limit=200)
        context["followup"]["pinned_evidence"] = result_excerpt(context["followup"]["pinned_evidence"])
        context["issue"]["body"] = followup.safe(context["issue"]["body"], limit=MAX_INTAKE_ISSUE_BODY)
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
    ids, packets, sessions = set(), set(), {}
    for value in values:
        keys = {"id", "packetId", "item", "phase", "at", "nativeActual", "nativeReserved",
                "sessionId", "state", "context", "decision"}
        if "turnId" in value:
            keys.add("turnId")
            items.identifier(value["turnId"], "normal turn")
        contracts.exact(value, keys, "normal receipt")
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
            if (value["sessionId"] in sessions
                    and (value.get("turnId") is None or sessions[value["sessionId"]] != value["turnId"])):
                raise ValueError("normal session replay")
            sessions[value["sessionId"]] = value.get("turnId")
        context_limit = MAX_INTAKE_CONTEXT if value["phase"] == "issue-intake" else MAX_CONTEXT
        if not isinstance(value["context"], dict) or len(json.dumps(value["context"]).encode()) > context_limit:
            raise ValueError("normal context exceeds bound")
        if "contextDigest" in value["context"]:
            keys = {"contextDigest", "issueDigest", "issue"}
            if value["phase"] == "issue-assessment":
                keys.add("assessmentDigest")
            contracts.exact(value["context"], keys, "normal context receipt")
            for key in keys - {"issue"}:
                if not isinstance(value["context"][key], str) or not re.fullmatch(
                        r"[0-9a-f]{64}", value["context"][key]):
                    raise ValueError("invalid normal context digest")
        if value["decision"] is not None and not isinstance(value["decision"], dict):
            raise ValueError("invalid normal decision receipt")


def reserve(api, run, now, item, phase, context):
    receipts = api.ledger.setdefault("normalRuns", [])
    for value in receipts:
        same_context = (value["context"]["contextDigest"] == digest(context)
                        if "contextDigest" in value["context"] else value["context"] == context)
        if value["item"] == item and value["phase"] == phase and same_context:
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
    # The full packet lives in the host's run artifacts. Keep its digest in
    # authority instead of duplicating issue bodies, transcripts and inventory.
    value["context"] = {"contextDigest": digest(context), "issueDigest": context["issueDigest"],
                        "issue": {"identity": deepcopy(context["issue"]["identity"])}}
    if phase == "issue-assessment":
        value["context"]["assessmentDigest"] = assessment_version(
            context["taskVersion"], context["mapping"], context["resultDigest"])
    receipts.append(value)
    # Assessment is stored both in the decision receipt and in follow-up.
    # Leave room for both bounded copies, a report and settlement metadata.
    settlement_reserve = 10000 if phase == "issue-assessment" else 16000
    try:
        admitted = len(state.render(api.ledger).encode()) + settlement_reserve <= state.MAX_BODY
    except ValueError:
        receipts.remove(value)
        raise
    if not admitted:
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
    def read_approval():
        approval = api.normal_approval()
        if approval is not None:
            return approval
        policy = policy_for(api, control["issue"])
        if policy is not None and all(policy[key] for key in (
                "allow_cloud_task", "allow_branch_writes", "allow_draft_pr", "track_linked_pr")):
            # Only host-owned policy can preapprove an issue. The model's route
            # and occurrence selection never supply or expand capabilities.
            return execution.approval_template(record, assignment)
        return None

    approval = read_approval()
    if approval is None:
        return {"outcome": "approval-required", "approval_template": execution.approval_template(record, assignment),
                "request": execution.request(api, record, assignment), "control": deepcopy(control)}
    assignment_id = assignment["id"]
    try:
        return execution.execute(api, read, read_approval)
    except execution.AdmissionDeferred as error:
        # execute reloads authority, replacing the original assignment object.
        current = next(value for value in api.ledger["workItems"] if value["id"] == item)
        assignment = next(value for value in current["assignments"] if value["id"] == assignment_id)
        if assignment["execution"]["state"] != "reserved":
            raise
        reason = followup.safe(error, api.token, 500)
        assignment["execution"]["attention"] = reason
        api.persist()
        return {"outcome": "deferred", "reason": reason}


def prepare(api, run, now, feed=None):
    if not hasattr(api, "normal_approval"):
        if feed is not None:
            raise ValueError("normal hosted work-item adapter unavailable")
        return None
    feeds = [] if feed is None else feed if isinstance(feed, list) else [feed]
    incoming = {}
    for entry in feeds:
        number = validate_feed(entry, api.repository)
        if number in incoming:
            raise ValueError("duplicate incoming issue")
        observed = source(api, number)
        incoming[number] = (entry, observed, candidates(api, observed) if eligible(observed) else [])
    # Refresh the complete worklist before dispatch/import/report actions. Active
    # tasks are observations, not a reason to stop intake or wait in this turn.
    observed_items = {}
    worklist = []
    for record in api.ledger.get("workItems", []):
        item = record["id"]
        prior = [value for value in api.ledger.get("normalRuns", [])
                 if value["item"] == item and value["phase"] == "issue-intake" and value["state"] == "settled"]
        saved = record["assignments"][-1].get("execution") if record["assignments"] else None
        row = {"item": item, "issue": deepcopy(record["control"]["issue"]),
               "control": record["control"]["action"],
               "state": saved["state"] if saved else (
                   record["assignments"][-1]["delivery"] if record["assignments"] else "unassigned")}
        # Legacy packet assignments are not normal-loop ownership. In particular,
        # their issue numbers cannot be resolved against the authority repository.
        if record["control"]["issue"]["repository"] != api.repository:
            row["statusOnly"] = "Issue is outside the authority namespace."
        elif not prior:
            row["statusOnly"] = "No settled normal intake receipt."
        if "statusOnly" in row:
            worklist.append(row)
            continue
        observed_items[item] = source(api, record["control"]["issue"]["number"])
        if saved and saved["state"] == "sent":
            try:
                task = execution.refresh(api, record, record["assignments"][-1])
                row["taskState"] = task["state"]
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                row["attention"] = followup.safe(error, api.token, 500)
        if saved and saved["imported_chain_id"]:
            row["chain"] = saved["imported_chain_id"]
        worklist.append(row)
    api.persist()
    packets = []
    for _, observed, _ in incoming.values():
        if not any(record["control"]["issue"] == observed["identity"] for record in api.ledger.get("workItems", [])):
            worklist.append({"item": f"issue-{observed['identity']['number']}-{observed['identity']['node_id']}",
                             "issue": observed["identity"], "state": "incoming" if eligible(observed) else "ineligible"})

    def reserve_packet(item, phase, context):
        # The transport bounds the complete turn, not each child separately.
        # Leave room for the wrapper and refreshed task/handoff status fields.
        size = len(json.dumps({"packets": packets, "worklist": worklist, "context": context}).encode())
        reason = None
        if len(api.ledger.get("normalRuns", [])) >= MAX_RUNS:
            reason = "Normal inference history bound exhausted."
        elif size + 4096 + 1024 * len(worklist) > contracts.MAX_JSON_BYTES:
            reason = "Decision packet capacity exhausted."
        if reason is not None:
            row = next(row for row in worklist if row["item"] == item)
            row["attention"] = reason + " No inference reserved for this item."
            return None
        try:
            return reserve(api, run, now, item, phase, context)
        except ValueError as error:
            import pilot_github
            if (isinstance(error, pilot_github.AuthorityUncertain)
                    or str(error) not in {"normal settlement capacity exhausted", "normal inference credit allowance exhausted"}):
                raise
            row = next(row for row in worklist if row["item"] == item)
            row["attention"] = str(error) + "; no inference reserved for this item."
            return None

    # Tasks are discovered from the canonical ledger, never from feed or previews.
    for record in list(api.ledger.get("workItems", [])):
        item = record["id"]
        prior = [value for value in api.ledger.get("normalRuns", [])
                 if value["item"] == item and value["phase"] == "issue-intake" and value["state"] == "settled"]
        if not prior or item not in observed_items:
            continue
        control = record["control"]
        observed = observed_items[item]
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
            if receipt["outcome"] == "deferred":
                next(row for row in worklist if row["item"] == item)["attention"] = receipt["reason"]
            print("CI Shepherd normal execution: " + json.dumps(followup.redact_fields(receipt, api.token)),
                  file=sys.stderr)
            continue
        if saved["state"] != "sent":
            continue
        try:
            task = execution.refresh(api, record, assignment)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            api.persist()
            print("CI Shepherd normal task refresh unavailable: " + followup.safe(error, api.token, 500),
                  file=sys.stderr)
            continue
        api.persist()
        if task["state"] not in state.TERMINAL:
            continue

        read = read_control(api, item)
        mapping = {"pr": None, "reason": "No platform PR artifact."}
        if any(artifact.get("type") == "pull" for artifact in task["artifacts"]):
            try:
                mapping["observation"] = pr_observation(api, task)
                candidate = execution.mapped_pr(api, record, assignment, task)
            except (ValueError, KeyError, TypeError, AttributeError, IncompleteInventory, LostResponse) as error:
                mapping["reason"] = "PR mapping denied: " + followup.safe(error, api.token, 500)
            else:
                imported = execution.import_pr(api, read)
                if imported["outcome"] != "tracked":
                    raise ValueError("normal PR import failed: " + imported["reason"])
                continue

        previous = saved.get("followup")
        assessments = [entry for entry in api.ledger.get("normalRuns", [])
                       if entry["item"] == item and entry["phase"] == "issue-assessment"
                       and entry["state"] == "settled"]
        current_version = assessment_version(followup.version(task), mapping, digest(assignment["result"]))
        if (previous is not None and previous["assessment"] is not None
                and previous["task_version"] == followup.version(task) and assessments
                and assessments[-1]["context"].get("assessmentDigest") == current_version):
            receipt = followup.report(api, read, publish=True, compact=True)
            print("CI Shepherd normal follow-up report: " + json.dumps(followup.redact_fields(receipt, api.token)),
                  file=sys.stderr)
            continue

        followup.collect(api, read, api.normal_collector)
        record = next(value for value in api.ledger["workItems"] if value["id"] == item)
        assignment = record["assignments"][-1]
        task = execution.refresh(api, record, assignment)
        if task["state"] not in state.TERMINAL:
            continue
        context = assessment_context(observed, control, assignment, task, mapping)
        packet = reserve_packet(item, "issue-assessment", context)
        if packet is not None:
            packets.append(packet)
    for number, (entry, observed, occurrences) in incoming.items():
        if not eligible(observed):
            continue
        item = f"issue-{number}-{observed['identity']['node_id']}"
        if any(record["control"]["issue"] == observed["identity"] for record in api.ledger.get("workItems", [])):
            continue
        context = intake_context(api, entry, observed, occurrences)
        packet = reserve_packet(item, "issue-intake", context)
        if packet is not None:
            packets.append(packet)
    if not packets:
        return None
    if len(worklist) == 1:
        return packets[0]
    for row in worklist:
        record = next((record for record in api.ledger.get("workItems", []) if record["id"] == row["item"]), None)
        if record and record["assignments"]:
            saved = record["assignments"][-1].get("execution")
            if saved:
                row.update(state=saved["state"], taskState=saved["task_state"])
                if saved["imported_chain_id"]:
                    chain = next(chain for chain in api.ledger["chains"] if chain["id"] == saved["imported_chain_id"])
                    row.update(chain=chain["id"], handoff=chain["handoff"]["phase"])
    turn_id = str(uuid.uuid4())
    operations = {packet["operation"] for packet in packets}
    for receipt in api.ledger["normalRuns"]:
        if receipt["id"] in operations:
            receipt["turnId"] = turn_id
    api.persist()
    return {"schemaVersion": 1, "kind": "pilot", "normal": "worklist", "packetId": turn_id,
            "operation": turn_id, "target": api.binding.name, "run": deepcopy(run),
            "preparedAt": issue_pr.stamp(now), "worklist": worklist, "packets": packets}


def validate_decision(packet, decision):
    if packet["normal"] == "worklist":
        contracts.exact(decision, {"schemaVersion", "packetId", "operation", "decisions"}, "worklist decision")
        if (type(decision["schemaVersion"]) is not int or decision["schemaVersion"] != 1
                or decision["packetId"] != packet["packetId"] or decision["operation"] != packet["operation"]
                or not isinstance(decision["decisions"], list)
                or len(decision["decisions"]) != len(packet["packets"])):
            raise ValueError("worklist decision binding mismatch")
        for child, answer in zip(packet["packets"], decision["decisions"]):
            validate_decision(child, answer)
        return
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
        if len(json.dumps(decision["assessment"], separators=(",", ":")).encode()) > MAX_ASSESSMENT_BYTES:
            raise ValueError("normal assessment exceeds storage bound")
    if (type(decision["schemaVersion"]) is not int or decision["schemaVersion"] != 1
            or decision["packetId"] != packet["packetId"] or decision["operation"] != packet["operation"]):
        raise ValueError("normal decision packet binding mismatch")


def check_reservation(api, packet, value):
    if (packet["target"] != api.binding.name or packet["packetId"] != value["packetId"]
            or packet["item"] != value["item"] or packet["normal"] != value["phase"]
            or (digest(packet["context"]) != value["context"]["contextDigest"]
                if "contextDigest" in value["context"] else packet["context"] != value["context"])
            or packet["preparedAt"] != value["at"]):
        raise ValueError("normal packet differs from canonical reservation")


def settle_turn(api, packet, evidence, usage, now, *, billing_only=False):
    import reasoning
    api.read_authority()
    children = packet["packets"]
    if not children or len({child["operation"] for child in children}) != len(children):
        raise ValueError("invalid worklist operations")
    for child in children:
        receipt = next(value for value in api.ledger["normalRuns"] if value["id"] == child["operation"])
        if receipt.get("turnId") != packet["packetId"] or child["target"] != packet["target"]:
            raise ValueError("worklist reservation mismatch")
        check_reservation(api, child, receipt)
    decision = None
    try:
        if not billing_only and evidence is not None:
            decision, _ = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
            validate_decision(packet, decision)
    except (ValueError, KeyError, TypeError):
        decision = None
    # One fresh session serves the turn. Split its measured usage exactly once
    # across the per-item holds; unknown billing retains every outstanding hold.
    shares = [None] * len(children)
    if usage is not None:
        state.amount(usage)
        shares = [usage / len(children)] * len(children)
        shares[-1] = usage - sum(shares[:-1])
    # Settle all measured inference before any child effect. An uncertain send
    # must stop mutations without losing the known usage of later children.
    for child, share in zip(children, shares):
        receipt = next(value for value in api.ledger["normalRuns"] if value["id"] == child["operation"])
        if share is not None or receipt["nativeActual"] is None:
            state.settle_native(receipt, share)
    api.persist()
    results = []
    for index, child in enumerate(children):
        results.append({"item": child["item"], **settle(
            api, child, evidence, shares[index], now, billing_only=billing_only or decision is None,
            _turn_decision=None if decision is None else decision["decisions"][index],
            _turn_id=packet["packetId"])})
    return {"outcome": "processed", "items": results, "waitingForCloud": False}


def settle(api, packet, evidence, usage, now, *, billing_only=False, _turn_decision=None, _turn_id=None):
    if packet["normal"] == "worklist":
        return settle_turn(api, packet, evidence, usage, now, billing_only=billing_only)
    # Billing is independent of issue eligibility, packet expiry and model output.
    api.read_authority()
    value = next(value for value in api.ledger.get("normalRuns", []) if value["id"] == packet["operation"])
    check_reservation(api, packet, value)
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
        if _turn_decision is not None:
            decision = _turn_decision
        if any(other.get("sessionId") == evidence["sessionId"] for other in api.ledger.get("normalRuns", [])
               if other is not value and not (_turn_id is not None and other.get("turnId") == _turn_id
                                             and value.get("turnId") == _turn_id)) or any(operation["sessionId"] == evidence["sessionId"]
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
            task = followup.stable(api, read, control, record, assignment, packet["context"]["taskVersion"])
            if ("observation" in packet["context"]["mapping"]
                    and pr_observation(api, task) != packet["context"]["mapping"]["observation"]):
                raise ValueError("assessment PR evidence changed")
            if (digest(assignment["result"]) != packet["context"]["resultDigest"]
                    or assignment["execution"]["followup"]["task_version"] != packet["context"]["taskVersion"]):
                raise ValueError("assessment result version changed")
            assignment["execution"]["followup"]["assessment"] = followup.redact_fields(
                deepcopy(decision["assessment"]), api.token)
            receiver.fresh(api, read, control)
            value = next(entry for entry in api.ledger["normalRuns"] if entry["id"] == packet["operation"])
            value.update(state="settled", decision=followup.redact_fields(deepcopy(decision), api.token))
            api.persist()
            report = followup.report(api, read, publish=True, compact=True)
            return {"outcome": "assessed", "report": report, "fixValidated": False}
        if decision["action"] == "human":
            value.update(state="settled", decision=followup.redact_fields(deepcopy(decision), api.token))
            api.persist()
            return {"outcome": "human", "reason": decision["reason"]}
        fresh = candidates(api, observed)
        if digest(fresh) != packet["context"]["candidateDigest"]:
            raise ValueError("occurrence evidence changed")
        selected = next(entry for entry in fresh if entry["id"] == decision["evidenceId"])
        policy = policy_for(api, observed["identity"])
        control = {"schema_version": 1, "id": value["item"], "revision": 1, "issue": observed["identity"],
                   "reported_kind": packet["context"]["feed"]["reported_kind"], "requested_route": decision["route"],
                   "action": "run", "occurrence": selected["occurrence"], "scope": SCOPE,
                   "authority": {"local_edits": False, "issue_comment": bool(policy and policy["issue_comment"]),
                                 "draft_pr": False,
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
