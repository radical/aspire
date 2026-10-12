"""Sticky PR ownership transfer within the existing serialized Shepherd core."""

from copy import deepcopy
from datetime import timedelta
import json
import re
import sys
import uuid

from github import IncompleteInventory, LostResponse, Response
import prompt_templates
import issue_pr
import round as contracts

PHASES = {"initial", "handoff_pending", "handoff_needed", "watching", "merged", "closed"}
SENDS = {"idle", "prepared", "sent", "known", "uncertain", "human"}
APP_STAGES = ("intent", "session-bound", "activation-requested")
APP_ACTIONS = {"address_reviews": True, "fix_ci": True, "resolve_conflicts": True, "merge_pr": False}


def app_settings(value, *, enabled=False):
    contracts.exact(value, {"enabled", "actions"}, "current app settings")
    contracts.exact(value["actions"], set(APP_ACTIONS), "current app actions")
    if (type(value["enabled"]) is not bool
            or any(type(action) is not bool for action in value["actions"].values())
            or value["actions"]["merge_pr"] is not False):
        raise ValueError("Current app settings require exact booleans and merging OFF.")
    if enabled and (value["enabled"] is not True or value["actions"] != APP_ACTIONS):
        raise ValueError("Current Agent Merge must be enabled with all three repair actions ON and merging OFF.")
    return value


def app_identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ValueError(f"invalid {label}")


def app_head(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid app head")


def validate_app(value):
    contracts.exact(value, {"attemptId", "intentHead", "stage", "sessionId", "activationSettings",
                            "confirmedHead", "settings"}, "app transfer")
    if (not isinstance(value["attemptId"], str) or str(uuid.UUID(value["attemptId"])) != value["attemptId"]
            or value["stage"] not in APP_STAGES):
        raise ValueError("invalid app attempt/stage")
    app_head(value["intentHead"])
    if value["sessionId"] is not None:
        app_identifier(value["sessionId"], "native app session")
    if (value["stage"] == "intent") != (value["sessionId"] is None):
        raise ValueError("app stage/session binding mismatch")
    if value["stage"] == "activation-requested":
        app_settings(value["activationSettings"])
    elif value["activationSettings"] is not None:
        raise ValueError("app activation settings before activation boundary")
    if value["confirmedHead"] is not None:
        app_head(value["confirmedHead"])
        if value["stage"] != "activation-requested":
            raise ValueError("app confirmation before activation boundary")
        app_settings(value["settings"], enabled=True)
    elif value["settings"] is not None:
        raise ValueError("app settings without confirmation")


def validate(value):
    contracts.exact(value, {"id", "phase", "responsible", "head", "progressAt", "confirmedAt",
                           "sendState", "taskId", "attention", "mergeLabel"}
                    | ({"taskTerminal"} if "taskTerminal" in value else set())
                    | ({"appTransfer"} if "appTransfer" in value else set()), "manual handoff")
    if str(uuid.UUID(value["id"])) != value["id"] or value["phase"] not in PHASES:
        raise ValueError("invalid handoff identity/phase")
    if value["responsible"] != "radical" or value["sendState"] not in SENDS:
        raise ValueError("invalid handoff operator/send boundary")
    if value["head"] is not None and not re.fullmatch(r"[0-9a-f]{40}", value["head"]):
        raise ValueError("invalid handoff head")
    issue_pr.timestamp(value["progressAt"])
    if value["confirmedAt"] is not None:
        issue_pr.timestamp(value["confirmedAt"])
    if value["taskId"] is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value["taskId"]):
        raise ValueError("invalid initial task identity")
    if "taskTerminal" in value and (type(value["taskTerminal"]) is not bool
            or value["taskId"] is None or value["sendState"] != "known"):
        raise ValueError("terminal capacity receipt requires a known initial task")
    if value["attention"] is not None:
        issue_pr.text(value["attention"], "handoff attention", 1000)
    if value["mergeLabel"] not in {"none", "sent", "uncertain", "confirmed"}:
        raise ValueError("invalid merge-label receipt")
    if value["phase"] == "watching" and value["confirmedAt"] is None:
        raise ValueError("watching requires explicit operator confirmation")
    if "appTransfer" in value:
        validate_app(value["appTransfer"])
        if value["appTransfer"]["confirmedHead"] is not None and value["confirmedAt"] is None:
            raise ValueError("app confirmation requires handoff confirmation")


def converted(chain):
    return "handoff" in chain


def blocked(chain):
    return converted(chain) and (chain["handoff"]["phase"] != "initial" or chain["child"] is not None)


