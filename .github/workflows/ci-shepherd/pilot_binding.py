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
UPSTREAM = Binding("upstream-20722", "microsoft/aspire", 696529789, 20722, 5)


def select(name="fork", event="workflow_dispatch"):
    if name == "fork" or event == "schedule":
        return FORK
    if name == UPSTREAM.name and event == "workflow_dispatch":
        return UPSTREAM
    raise ValueError("unsupported/manual-only pilot binding")


TRIAL_HEAD = "ae3b3a7d56449c4fe9e1ab81fc361dd687e15603"


def policy(binding):
    if binding == FORK:
        return "Do not modify workflows or permissions."
    return (
        "Only repair bounded workflow repository guards/always-true conditionals and their "
        "source-derived generated updates using pinned gh-aw v0.89.17. "
        "Never edit labeler workflows, change authentication, broaden permissions or weaken tests. "
        "Preserve the existing action-lock policy baseline.")


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
