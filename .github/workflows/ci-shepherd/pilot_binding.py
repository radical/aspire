"""Closed target selection; controller execution and authority stay in the fork."""

from dataclasses import dataclass

import prompt_templates


@dataclass(frozen=True)
class Binding:
    name: str
    repository: str
    repository_id: int
    subject: int | None
    round_limit: int
    base_ref: str = "main"

    def __post_init__(self):
        if self.base_ref != "main" and not (
                self.name == "fork-merge-proof" and self.repository == "radical/aspire"
                and self.repository_id == 746880239 and self.subject == 139
                and self.base_ref == "fork-merge-proof-base-18213f39"):
            raise ValueError("unsupported isolated fork base binding")


FORK = Binding("fork", "radical/aspire", 746880239, None, 10)
FORK_MERGE_PROOF = Binding("fork-merge-proof", "radical/aspire", 746880239, 139, 10,
                           "fork-merge-proof-base-18213f39")
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
        return prompt_templates.load("target-fork")
    return (
        prompt_templates.load("target-upstream"))


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