def unresolved_credit(ledger):
    return any(converted(chain) and chain["handoff"]["phase"] not in {"initial", "handoff_pending"} and (
        any(operation["nativeReserved"] or operation["workerReserved"] for operation in chain["operations"])
        or any(review["reserved"] for review in chain.get("reviews", [])))
        for chain in ledger["chains"])


def enroll(api, chain, now):
    if converted(chain) or api.pr_handoff != "manual" or chain["state"] != "open":
        return
    if api.repository not in {"radical/aspire", "microsoft/aspire"}:
        raise ValueError("manual PR handoff target is unsupported")
    if api.repository == "microsoft/aspire":
        if chain["kind"] != "pr":
            return
        pr = api.mapping(chain["origin"])
        from pilot_github import managed
        if pr["node_id"] != chain["node"] or not managed(pr):
            return
    initial = chain["kind"] == "issue" and chain["child"] is None and not chain["operations"]
    chain["handoff"] = {"id": str(uuid.uuid4()), "phase": "initial" if initial else "handoff_pending",
                        "responsible": api.actor["login"], "head": None, "progressAt": issue_pr.stamp(now),
                        "confirmedAt": None, "sendState": "idle", "taskId": None, "attention": None,
                        "mergeLabel": "none"}


def quiescent(api, chain):
    """Read every owned task; admission receipts, not age, distinguish unsent work."""
    import pilot_reviews
    import pilot_state as state
    if "sourceWorkItem" in chain:
        import work_item_execution
        work_item_execution.quiescent(api, chain)
        return None
    record = chain["handoff"]
    record.pop("taskTerminal", None)
    if record["sendState"] in {"sent", "uncertain"}:
        raise ValueError("Initial task send outcome unknown; no retry or cancellation.")
    tasks = [(op["taskId"], op) for op in chain["operations"] if op["taskId"] is not None]
    if any(op["taskId"] is None and op["state"] in {"reserved", "sent", "waiting", "uncertain"}
           for op in chain["operations"]):
        raise ValueError("Owned legacy admission/send unresolved; wait for definitive no-send or task receipt.")
    pending_reviews = [review for review in chain.get("reviews", []) if review["state"] in pilot_reviews.PENDING]
    if pending_reviews:
        pr = api.mapping(chain["child"] or chain["origin"])
        inventory = api.api.pages(f"{api.prefix}/pulls/{pr['number']}/reviews")
        evidence = pilot_reviews.evidence(chain, pr, inventory, False)
        if (evidence["requested"] or evidence["inProgress"] or any(
                not any(receipt["head"] == review["head"] for receipt in evidence["receipts"])
                for review in pending_reviews)):
            raise ValueError("Owned review request unresolved; human verification required.")
    if record["taskId"] is not None:
        tasks.append((record["taskId"], {"id": record["id"]}))
    initial_task = None
    for task_id, operation in tasks:
        task = api.api.get(f"agents/repos/{api.repository}/tasks/{task_id}")
        api.verify_task(task, task_id, chain, operation, summarize=False)
        if task["state"] not in state.TERMINAL:
            raise ValueError("Owned task/session still active; wait without cancellation.")
        if task_id == record["taskId"]:
            record["taskTerminal"] = True
            initial_task = task
        elif chain["kind"] == "issue" and chain["child"] is None and task["artifacts"]:
            if initial_task is not None:
                raise ValueError("Multiple owned issue task artifacts; human mapping verification required.")
            initial_task = task
    return initial_task


def refresh_capacity(api):
    """Revalidate released compact slots without mapping or replacing workers."""
    changed = False
    for chain in api.ledger["chains"]:
        record = chain.get("handoff")
        if record is None or record["phase"] not in {"initial", "handoff_pending"} or not record.get("taskTerminal"):
            continue
        before = deepcopy(record)
        try:
            quiescent(api, chain)
        except (ValueError, KeyError, TypeError, IncompleteInventory) as error:
            record["attention"] = str(error)[:1000]
            print(f"CI Shepherd handoff #{chain['origin']} capacity attention-needed: {record['attention']}", file=sys.stderr)
        changed |= record != before
    if changed:
        api.persist()


