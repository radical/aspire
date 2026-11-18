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

These item and PR limits do not change run concurrency. The latest direct
capacity read reports one Worker run at a time; do not claim that three
parallel Worker runs are configured or raise concurrency through an
unsupported field.
