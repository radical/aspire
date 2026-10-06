"""Closed target selection; controller execution and authority stay in the fork."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Binding:
    name: str
    repository: str
    repository_id: int
    subject: int | None
    round_limit: int


FORK = Binding("fork", "radical/aspire", 746880239, None, 10)
UPSTREAM = Binding("upstream-20722", "microsoft/aspire", 696529789, 20722, 10)
UPSTREAM_ALL = Binding("upstream", "microsoft/aspire", 696529789, None, 10)


def select(name="fork", event="workflow_dispatch"):
    if name == "fork" or event == "schedule":
        return FORK
    if event == "workflow_dispatch":
        for binding in (UPSTREAM, UPSTREAM_ALL):
            if name == binding.name:
                return binding
    raise ValueError("unsupported/manual-only pilot binding")


TRIAL_HEAD = "ae3b3a7d56449c4fe9e1ab81fc361dd687e15603"


def policy(binding):
    if binding == FORK:
        return "Do not modify workflows or permissions."
    return (
        "Investigate and repair one bounded batch of ordinary current-PR CI or review feedback. "
        "The shared repair-scope policy limits code changes to the adopted request and regressions "
        "caused by it; unrelated failures and suspected flakes do not authorize repairs. "
        "Failed job names alone are not a diagnosis or unsupported scope. Unknown failures require "
        "worker diagnosis of logs, artifacts and annotations before changing code. Repair only a "
        "verified cause; report a concrete human-only blocker when one exists. Infrastructure or "
        "cancellation-only failures require waiting/rerun, not an artificial code change or human handoff. "
        "For intentional generated workflow changes use pinned gh-aw v0.89.17. "
        "Never edit labeler workflows, change authentication, broaden permissions or weaken tests. "
        "Preserve the existing action-lock policy baseline; never bypass action-pin restrictions.")


def brief(binding, head):
    if binding != UPSTREAM or head != TRIAL_HEAD:
        return None
    return {
        "head": TRIAL_HEAD,
        "validation": "Run 37211582230 compiled eight workflows with gh-aw v0.89.17; "
                      "cleanliness failed on M .github/workflows/agentics-maintenance-microsoft-aspire.dev.yml.",
        "zizmor": "Check 111464970425: polyglot-validation.yml:246 unsoundconditionalexpression; "
                  "condition always evaluates true.",
    }