def map_child(api, chain, task):
    """Reuse the exact artifact mapping without adopting the PR into repairs."""
    from pilot_github import managed
    origin = api.api.get(f"{api.prefix}/issues/{chain['origin']}")
    if origin["number"] != chain["origin"] or origin["node_id"] != chain["node"] or not managed(origin):
        raise ValueError("Source issue identity/management changed before child mapping.")
    pulls = [item["data"] for item in task["artifacts"]
             if item.get("provider") == "github" and item.get("type") == "pull"]
    branches = [item["data"] for item in task["artifacts"]
                if item.get("provider") == "github" and item.get("type") == "branch"]
    if len(pulls) != 1 or len(branches) != 1:
        raise ValueError("Initial worker has no unique verified PR/branch artifact; no second implementation.")
    candidates = [value for value in api.api.pages(f"{api.prefix}/pulls", query={"state": "all"})
                  if value["id"] == pulls[0]["id"]]
    if len(candidates) != 1:
        raise ValueError("Initial task PR artifact mapping unavailable.")
    pr = api.mapping(candidates[0]["number"])
    if (branches[0] != {"head_ref": pr["head"]["ref"], "base_ref": pr["base"]["ref"]}
            or pulls[0].get("global_id") not in {None, "", pr["node_id"]}
            or any(session["head_ref"] != pr["head"]["ref"] or session["base_ref"] != "main"
                   for session in task["sessions"])):
        raise ValueError("Initial task/session/PR artifact mismatch.")
    ref = api.api.get(f"{api.prefix}/git/ref/heads/{pr['head']['ref']}")
    if ref.get("ref") != "refs/heads/" + pr["head"]["ref"] or ref.get("object", {}).get("sha") != pr["head"]["sha"]:
        raise ValueError("Independent initial PR branch mapping mismatch.")
    if not api.write:
        raise ValueError("Read-only verified PR discovery; mapping must be persisted by the authorized writer.")
    import pilot_state as state
    state.bind_child(api.ledger, chain, pr["number"], pr["node_id"])
    api.persist()
    return pr


def observe(api, chain):
    record = chain["handoff"]
    number = chain["child"] or chain["origin"]
    result = {"number": number, "node": chain["childNode"] or chain["node"],
              "kind": "pr" if chain["child"] or chain["kind"] == "pr" else "issue",
              "head": record["head"] or "0" * 64, "state": "unknown", "managed": False,
              "originManaged": None, "originAdopted": None, "originHandsOff": None,
              "handsOff": None, "actionable": False, "ready": False,
              "feedback": [], "pendingCI": False, "approval": None, "workflowAttention": None,
              "attention": None, "url": f"https://github.com/{api.repository}/"
              + ("pull/" if chain["child"] or chain["kind"] == "pr" else "issues/") + str(number)}
    try:
        task = quiescent(api, chain) if (
            "sourceWorkItem" in chain or record["phase"] in {"initial", "handoff_pending"}) else None
        if chain["kind"] == "issue" and chain["child"] is None:
            if task is None:
                raise ValueError("Initial implementation has no verified PR; no redispatch.")
            map_child(api, chain, task)
            record["phase"] = "handoff_pending"
            number = chain["child"]
            result.update(number=number, node=chain["childNode"], kind="pr",
                          url=f"https://github.com/{api.repository}/pull/{number}")
        pr = api.mapping(number)
        if pr["node_id"] != (chain["childNode"] or chain["node"]):
            raise ValueError("Handoff PR node changed.")
        if (pr["state"] not in {"open", "closed"} or type(pr.get("merged")) is not bool
                or pr["merged"] and (pr["state"] != "closed" or not pr.get("merged_at"))):
            raise ValueError("PR lifecycle/merge evidence unavailable.")
        if pr["merged"]:
            issue_pr.timestamp(pr["merged_at"])
        from pilot_github import managed
        origin = None
        if chain["kind"] == "issue":
            origin = api.api.get(f"{api.prefix}/issues/{chain['origin']}")
            if origin["node_id"] != chain["node"] or origin["number"] != chain["origin"]:
                raise ValueError("Mapped origin issue identity changed.")
        if "sourceWorkItem" in chain:
            import work_item_execution
            work_item_execution.source_guard(api, chain)
            active = True
        else:
            active = managed(origin) if origin is not None else managed(pr)
        result.update(number=number, node=pr["node_id"], head=pr["head"]["sha"], state=pr["state"],
                      kind="pr", managed=active and pr["state"] == "open"
                      and not any(label["name"] == "shepherd-hands-off" for label in pr["labels"]),
                      originManaged=active if origin is not None else None, merged=pr["merged"],
                      originAdopted=any(label["name"] == "shepherd-adopted" for label in origin["labels"])
                      if origin is not None else None,
                      originHandsOff=any(label["name"] == "shepherd-hands-off" for label in origin["labels"])
                      if origin is not None else None,
                      handsOff=any(label["name"] == "shepherd-hands-off" for label in pr["labels"]),
                      url=f"https://github.com/{api.repository}/pull/{number}")
        now = api.clock()
        if now < issue_pr.timestamp(record["progressAt"]):
            raise ValueError("Handoff observation clock rolled backwards; progress retained.")
        phase = "merged" if pr["merged"] else "closed" if pr["state"] == "closed" else (
            "watching" if record["confirmedAt"] is not None else "handoff_needed")
        if record["head"] != result["head"] or record["phase"] != phase:
            record.update(head=result["head"], progressAt=issue_pr.stamp(now))
        record["phase"] = phase
        if origin is not None and any(label["name"] == "quarantined-test" for label in origin["labels"]):
            # Closing keywords in commits can close an issue without a PR link.
            # Absence does not prove that GitHub's manual Development link is safe.
            # https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
            commits = api.api.pages(f"{api.prefix}/pulls/{number}/commits", identity_key="sha", max_bytes=1000000)
            body = pr.get("body")
            if (not isinstance(body, str) or type(pr.get("commits")) is not int
                    or not 0 < pr["commits"] <= 250 or len(commits) != pr["commits"] or any(
                    not isinstance(item.get("commit", {}).get("message"), str) for item in commits)):
                raise ValueError("Quarantine PR body/complete commit evidence unavailable; transfer unsupported.")
            texts = [body, *[item["commit"]["message"] for item in commits]]
            closing = any(re.search(r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?\s*(?:[\w.-]+/[\w.-]+)?#\d+",
                                    text, re.IGNORECASE) for text in texts)
            raise ValueError("Quarantine closure-capable keyword observed; human attention, no handoff."
                             if closing else "Quarantine manual closure associations unverified; transfer unsupported.")
        record["attention"] = None
    except (ValueError, KeyError, TypeError, IncompleteInventory) as error:
        record["attention"] = str(error)[:1000]
        if record["phase"] not in {"initial", "merged", "closed"}:
            record["phase"] = "handoff_pending"
        number = chain["child"] or chain["origin"]
        result.update(number=number, node=chain["childNode"] or chain["node"],
                      kind="pr" if chain["child"] or chain["kind"] == "pr" else "issue",
                      url=f"https://github.com/{api.repository}/"
                      + ("pull/" if chain["child"] or chain["kind"] == "pr" else "issues/") + str(number))
        result["attention"] = record["attention"]
        print(f"CI Shepherd handoff #{number} attention-needed: {record['attention']}", file=sys.stderr)
    return result


