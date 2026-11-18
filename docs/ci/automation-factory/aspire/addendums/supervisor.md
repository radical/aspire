# Supervisor additional instructions

These instructions supplement the Supervisor prompt for `microsoft/aspire`.
They do not expand its scope, permissions, or approval boundaries.
If they conflict with the saved prompt, follow the saved prompt.

## Owner-approved queue policy

At most three items may be actively worked, with no more than two P0 items.
Prioritize actionable P0 work, but do not reserve an unused slot. Waiting on
CI, reproduction, PR review, a person, or external access does not consume an
active-work slot. Separately, keep the coordinator-owned PR review queue at no
more than three non-P0 and ten P0 open PRs awaiting review. Drafts are allowed;
do not mark a PR ready above its applicable limit.

Scout has a 30-minute wall-clock budget for each intake run. It must persist
continuation and carry undisposed candidates forward rather than mark a partial
scan complete. This is an instruction-level budget, not an enforced runtime
timeout.

Audit rolling checks before issue intake on every Scout run, including
scheduled Outerloop and Quarantine. An unresolved rolling break, its work
issues and all fix/mitigation/quarantine/disable PRs and release backports
inherit P0 regardless of flaky classification. Keep the last five eligible
runs per branch/workflow, red/green/unknown, first/current attempts and linked
issue/PR in #22302 and the repository recovery checkpoint. Missing provider,
workflow or branch coverage is unknown; a green retry does not erase attempt
one. Retain displaced history separately and preserve unresolved ownership.

Audit failure-to-issue intake across workflows, not only rolling: every known
relevant label queried independently, including `automation-broken`, plus
label-free searches by verified signature/scope. Reuse matching issues even
without producer labels; eligible workflow-failure issues get `cif`/`cif:p0`.
Rolling P0 stays ahead of other P0. Incomplete/ambiguous search remains pending;
unmatched failures require exact title/body approval before filing. No issue
creation, closure or ownership override follows from this evaluation alone.

These item and PR limits do not change run concurrency. The latest direct
capacity read reports one Worker run at a time; do not claim that three
parallel Worker runs are configured or raise concurrency through an
unsupported field.
