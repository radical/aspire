# CI-health reliability plan and audit register

Status: proposed extension to the unapplied recovery candidates. No deployment,
new notification channel, runtime limit, watchdog or takeover authority is
established by this document.

## Outcome and scope

The group should deliver correct fixes without routine human input, duplicate
effects or repeated work that cannot make progress. Preserve accepted work,
original evidence, branch targeting, independent assessment and human merge.
Keep the existing four automations for this change. Moving per-PR code review
into a fixer subagent is a separate design option, not an approved removal of
later outcome verification or missed-work assessment.

This register incorporates the owner's supplied read-only audit of commit
`d87f87fe8ac8b6a5851862e24be3a1791f6e9c14`. Its F/D identifiers are retained
for traceability, not as runtime terms. The audit examined saved prompts, not
live behavior. Its assertion of safety "by construction" is too strong:
artifact compare-and-swap does not atomically protect GitHub side effects,
prompt limits are not enforced execution limits, and expired claims do not
prove a writer stopped.

## Verified configuration and remaining unknowns

Manager reads on 2026-11-15 confirmed these live definitions before this change:

| Role | Generation | UTC cron | Returned maximum concurrent runs | Responsibility |
| --- | --- | --- | --- | --- |
| Monitor | 24 | `*/15 * * * *` | 5 | repository |
| Fixer | 22 | `7,37 * * * *` | 5 | pull request |
| Reviewer | 19 | `17,47 * * * *` | 5 | repository |
| Supervisor | 17 | `27,57 * * * *` | not returned; unknown | none |

All four were enabled with the separate execution gate enabled. GitHub event
readiness was blocked by missing repository administration access; this is
separate from cron. These are settings, not a live execution inventory or a
claim that five copies run concurrently. Item responsibilities can reuse an
owner session; scheduling and responsibility are not execution exclusion.

Actual termination, total elapsed-work budgets, aggregate token quotas and
remote command controls remain unknown or unavailable. Do not infer them from
lease TTLs, persisted lifecycle flags or the absence of a returned field.
New prompts apply prospectively; already-admitted sessions retain old rules.

## Findings, suggestions and disposition

| Audit | Problem or suggestion | Planned disposition and evidence needed |
| --- | --- | --- |
| F1 | Unspecified behavior when another holder owns the lease | Add explicit no-protected-writes/no-polling exit for role leases. For workers, skip only the contested incident. Distinguish conflict from unknown outcome and reconcile the latter. |
| F2 | Monitor dies while holding its lease | Preserve intent/outcome reconciliation and expose evidence age. Do not shorten TTL or infer termination. Real crash exercise requires an authorized disposable execution. |
| F3 | Old and new monitor overlap during a GitHub write | State residual race explicitly. Recheck ownership/lease immediately before effects; preserve unknown intent and reconcile actual body. Do not promise one edit globally. |
| F4 | Workers select the same incident | Retain create-only distinct-holder claim and rechecks. Verify contenders do not treat another holder as their own; no broad claim migration. |
| F5 | Dead worker leaves an unsettled claim | Recover authoritative outcomes without demanding one dead-writer receipt. Preserve separate evidence of executor settlement. No automatic takeover based on expiry, unchanged head or absent intent. Missing controls stay a specific unresolved action. |
| F6 | Slow worker is mistaken for dead | Meaningful result milestones, not age alone. No kill or replacement authorization. Continue nonconflicting work. |
| F7 | Optional reviewer exclusion and conflicting verdicts | Require reviewer publication lease in new prompts, define contention and current-head conflict handling. Historical or concurrent contradictory verdicts need explicit evidence-backed supersession, not silent last-writer pass. A new commit is not required merely to correct a review. |
| F8 | Reviewer and supervisor repeat each other's assessment | Attribute evidence and responsibility; preserve independent missed-work sampling. Do not treat separate models or role names as proof of independence. |
| F9 | Agent loops, hangs or spends too long | Add progress-based checks, bounded unchanged methods and checkpoints. Host-enforced stop/time/token controls are not implemented by prose. Do not invent a final-settlement deadline a hung process cannot meet. |
| F10 | An incident cannot progress | Explicit parked action, stable age/identity, cheap observable reopen check and preserved unfinished work. Do not automatically abandon it or reissue the same escalation every pass. |
| F11 | Parallel instances multiply reads and hit rate limits | Document actual returned limits and distinction from total cost. Retain Retry-After/checkpoint behavior. Shared backoff or changed concurrency needs a verified shared contract/configuration decision; do not introduce a second coordination ledger speculatively. |
| F12 | Old/new prompt semantics overlap | Add compatibility notes and exact deployment attribution. A new mandatory lease cannot bind old sessions. Do not claim atomic four-role rollout or drain old executions without authority. |
| F13 | Diagnostic reservation stays occupied | Preserve expected result deadline and age, inspect original child/create/send/result evidence. An unavailable child is not absent; no expiry-only reset. Record missing exact reset/termination path. |
| F14 | PR slot reserved before uncertain creation | Reconcile original creation and ownership before reclaim. Identify worker ownership of the slot and supervisor responsibility for overdue coordination; preserve unresolved reservations. |
| F15 | A visible request does not notify anyone | Keep an explicit notification-transport gap. Discover a supported owner-facing channel before proposing its exact permission/recipient/deduplication policy. Do not silently permit new public comments or call issue-body publication a notification. |
| F16 | All group automations stop | Specify an external freshness monitor as a separate deliverable requiring an independent execution/failure domain, accessible source and real alert route. Another member on the same failed platform is insufficient. No new workflow or recurring run in this change without an approved operational design. |
| F17 | Untrusted text can redirect an agent | Preserve evidence-not-authority rule and least required actions. Actual configured tools can exceed prompt duties; do not claim permissions enforce all role boundaries. No hostile-input experiment on production credentials. |
| F18 | Every assessment rewrites the public report | Prefer material-change publication plus a bounded freshness signal only if the complete report contract is preserved. Make freshness and last material change distinct; no fake current timestamp when evidence is old. Account for report-writing races. |