def reject_unsent(api, chain, operation, *, sent=False):
    """A definitive pre-POST rejection settles admission, never refunds billing."""
    import pilot_state as state
    latest = api.read_authority()
    current = state.find_chain(latest, chain["origin"])
    if current is None or not converted(current):
        return False
    saved = next((item for item in current["operations"] if item["id"] == operation["id"]), None)
    if saved is None or saved["taskId"] is not None or saved["state"] not in ({"reserved", "sent"} if sent else {"reserved"}):
        return False
    # Rebase only this definitive admission receipt on the freshly read transfer;
    # do not overwrite its ownership, mapping or frozen accounting with stale data.
    api.expected, api.ledger = deepcopy(latest), deepcopy(latest)
    saved = next(item for item in state.find_chain(api.ledger, chain["origin"])["operations"]
                 if item["id"] == operation["id"])
    saved["state"] = "no-send"
    api.persist()
    return True


def initial_packet(api, run, now, observations):
    import pilot_state as state
    for chain in api.ledger["chains"]:
        value = chain.get("handoff")
        if value is None or value["phase"] != "initial" or value["sendState"] != "idle":
            continue
        observed = observations[chain["origin"]]
        if not observed["actionable"] or not observed["managed"]:
            continue
        if state.worker_slots(api.ledger) >= 2:
            api.admission_reasons[chain["id"]] = "Tracking authority worker capacity exhausted; no inference."
            continue
        packet = {"schemaVersion": 1, "kind": "pilot", "packetId": str(uuid.uuid4()), "run": deepcopy(run),
                  "chain": chain["id"], "operation": value["id"], "preparedAt": issue_pr.stamp(now),
                  "observation": deepcopy(observed), "lane": "cloud", "context": None,
                  "target": api.binding.name, "handoffInitial": True}
        if len(json.dumps(packet, ensure_ascii=True, allow_nan=False).encode()) > contracts.MAX_JSON_BYTES:
            raise ValueError("Initial handoff packet exceeds bound; no inference.")
        value["sendState"] = "prepared"
        api.persist()
        return packet
    return None


