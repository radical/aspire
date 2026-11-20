#!/usr/bin/env python3
"""Advisory fast gate for the Takeoff CIF Worker.

The Worker pays session startup on every cron tick. Most ticks find nothing it
can do, yet it used to spend minutes re-deriving that from scratch. This helper
turns state the Worker has already read into one of three answers:

  EXIT       Positive proof that nothing is due and nothing is admissible.
  WORK       Something is actionable now; take the normal path.
  RECONCILE  State is missing, partial or ambiguous; take the normal path.

Only EXIT short-circuits the Worker. WORK and RECONCILE both mean "run the
normal prompt path", so a wrong WORK/RECONCILE costs time, never correctness.
Every rule therefore errs away from EXIT. The helper never mutates state,
dispatches runs, reads the network or decides ownership; the prompt's safety
rules stay authoritative.

Subcommands (JSON on stdin, JSON on stdout):

  gate  Decide EXIT / WORK / RECONCILE at the start of a run.
  wake  After a verified release of this Worker's own slot, decide whether to
        request one fresh Worker run for a waiting P0, and derive its stable
        request key from the release identity.

Exit status is 0 whenever a decision was printed. Any other status, or output
that does not parse, must be treated by the caller as RECONCILE / no wake.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid

MAX_ACTIVE = 3
MAX_P0_ACTIVE = 2
QUOTA_RESERVE = 500

# Fixed namespace so the same release always derives the same request key,
# which lets an uncertain dispatch be retried with an identical key.
WAKE_NAMESPACE = uuid.UUID("5f0c1c0e-8f0e-4c55-9b55-2b1f0d6a4c11")

REQUIRED_READY_LABELS = {"cif", "cif:ready"}
TERMINAL_HANDOFF_STATES = {"done", "closed", "released"}


class InvalidInput(ValueError):
    pass


def parse_time(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise InvalidInput(f"{field} must be an ISO-8601 UTC string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidInput(f"{field} is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise InvalidInput(f"{field} has no timezone: {value!r}")
    return parsed


def require(container: dict, key: str, kind: type, where: str):
    value = container.get(key)
    if not isinstance(value, kind):
        raise InvalidInput(f"{where}.{key} must be {kind.__name__}")
    return value


def section(state: dict, key: str) -> dict:
    """Return a section that its reader marked complete, or raise.

    `complete` is the caller's assertion that every page was read and no read
    failed. A partial read is never evidence of absence.
    """
    value = require(state, key, dict, "input")
    if value.get("complete") is not True:
        raise InvalidInput(f"{key} is not marked complete")
    return value


def issue_number(item: dict, where: str) -> int:
    number = item.get("issue", item.get("number"))
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise InvalidInput(f"{where} has no valid issue number")
    return number


def active_slots(slots: dict) -> list[dict]:
    active = require(slots, "active", list, "slots")
    seen: set[int] = set()
    for index, entry in enumerate(active):
        if not isinstance(entry, dict):
            raise InvalidInput(f"slots.active[{index}] must be an object")
        number = issue_number(entry, f"slots.active[{index}]")
        if number in seen:
            # Two slots for one item is conflicting ownership, not capacity.
            raise InvalidInput(f"issue {number} holds more than one slot")
        seen.add(number)
        if entry.get("priority") not in ("P0", "P1"):
            raise InvalidInput(f"slots.active[{index}].priority must be P0 or P1")
    if len(active) > MAX_ACTIVE or sum(e["priority"] == "P0" for e in active) > MAX_P0_ACTIVE:
        raise InvalidInput("slots exceed configured limits")
    return active


def ready_candidates(ready: dict, owned: set[int]) -> tuple[list[int], list[int]]:
    issues = require(ready, "issues", list, "ready")
    p0: list[int] = []
    p1: list[int] = []
    for index, item in enumerate(issues):
        if not isinstance(item, dict):
            raise InvalidInput(f"ready.issues[{index}] must be an object")
        number = issue_number(item, f"ready.issues[{index}]")
        labels = require(item, "labels", list, f"ready.issues[{index}]")
        if not REQUIRED_READY_LABELS.issubset(labels) or number in owned:
            continue
        (p0 if "cif:p0" in labels else p1).append(number)
    return sorted(p0), sorted(p1)


def decide_gate(state: dict) -> dict:
    now = parse_time(state.get("now"), "now")

    # Every inbox page must be drained. Anything other than a bare cron tick
    # (label event, PR review/check/close, comment, dispatch) or any pending
    # feedback is actionable, whatever PR or issue the trigger names.
    inbox = section(state, "runInputs")
    items = require(inbox, "items", list, "runInputs")
    if any(not isinstance(i, dict) or i.get("kind") != "cron" for i in items):
        return result("WORK", "run has non-cron input")
    pending = inbox.get("pendingFeedback")
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise InvalidInput("runInputs.pendingFeedback must be a non-negative integer")
    if pending:
        return result("WORK", "pending feedback")

    # Low or unknown quota is not "nothing to do": the normal path saves a
    # continuation or applies caps conservatively.
    quota = require(state, "quota", dict, "input")
    for key in ("restRemaining", "graphqlRemaining"):
        remaining = quota.get(key)
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            raise InvalidInput(f"quota.{key} must be an integer")
        if remaining < QUOTA_RESERVE:
            return result("RECONCILE", f"quota.{key} below reserve")

    slots = section(state, "slots")
    active = active_slots(slots)

    # Handoffs are the existing authoritative record of owned items, external
    # waits, clean yields and unresolved effects. The caller must supply all
    # of them; there is deliberately no separate wake index.
    handoffs = section(state, "handoffs")
    records = require(handoffs, "items", list, "handoffs")
    owned: set[int] = {issue_number(e, "slots.active[]") for e in active}
    for index, record in enumerate(records):
        where = f"handoffs.items[{index}]"
        if not isinstance(record, dict):
            raise InvalidInput(f"{where} must be an object")
        number = issue_number(record, where)
        status = record.get("state")
        if not isinstance(status, str):
            raise InvalidInput(f"{where}.state must be a string")
        if status in TERMINAL_HANDOFF_STATES:
            continue
        owned.add(number)
        if record.get("unresolvedEffects") is not False:
            return result("WORK", f"issue {number} has unresolved or unknown effects")
        due = record.get("dueAt")
        if due is None:
            # A live item with no recorded next check is unexplained: only the
            # normal path can decide whether it is stalled, waiting or done.
            return result("RECONCILE", f"issue {number} has no recorded dueAt")
        if parse_time(due, f"{where}.dueAt") <= now:
            return result("WORK", f"issue {number} is due")

    p0_waiting, p1_waiting = ready_candidates(section(state, "ready"), owned)
    p0_active = sum(e["priority"] == "P0" for e in active)
    has_room = len(active) < MAX_ACTIVE
    if p0_waiting:
        if has_room and p0_active < MAX_P0_ACTIVE:
            return result("WORK", "P0 waiting with capacity", p0_waiting)
        # The normal path owns P0 pressure recording and P1 yielding, so a
        # waiting P0 without capacity is never a fast exit.
        return result("RECONCILE", "P0 waiting without capacity", p0_waiting)
    if p1_waiting and has_room:
        return result("WORK", "P1 waiting with capacity", p1_waiting)
    return result("EXIT", "no due, unresolved or admissible work")


def decide_wake(state: dict) -> dict:
    """Decide whether a Worker that just released its own slot should wake one
    fresh Worker for a waiting P0.

    The trigger is an attributable release, never free capacity alone. The key
    derives only from the release identity, so a retry after an uncertain
    dispatch reuses it, and a different release never collides with it.
    """
    release = require(state, "release", dict, "input")
    if release.get("verified") is not True:
        return no_wake("release not verified")
    run_id = require(release, "runId", str, "release")
    nonce = require(release, "claimNonce", str, "release")
    etag = require(release, "releasedSlotEtag", str, "release")
    source = issue_number(release, "release")
    if not (run_id and nonce and etag):
        raise InvalidInput("release identity fields must be non-empty")

    active = active_slots(section(state, "slotsAfterRelease"))
    owned = {issue_number(e, "slotsAfterRelease.active[]") for e in active}
    if source in owned:
        return no_wake("source issue still holds a slot")
    handoffs = section(state, "handoffs")
    for index, record in enumerate(require(handoffs, "items", list, "handoffs")):
        if isinstance(record, dict) and record.get("state") not in TERMINAL_HANDOFF_STATES:
            owned.add(issue_number(record, f"handoffs.items[{index}]"))

    p0_waiting, _ = ready_candidates(section(state, "ready"), owned | {source})
    if not p0_waiting:
        return no_wake("no other P0 waiting")
    p0_active = sum(e["priority"] == "P0" for e in active)
    if len(active) >= MAX_ACTIVE or p0_active >= MAX_P0_ACTIVE:
        return no_wake("no visible P0 capacity")

    key = uuid.uuid5(WAKE_NAMESPACE, f"release:{run_id}:{nonce}:{etag}")
    return {
        "wake": True,
        "requestId": str(key),
        "reason": "verified release and P0 waiting with capacity",
        # Informational only: the woken Worker re-derives admission itself.
        "waitingP0": p0_waiting,
    }


def result(decision: str, reason: str, candidates: list[int] | None = None) -> dict:
    output = {"decision": decision, "reason": reason}
    if candidates:
        output["candidates"] = candidates
    return output


def no_wake(reason: str) -> dict:
    return {"wake": False, "reason": reason}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("gate", "wake"):
        print("usage: worker_gate.py gate|wake < state.json", file=sys.stderr)
        return 2
    command = argv[1]
    try:
        state = json.load(sys.stdin)
        if not isinstance(state, dict):
            raise InvalidInput("input must be a JSON object")
        output = decide_gate(state) if command == "gate" else decide_wake(state)
    except (InvalidInput, json.JSONDecodeError) as error:
        output = (result("RECONCILE", f"invalid input: {error}") if command == "gate"
                  else no_wake(f"invalid input: {error}"))
    json.dump(output, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