## Decisions and unresolved capability work

| Audit | Decision | Default for this implementation |
| --- | --- | --- |
| D1 | Same-role concurrency | Preserve saved configuration; document verified settings and write safe contention behavior for queued or overlapping runs. Do not guess supervisor's missing limit. |
| D2 | Human push route | Do not expand public posting permissions. Record the missing recipient/transport/grant and a bounded proposal after capability discovery. Needs-you visibility alone must not be advertised as an out-of-band push. |
| D3 | Observation-based crash recovery | Accept authoritative outcome evidence, not a supervisor signature as a substitute for settlement. Preserve existing manager/owner authority boundaries. New exact cancellation or takeover requires a supported route and explicit authority. |
| D4 | Reviewer exclusion | Tighten new reviewer publication with a mandatory lease and explicit verdict conflict handling; keep immutable verdict history. Mixed-generation enforcement remains unknown until old writers are accounted for. |

Additional suggestions to retain:

- Current pointers are discovery hints, not the inventory of all accepted work.
- Keep common recovery rules in one canonical skill core, embedded in each
  deployed prompt. Fork-only files cannot be a required runtime dependency.
- Keep essential authorization, storage boundaries, branch/backport policy,
  evidence quality and input handling in every standalone prompt that needs them.
- Keep schemas additive and bounded, with recent attempts inline and immutable
  history separate. A compatibility table documents readers; it does not grant
  authority to reject or erase legacy work.
- Clarify `manager/current.json` versus `manager/requests/current.json` through
  existing authorized producer evidence. Until verified, treat each as an
  optional exact legacy input, not a required new writer or schema.
- A code-review subagent can reduce review latency. Someone must still own
  post-merge verification, feedback consumption, missed work and future revisits.
  Do not transfer those duties in this recovery rollout.

## Implementation sequence

1. Finish the already-started candidate corrections without deploying them.
   Preserve exact live `prompts/current/` and baseline snapshots.
2. Have an Opus 5.5 medium session audit this plan, including safety, missing
   acceptance criteria, excess machinery and changes requiring new authority.
   Record each finding and its disposition here; iterate unresolved substantive
   findings with that session.
3. Have Opus 5.5 medium implement the supported prompt/documentation changes,
   preserving every standalone obligation. Update candidates, canonical core,
   source templates and the preservation map together.
4. Run the local authoring checker and inspect exact assembled prompts. Check
   size, assembly and obligations; do not describe anchor presence as behavioral
   coverage or enforcement. Avoid runtime helpers unavailable to the automations.
5. Have a fresh GPT-6 Astra session review the actual candidate/source diff,
   full old/new obligations, compatibility and deployment plan. Send findings
   privately to the implementer. Fix and re-review substantive issues.
6. Commit/push the tested candidate sources and records to the existing fork
   branch. Freshly read each live owner-scoped automation immediately before
   a prompt-only compare-and-swap update. Reconcile drift; no blind overwrite.
7. Read back exact applied prompt and unchanged configuration. Archive the exact
   predecessor, then update `current/` only for applied revisions. If the server
   requires approval, retain candidate status and exact proposal ID; pending is
   not deployed. Never raise autonomy or switch APIs to evade that gate.
8. Observe naturally admitted executions for adoption and useful results.
   No manual production dispatch, cancellation or fault injection is included.

## Acceptance and actual proof

Immediate completion means every finding above has an explicit disposition,
all supported edits preserve standalone constraints, the candidate source and
assembled text agree, review findings are resolved or explicitly retained, and
each live target is either verified applied or accurately identified as pending.
No unsupported runtime feature is a hidden prerequisite of the deployed prompts.

Evaluate the first seven covered days after adoption against all eligible
overdue actions, including unselected work and old-generation consumers:
time to useful resumption, repeated unchanged recovery attempts, duplicate
effects, unresolved control gaps and missed accepted work. Separate genuine
external waits from execution stalls. Establish a pre-change baseline from
available source evidence; if unavailable, report descriptive results and
coverage, not an invented improvement percentage.

Retain the external audit's proposed behavioral exercises as unexecuted:
concurrent monitors/workers/reviewers; crashes before pointer publication,
during report edits and around PR creation; full reserved PR capacity;
rate-limit responses; mixed-generation consumers; total scheduler outage; and
hostile input. Each requires an approved disposable setup, verified tools and
an observable effect/alert. Local prompt checks cannot prove these outcomes.

## Review and rollout record

- Plan: pending Opus 5.5 medium audit.
- Candidate implementation: earlier recovery work exists, unapplied; expanded
  scope waits for the plan audit.
- GPT-6 Astra implementation review: not started.
- Live rollout: not started.
- Runtime recovery and external notification: not demonstrated.