def initial_guard(api, chain, observed):
    import pilot_state as state
    from pilot_github import fingerprint
    if (chain["kind"] != "issue" or chain["child"] is not None
            or chain["handoff"]["phase"] != "initial" or chain["handoff"]["taskId"] is not None
            or chain["handoff"]["sendState"] not in {"prepared", "sent"}):
        raise ValueError("Initial issue implementation no longer eligible.")
    api.adoption_effect_guard()
    fresh = api.observe_initial(chain)
    if (not fresh["managed"] or fresh["state"] != "open"
            or fingerprint(fresh) != fingerprint(observed)):
        raise ValueError("Initial issue basis/management changed.")
    # A task can resume after the sweep released its slot, even when its PR
    # artifact is missing. Refresh terminal capacity receipts before each POST
    # guard without treating them as PR mapping or ownership evidence.
    refresh_capacity(api)
    api.authority_guard()
    # Prepared/sent already reserves this worker's slot, including the final
    # pre-POST check. Do not charge its own admission a second time.
    if state.worker_slots(api.ledger) > 2:
        raise ValueError("Tracking authority worker capacity exhausted; no initial worker.")
    return fresh


def worker_request(api, chain, packet):
    from pilot_github import CORRELATION
    observed = packet["observation"]
    # Use plain, nonclosing references even for mitigation PRs; the controller
    # verifies platform artifacts, not a worker's narrative linking claim.
    correlation = {"chain": chain["id"], "operation": chain["handoff"]["id"], "origin": chain["origin"]}
    prompt = CORRELATION + json.dumps(correlation, separators=(",", ":")) + "\n" + prompt_templates.load(
        "initial-handoff",
        repository=str(api.repository),
        issue=str(chain['origin']),
        authority_repository=str(api.repository),
        tracker=str(api.tracker),
        authority=str(api.authority_id),
        chain=str(chain['id']),
        handoff=str(chain['handoff']['id']),
        evidence_json=json.dumps(observed, ensure_ascii=True, allow_nan=False),
    )
    body = {"prompt": prompt, "base_ref": "main", "create_pull_request": True}
    if len(json.dumps(body, ensure_ascii=True).encode()) > 20000:
        raise ValueError("Initial issue worker request exceeds bound; no worker.")
    return body


