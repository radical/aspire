# PR human reminder policy

The deterministic policy in `pilot_reminders.py` extends the existing
serialized reminder boundary. It does not start agents, request reviews,
change labels, approve workflows or merge. Only `radical` is mentioned.

## Configuration

PR reminders are opt-in for both local and hosted controllers:

```shell
CI_SHEPHERD_PR_REMINDERS=true
CI_SHEPHERD_REMINDER_DELAY_SECONDS=120
CI_SHEPHERD_REMINDER_REPEAT_SECONDS=300
```

The pilot waits two minutes after observing an unchanged eligible episode and
five minutes between confirmed notices. Both timers accept whole seconds
from 1 through 86400. Existing blocker reminders retain their one-shot
behavior when the PR policy is disabled.

## Ready for human action

Require complete fresh current-head CI evidence, including optional checks,
with no failures or pending work; known conflict-free mergeability; and
resolved review threads with no outstanding changes-requested review.
An approval must be a human approval on the current head, with no renewed
request for that reviewer.

Draft status, missing approval, `NO-MERGE` and pending review requests are
disclosed as holds in an **awaiting human review** notice, never as merge-ready.
Blocked merge requirements and a branch behind its base are also explicit
holds. Formal readiness requires GitHub's final merge state to be `clean`,
not merely a conflict-free head and one approval. Unknown final readiness
holds all readiness notices. Without holds the notice says **ready for human
merge/review action**.
Neither notice authorizes merging.

## Stuck or human input required

Explicit human decisions, workflow approval and verified owned workers waiting
for user input qualify for human attention. Failed, cancelled or timed-out
owned workers qualify only against a matching current-head attempt basis
while that CI/review/conflict blocker remains. Historical failed workers
cannot explain a different current-head failure.
Two verified terminal same-head repair attempts matching the current CI
blocker with CI still red also
qualify. A successful terminal worker alone is not a reason to notify.
Ordinary pending CI and active/resumed workers suppress stale notices.
Every saved task and session is freshly verified. Mixed terminal/active
evidence or an input task without a verified input session holds notification.

GitHub Copilot timeline events remain descriptive. They do not establish
owned tasks or prove desktop Agent Merge failure. A recent unpaired start
suppresses a notice conservatively; absence of a start is not proof of idle
desktop work. Notices explicitly disclose that desktop activity is not
observable by this controller. An overdue re-review is a review hold, not an
inferred worker failure.

## Episode and send safety

Source-head changes, real human comments/reviews, thread resolution,
CI/blocker transitions and verified worker-state changes reset the episode.
Owned status, worker-report and reminder comments do not restart the timer.
The episode stores a compact CRC32 fingerprint of canonical UTF-8 activity,
not raw feedback bodies. Existing API inventory bounds apply. The fingerprint
detects inactivity changes, not authorization or tampering; fresh management,
head and policy guards independently authorize every send.
Unknown/read-incomplete evidence preserves receipts and never authorizes a
notice. Terminal PR outcomes and human takeover suppress future notices.

Before every POST, refresh management, head, CI, reviews and saved task
receipts. Persist the unique send identity and timestamp first, then check
again. Recover a lost response only from one exact owned comment receipt.
Without a unique receipt, never retry that send. Each confirmed unchanged
episode may repeat after its cooldown with a new persisted send identity.
The cooldown starts at confirmation, including receipt recovery, so network
latency cannot shorten the gap between published notices.

A comment receipt proves publication, not GitHub inbox or email delivery.
The local writer is `radical`; a self-mention does not establish that GitHub
notified the same account. Delivery must be reported separately.
