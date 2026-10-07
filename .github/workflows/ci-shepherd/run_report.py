"""Human-readable local receipts, never authority or repair input."""

import html
from pilot_results import reported_claim


def text(value):
    # Render identifiers/reasons as one inert Markdown line, not embedded markup.
    value = html.escape(" ".join(str(value).split()), quote=False)
    for character in "\\`*_[]#":
        value = value.replace(character, "\\" + character)
    return value[:600]


def accounting(ledger):
    actual = reserved = 0
    unknown = False
    for chain in ledger["chains"]:
        for operation in chain["operations"]:
            actual += sum(operation[key] or 0 for key in ("nativeActual", "workerActual"))
            reserved += operation["nativeReserved"] + operation["workerReserved"]
            unknown |= operation["nativeReserved"] > 0 or operation["workerReserved"] > 0 or operation["nativeActual"] is None or (
                operation["taskId"] is not None and operation["workerActual"] is None)
        for review in chain.get("reviews", []):
            actual += review["actual"] or 0
            reserved += review["reserved"]
            unknown |= review["actual"] is None
    return actual, reserved, unknown


def render(run, target, before, after, result, packet, started, ended):
    actual, reserved, unknown = accounting(after)
    previous_actual, _, _ = accounting(before)
    rounds = sum(chain["rounds"] for chain in after["chains"])
    previous_rounds = sum(chain["rounds"] for chain in before["chains"])
    lines = [
        "# CI Shepherd run report", "",
        f"Outcome: **{text(result['outcome'])}**",
        f"UTC: {text(started)} to {text(ended)}. Target: {text(target)}.",
        f"Run: `{text(run['runId'])}`; source: `{text(run['workflowSha'])}`.", "",
        "## What happened", "",
        f"- New action rounds: {rounds - previous_rounds}.",
    ]
    if result.get("error"):
        lines.append(f"- Controller error: {text(result['error'])}.")
    if result.get("until"):
        lines.append(f"- Deferred until {text(result['until'])}; reassess fresh evidence then.")
    if packet:
        observed = packet["observation"]
        descriptor = "head" if observed["kind"] == "pr" else "issue content state"
        lines.append(f"- Decision subject: {observed['kind']} #{observed['number']}, "
                     f"{descriptor} `{text(observed['head'])}`.")
        basis = (f"{len(observed['feedback'])} remaining feedback items"
                 if observed["kind"] == "pr" else "initial issue request or remaining feedback")
        lines.append(f"- Why a decision was admitted: {basis}; "
                     f"managed: {observed.get('managed', 'unknown')}; "
                     f"CI pending: {observed.get('pendingCI', 'unknown')}; "
                     f"ready: {observed.get('ready', 'unknown')}.")
        ids = ", ".join(text(item["id"]) for item in observed["feedback"])
        lines.append(f"- Decision input: {len(observed['feedback'])} feedback items"
                     + (f" ({ids[:1200]})." if ids else "; initial issue request."))
        operation = next((operation for chain in after["chains"] for operation in chain["operations"]
                          if operation["id"] == packet.get("operation")), None)
        decisions = operation.get("feedbackDecisions", {}) if operation else {}
        for item in observed["feedback"][:10]:
            disposition = decisions.get(item["id"], "input only")
            if disposition == "addressed":
                disposition = "repair requested; not verified resolved"
            summary = text(item["body"])
            summary = summary[:180] + "..." if len(summary) > 180 else summary
            lines.append(f"- {summary} ({text(disposition)}).")
        if len(observed["feedback"]) > 10:
            lines.append("- Additional feedback is retained in packet.json.")
    reasons = {value["chain"]: value["reason"] for value in result.get("reasons", [])}
    old_chains = {chain["id"]: chain for chain in before["chains"]}
    new_tasks = 0
    for chain in after["chains"]:
        old = old_chains.get(chain["id"], {"operations": [], "reviews": []})
        old_operations = {operation["id"]: operation for operation in old["operations"]}
        number = chain["child"] or chain["origin"]
        kind = "pull" if chain["child"] or chain["kind"] == "pr" else "issues"
        url = f"https://github.com/{after['repository']}/{kind}/{number}"
        lines.extend(["", f"### [{after['repository']}#{number}]({url})", "",
                      f"- Chain: {text(chain['state'])}; lifetime rounds: {chain['rounds']}."])
        if old.get("state") is not None and old["state"] != chain["state"]:
            lines.append(f"- Chain state: {text(old['state'])} -> {text(chain['state'])}.")
        if chain["id"] in reasons:
            lines.append(f"- No new decision/task for this chain: {text(reasons[chain['id']])}")
        handoff = chain.get("handoff")
        if handoff:
            lines.append(f"- Manual handoff: {text(handoff['phase'])}; responsible: @{text(handoff['responsible'])}; "
                         f"initial send: {text(handoff['sendState'])}. No PR repairs.")
            if handoff["attention"]:
                lines.append(f"- Handoff attention: {text(handoff['attention'])}.")
            if handoff["taskId"]:
                task = handoff["taskId"]
                task_link = f"[{text(task)}](https://github.com/{after['repository']}/tasks/{task})"
                if task != old.get("handoff", {}).get("taskId"):
                    new_tasks += 1
                    lines.append(f"- New saved initial implementation task: {task_link}; not proof of a fix.")
                else:
                    lines.append(f"- Saved initial implementation task: {task_link}.")
        for operation in chain["operations"]:
            previous = old_operations.get(operation["id"], {})
            task = operation["taskId"]
            task_link = f"[{text(task)}](https://github.com/{after['repository']}/tasks/{task})" if task else None
            if task and task != previous.get("taskId"):
                new_tasks += 1
                label = ("New observed repair task ID; persistence is unconfirmed"
                         if result["outcome"] == "failed" else "Task scheduled; New saved repair task")
                lines.append(f"- {label}: {task_link}.")
                lines.append("- Why a task was initiated: native decision authorized cloud repair "
                             "and the host accepted the guarded send; repair success is not yet verified.")
            if task and operation["workerState"] != previous.get("workerState"):
                lines.append(f"- Task state: {text(previous.get('workerState') or 'not observed')} -> "
                             f"{text(operation['workerState'] or 'unknown')}; {task_link}.")
                if operation["workerState"] == "in_progress":
                    lines.append(f"- Task started (observed): {task_link}.")
                elif operation["workerState"] == "completed":
                    lines.append(f"- Found completed task: {task_link}; fix/readiness still unverified.")
            if previous and operation["state"] != previous["state"]:
                lines.append(f"- Operation state: {text(previous['state'])} -> {text(operation['state'])}.")
            if task and (operation is chain["operations"][-1] or operation != previous):
                lines.append(f"- Worker `{text(task)}`: {text(operation['workerState'] or 'unknown')}; "
                             f"operation: {text(operation['state'])}.")
            elif operation != previous:
                lines.append(f"- Native operation: {text(operation['state'])}; no saved task ID.")
            if operation.get("wait") and operation != previous:
                lines.append(f"- Wait: {text(operation['wait']['until'])}; {text(operation['wait']['reason'])}")
            if task and operation.get("result"):
                receipt = operation["result"]
                if "version" not in receipt:
                    lines.append("- Result: incomplete; authority capacity exhausted. "
                                 "Matching saved attempt held; billing remains independent.")
                    continue
                lines.append(f"- Result: {text(receipt['status'])}; {text(receipt['summary'])}.")
                lines.append(f"- Result collection: {receipt['attempts']}/3 attempts; "
                             f"{text(receipt['reason'])}. Publication: {text(receipt['publication'])}; "
                             f"comment: {text(receipt['commentId'] or 'none (preview or unknown)')}.")
                if receipt.get("workerReport"):
                    lines.append("- Untrusted claim: " + reported_claim(receipt).strip())
        old_reviews = {record["id"]: record for record in old.get("reviews", [])}
        for review in chain.get("reviews", []):
            if review != old_reviews.get(review["id"]):
                label = "New Copilot review intent" if review["id"] not in old_reviews else "Copilot review update"
                lines.append(f"- {label}: {text(review['state'])}, head `{text(review['head'])}`.")
        reminder = chain.get("reminder")
        if reminder and reminder != old.get("reminder"):
            receipt = (f" ([comment]({url}#issuecomment-{reminder['commentId']}))"
                       if reminder["commentId"] is not None else "; no confirmed comment receipt")
            lines.append(f"- Reminder: {text(reminder['sendState'])}{receipt}.")
        if chain.get("childAdoption") != old.get("childAdoption") and chain.get("childAdoption") != "none":
            lines.append(f"- Child adoption: {text(chain['childAdoption'])}.")
        if chain["statusId"] != old.get("statusId") or chain["statusPending"] != old.get("statusPending", False):
            lines.append(f"- Presentation receipt: {text(chain['statusId'])}; "
                         f"pending: {chain['statusPending']}.")
    if not new_tasks:
        lines.extend(["", "No new saved repair task. An uncertain outcome does not prove no send occurred."])
    if any("handoff" in chain for chain in after["chains"]):
        lines.extend(["", "Legacy accounting below excludes transferred work. "
                      "Initial qualification/implementation usage is not collected here; "
                      "this is not a zero-spend claim. Unresolved frozen legacy reservations remain admission holds."])
    lines.extend([
        "", "## Accounting and limits", "",
        f"- Newly recorded credits: {actual - previous_actual:g}; cumulative known credits: {actual:g}.",
        f"- Outstanding reservations: {reserved:g}; billing: {'unknown amounts remain' if unknown else 'known reported amounts'}.",
        "- Newly recorded usage may settle earlier work; it is not necessarily this run's spending.",
        "- Reservations are admission holds, not actual charges or hard spending caps.", "",
        "## Still unverified", "",
        "Worker/task completion is not proof of a fix or green CI. This report does not assert a patch, "
        "merge or readiness without separate current-head evidence.",
        "Local snapshots are not authority. Failed or uncertain writes require reconciliation; "
        "do not replay actions from this report.", "",
    ])
    return "\n".join(lines)