def settle_initial(api, chain, packet, evidence, now, *, disabled=False):
    import pilot
    import reasoning
    record = chain["handoff"]
    if record["id"] != packet["operation"] or record["sendState"] != "prepared":
        return {"outcome": "replay; no initial worker"}
    try:
        if disabled or evidence is None:
            raise ValueError("Initial decision disabled or unavailable; human attention, no retry.")
        decision, _ = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
        if not issue_pr.timestamp(packet["preparedAt"]) <= now < issue_pr.timestamp(packet["preparedAt"]) + timedelta(minutes=10):
            raise ValueError("Initial issue packet expired.")
        pilot.validate_decision(packet, decision)
        if decision["action"] != "cloud":
            raise ValueError("Initial qualification requires human attention; no implementation was dispatched.")
        body = worker_request(api, chain, packet)
        initial_guard(api, chain, packet["observation"])
        record["sendState"] = "sent"
        api.persist()
        try:
            initial_guard(api, chain, packet["observation"])
        except (ValueError, IncompleteInventory):
            record["sendState"] = "human"
            raise
        try:
            response = api.transport("POST", f"agents/repos/{api.repository}/tasks", body)
            if (not isinstance(response, Response) or response.status != 201
                    or not isinstance(response.payload, dict)
                    or not isinstance(response.payload.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", response.payload["id"])):
                raise LostResponse("Initial send outcome unknown.")
        except LostResponse:
            record.update(sendState="uncertain", attention="Initial task send outcome unknown; never redispatch.")
            api.persist()
            return {"outcome": "uncertain"}
        record.update(taskId=response.payload["id"], sendState="known", attention=None)
        api.persist()
        return {"outcome": "waiting", "taskId": record["taskId"]}
    except (ValueError, KeyError, TypeError) as error:
        from pilot_github import AuthorityUncertain
        if isinstance(error, AuthorityUncertain):
            raise
        record.update(sendState="human", attention=str(error)[:1000])
        api.persist()
        return {"outcome": "human", "error": str(error)}


def monitor_guard(api, chain, observation):
    api.authority_guard()
    fresh = api.observe(chain)
    keys = ("number", "node", "head", "state", "managed", "originManaged", "originAdopted",
            "originHandsOff", "handsOff", "attention", "merged")
    if any(fresh.get(key) != observation.get(key) for key in keys):
        raise ValueError("Handoff lifecycle/management evidence changed.")
    return fresh


def app_identity(api, chain, attempt_id, intent_head):
    # Repository, PR, handoff and source assignment stay in their canonical
    # outer records. The bridge never creates a second ownership ledger.
    return {"repository": api.repository, "pr": chain["child"] or chain["origin"],
            "node": chain["childNode"] or chain["node"], "chainId": chain["id"],
            "handoffId": chain["handoff"]["id"], "sourceWorkItem": chain.get("sourceWorkItem"),
            "attemptId": attempt_id, "intentHead": intent_head}


def app_basis(api, chain, head=None, *, effect=False):
    """Probe a copy: task refresh/observe must not write failure compensation."""
    if effect:
        if not api.write:
            raise ValueError("App checkpoints require the authorized local writer.")
        api.adoption_effect_guard()
    if chain["state"] != "open" or chain["handoff"]["phase"] not in {
            "handoff_pending", "handoff_needed", "watching"}:
        raise ValueError("Transferred chain is not open for app ownership.")
    ledger, write = api.ledger, api.write
    try:
        api.ledger, api.write = deepcopy(ledger), False
        import pilot_state as state
        probe = state.find_chain(api.ledger, chain["origin"])
        quiescent(api, probe)
        if (any(op["nativeReserved"] or op["workerReserved"] for op in probe["operations"])
                or any(review["reserved"] for review in probe.get("reviews", []))):
            raise ValueError("Owned credit/admission holds unresolved; reconciliation required.")
        if "sourceWorkItem" in probe:
            import work_item_execution
            record, assignment, saved = work_item_execution.source_guard(api, probe)
            if (record["assignments"][-1]["id"] != assignment["id"]
                    or assignment["revision"] != record["control"]["revision"]):
                raise ValueError("Imported source assignment/revision changed; reconciliation required.")
            if saved["worker_reserved"]:
                raise ValueError("Imported worker billing hold unresolved; reconciliation required.")
            if any(receipt["item"] == record["id"]
                   and (receipt["nativeReserved"] or receipt["state"] == "reserved")
                   for receipt in api.ledger.get("normalRuns", [])):
                raise ValueError("Imported source native hold unresolved; reconciliation required.")
        observed = observe(api, probe)
        if (observed["attention"] or not observed["managed"] or observed["state"] != "open"
                or observed["kind"] != "pr" or observed["merged"]
                or head is not None and observed["head"] != head):
            raise ValueError("App handoff PR/head/source/management/quiescence changed.")
        # Re-read both mapping and authority after the task/source inventory.
        monitor_guard(api, probe, observed)
        return observed
    finally:
        api.ledger, api.write = ledger, write


def pending_handoffs(api, number=None):
    """Enumerate only canonical transferred chains; never discover or enroll."""
    api.read_authority()
    entries = []
    for chain in api.ledger["chains"]:
        if not converted(chain) or number is not None and number not in {chain["origin"], chain["child"]}:
            continue
        transfer = chain["handoff"].get("appTransfer")
        entry = {"identity": app_identity(api, chain, transfer["attemptId"] if transfer else None,
                                         transfer["intentHead"] if transfer else None),
                 "head": None, "stage": transfer["stage"] if transfer else None,
                 "sessionId": transfer["sessionId"] if transfer else None,
                 "confirmedHead": transfer["confirmedHead"] if transfer else None,
                 "disposition": "reconciliation" if transfer else "actionable", "reason": None}
        try:
            observed = app_basis(api, chain)
            entry["head"] = observed["head"]
            if transfer and transfer["confirmedHead"] == observed["head"]:
                entry["reason"] = "Confirmed receipt; read native current settings, never replay activation."
            elif transfer:
                entry["reason"] = "Resume this attempt through native inventory/settings; never blindly reopen or re-enable."
        except (ValueError, KeyError, TypeError, IncompleteInventory) as error:
            entry.update(disposition="attention", reason=str(error)[:1000])
        entries.append(entry)
    api.authority_guard()
    return {"outcome": "canonical handoffs; read-only", "entries": entries}


def app_input(api, value, *, stage=None):
    keys = {"identity", "head"}
    if stage != "intent":
        keys.add("session")
    if stage in {None, "activation-requested"}:
        keys.add("settings")
    contracts.exact(value, keys, "app bridge input")
    identity = contracts.exact(value["identity"], {
        "repository", "pr", "node", "chainId", "handoffId", "sourceWorkItem", "attemptId", "intentHead"},
        "app immutable intent identity")
    if not isinstance(identity["attemptId"], str) or str(uuid.UUID(identity["attemptId"])) != identity["attemptId"]:
        raise ValueError("invalid app attempt identity")
    app_head(identity["intentHead"])
    app_head(value["head"])
    issue_pr.positive(identity["pr"], "app PR")
    import pilot_state as state
    api.read_authority()
    chain = state.find_chain(api.ledger, identity["pr"])
    if (chain is None or not converted(chain)
            or identity != app_identity(api, chain, identity["attemptId"], identity["intentHead"])):
        raise ValueError("App intent canonical repository/PR/node/handoff/source binding mismatch.")
    transfer = chain["handoff"].get("appTransfer")
    if transfer is not None and (
            transfer["attemptId"] != identity["attemptId"] or transfer["intentHead"] != identity["intentHead"]):
        raise ValueError("Conflicting immutable app attempt; reconcile without new effects.")
    if stage != "intent":
        session = contracts.exact(value["session"], {"sessionId", "repository", "pr", "activity"},
                                  "native session association attestation")
        app_identifier(session["sessionId"], "native app session")
        if (session["repository"] != api.repository or session["pr"] != identity["pr"]
                or type(session["pr"]) is not int
                or not isinstance(session["activity"], str)
                or session["activity"] not in ({"idle", "busy"} if stage is None else {"idle"})):
            raise ValueError("Exact safe native PR session association/activity required.")
        if transfer is None or transfer["sessionId"] not in {None, session["sessionId"]}:
            raise ValueError("Missing intent or conflicting native app session; reconcile without effects.")
        for other in api.ledger["chains"]:
            saved = other.get("handoff", {}).get("appTransfer")
            if other is not chain and saved and saved["sessionId"] == session["sessionId"]:
                raise ValueError("Native app session already belongs to another handoff.")
    if "settings" in value:
        app_settings(value["settings"], enabled=stage is None)
    if stage is not None and value["head"] != identity["intentHead"]:
        raise ValueError("Head changed before activation; reconcile this attempt, do not reopen.")
    return chain, transfer


def app_checkpoint(api, stage, value):
    if stage not in APP_STAGES:
        raise ValueError("Unsupported fixed app checkpoint stage.")
    chain, transfer = app_input(api, value, stage=stage)
    app_basis(api, chain, value["head"], effect=True)
    before = deepcopy(chain["handoff"].get("appTransfer"))
    if stage == "intent":
        if transfer is None:
            if any(other.get("handoff", {}).get("appTransfer", {}).get("attemptId")
                   == value["identity"]["attemptId"] for other in api.ledger["chains"]):
                raise ValueError("App attempt already belongs to another handoff.")
            chain["handoff"]["appTransfer"] = {
                "attemptId": value["identity"]["attemptId"], "intentHead": value["head"],
                "stage": "intent", "sessionId": None, "activationSettings": None,
                "confirmedHead": None, "settings": None}
    elif stage == "session-bound":
        if transfer["stage"] == "intent":
            transfer.update(stage=stage, sessionId=value["session"]["sessionId"])
    elif transfer["stage"] == "session-bound":
        transfer.update(stage=stage, activationSettings=deepcopy(value["settings"]))
    elif transfer["stage"] != "activation-requested":
        raise ValueError("Bind the native session before the activation boundary.")
    api.persist()
    return {"outcome": "app checkpoint persisted; no native effects",
            "stage": chain["handoff"]["appTransfer"]["stage"],
            "replay": before == chain["handoff"]["appTransfer"]}


def confirm_app_handoff(api, value, now):
    chain, transfer = app_input(api, value)
    if transfer is None or transfer["stage"] != "activation-requested":
        raise ValueError("Durable activation boundary required before receipt confirmation.")
    app_basis(api, chain, value["head"], effect=True)
    record = chain["handoff"]
    if now < issue_pr.timestamp(record["progressAt"]):
        raise ValueError("App confirmation clock rolled backwards; progress retained.")
    # A tick can edit the head. Bind that fresh head to this same attempt/session,
    # not a new activation. Same-head replay preserves all reminder/time state.
    if transfer["confirmedHead"] != value["head"]:
        transfer.update(confirmedHead=value["head"], settings=deepcopy(value["settings"]))
        if record["confirmedAt"] is None or record["head"] != value["head"]:
            record.update(progressAt=issue_pr.stamp(now))
            chain.pop("reminder", None)
        if record["confirmedAt"] is None:
            record.update(confirmedAt=issue_pr.stamp(now))
        record.update(phase="watching", head=value["head"], attention=None)
    api.persist()
    return {"outcome": "authorized native bridge attestation confirmed; not independently app-authenticated",
            "pr": value["identity"]["pr"], "head": value["head"], "sessionId": transfer["sessionId"]}


def confirm(api, number, head, now, *, app_enabled, address_reviews, fix_ci, resolve_conflicts, merge_disabled):
    if (app_enabled is not True or address_reviews is not True or fix_ci is not True
            or resolve_conflicts is not True or merge_disabled is not True
            or not re.fullmatch(r"[0-9a-f]{40}", head)):
        raise ValueError("Explicit Agent Merge action, merging-OFF and exact head assertions required.")
    api.read_authority()
    import pilot_state as state
    chain = state.find_chain(api.ledger, number)
    if chain is None or not converted(chain) or number != (chain["child"] or chain["origin"]):
        raise ValueError("Exact transferred PR required.")
    value = chain["handoff"]
    if "appTransfer" in value:
        raise ValueError("Native app transfer cannot be mixed with legacy manual confirmation.")
    if value["phase"] not in {"handoff_needed", "watching"}:
        raise ValueError("Manual handoff is not ready for app ownership confirmation.")
    api.adoption_effect_guard()
    quiescent(api, chain)
    observed = observe(api, chain)
    if observed["attention"] or not observed["managed"] or observed["head"] != head or observed["state"] != "open":
        raise ValueError("Handoff PR/head/management/quiescence changed.")
    monitor_guard(api, chain, observed)
    if value["confirmedAt"] is None:
        value.update(phase="watching", confirmedAt=issue_pr.stamp(now), progressAt=issue_pr.stamp(now))
        chain.pop("reminder", None)
    api.persist()
    return {
        "outcome": "operator confirmed review, CI and conflict actions enabled with merging OFF; activation not API-verified",
        "pr": number, "head": head,
    }


def merged_label(api, chain, observation):
    value = chain["handoff"]
    if not api.write or chain["kind"] != "issue" or value["phase"] != "merged" or observation["attention"]:
        return
    origin = api.api.get(f"{api.prefix}/issues/{chain['origin']}")
    if origin["node_id"] != chain["node"] or origin["number"] != chain["origin"]:
        raise ValueError("Merged origin mapping changed.")
    names = [label["name"] for label in origin["labels"]]
    if "shepherd-adopted" not in names:
        value["mergeLabel"] = "confirmed"
        api.persist()
        return
    if value["mergeLabel"] != "none" or "shepherd-hands-off" in names:
        return
    api.adoption_effect_guard()
    monitor_guard(api, chain, observation)
    value["mergeLabel"] = "sent"
    api.persist()
    api.adoption_effect_guard()
    monitor_guard(api, chain, observation)
    try:
        response = api.transport("DELETE", f"{api.prefix}/issues/{chain['origin']}/labels/shepherd-adopted", None)
        if (not isinstance(response, Response) or response.status != 200
                or not isinstance(response.payload, list)
                or any(label.get("name") == "shepherd-adopted" for label in response.payload)):
            raise LostResponse("Post-merge adoption-label removal outcome unknown.")
        value["mergeLabel"] = "confirmed"
    except LostResponse as error:
        value["mergeLabel"] = "uncertain"
        print(f"CI Shepherd post-merge label requires verification: {error}", file=sys.stderr)
    api.persist()


def status(chain, observation):
    value = chain["handoff"]
    attention = value["attention"] or (
        "Enable Agent Merge manually in the app with `address_reviews`, `fix_ci` and "
        "`resolve_conflicts` ON, and `merge_pr` OFF; activation is not verified."
        if value["phase"] == "handoff_needed" else
        "Expected Agent Merge settings: `address_reviews`, `fix_ci` and `resolve_conflicts` ON, "
        "`merge_pr` OFF; settings are not API-verified. Monitor only."
        if value["phase"] == "watching" else "No PR repairs; observe lifecycle or unresolved owned work.")
    task = f"\nInitial task: {value['taskId']}" if value["taskId"] else ""
    receipt = f"\nPost-merge adoption-label receipt: {value['mergeLabel']}." if value["phase"] == "merged" else ""
    return (f"[automated] CI Shepherd manual handoff: {value['phase']}\n\n"
            f"Responsible human: @{value['responsible']}. {attention}\n"
            f"Evidence: {observation['url']}{task}{receipt}")
