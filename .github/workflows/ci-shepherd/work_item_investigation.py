"""Consistency checks on worker-reported comparable-run evidence, not attestation."""

import round as contracts
import work_items as items


CONTEXT = ("workflow_file", "branch", "lane", "job", "test")


def receipt(value):
    contracts.exact(value, {"run_id", "run_attempt", "head_sha", "created_at", "executed", "conclusion", *CONTEXT},
                    "worker run evidence")
    for key in ("run_id", "run_attempt"):
        items.issue_pr.positive(value[key], key)
    items.sha(value["head_sha"])
    items.issue_pr.timestamp(value["created_at"])
    items.boolean(value["executed"], "test executed")
    for key in CONTEXT:
        items.text(value[key], key, 500)
    items.issue_pr.choice(value["conclusion"], {"passed", "failed", "pending", "skipped", "unknown"},
                          "worker run conclusion")


def validate(value):
    contracts.exact(value, {"pinned", "latest", "last_passing", "compare"}, "investigation")
    receipt(value["pinned"])
    for key in ("latest", "last_passing"):
        if value[key] is not None:
            receipt(value[key])
    compared = value["compare"]
    if compared is not None:
        contracts.exact(compared, {"base_sha", "head_sha", "commits"}, "worker commit comparison")
        items.sha(compared["base_sha"])
        items.sha(compared["head_sha"])
        items.texts(compared["commits"], "candidate commits")
        for commit in compared["commits"]:
            items.sha(commit)
        if not passing_baseline(value):
            raise ValueError("comparison requires an executed comparable passing baseline")
        if (compared["base_sha"] != value["last_passing"]["head_sha"]
                or compared["head_sha"] != value["pinned"]["head_sha"]):
            raise ValueError("comparison SHA mismatch")


def comparable(first, second):
    return all(first[key] == second[key] for key in CONTEXT)


def passing_baseline(value):
    before, pinned = value["last_passing"], value["pinned"]
    return (before is not None and comparable(before, pinned) and before["executed"]
            and before["conclusion"] == "passed"
            and items.issue_pr.timestamp(before["created_at"]) < items.issue_pr.timestamp(pinned["created_at"]))


def status(value):
    validate(value)
    pinned, latest = value["pinned"], value["latest"]
    if latest is None or not comparable(pinned, latest) or not latest["executed"]:
        return "unknown"
    latest_at, pinned_at = map(items.issue_pr.timestamp, (latest["created_at"], pinned["created_at"]))
    if latest_at < pinned_at or latest["run_id"] == pinned["run_id"] and latest["run_attempt"] < pinned["run_attempt"]:
        return "unknown"
    if latest["conclusion"] == "failed":
        return "current_unresolved_failure"
    newer = latest_at > pinned_at or (
        latest["run_id"] == pinned["run_id"] and latest["run_attempt"] > pinned["run_attempt"])
    if newer and latest["conclusion"] == "passed":
        return "later_matching_pass"
    return "unknown"
