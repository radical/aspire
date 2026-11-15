# CI-health reliability plan and audit register

Status: proposed extension to the unapplied recovery candidates. No deployment,
new notification channel, runtime limit, watchdog or takeover authority is
established by this document.

## Outcome and scope

The group should deliver correct fixes without routine human input, duplicate
effects or repeated work that cannot make progress. Preserve accepted work,
original evidence, branch targeting, independent assessment and human merge.
Update (owner request, November 15): the separate reviewer cron is replaced
in UNAPPLIED 3-role candidates. The fixer owns a fresh-context read-only
review child; later outcome verification moves to the monitor and missed-work
sampling to the supervisor, so no duty is orphaned. See the review record
below.

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

These dispositions separate changes deliverable now from unimplemented runtime
features. "Implement-now" includes preserving or clarifying an existing rule;
it does not assert that the whole failure mode is eliminated.

| Disposition | Findings and decisions |
| --- | --- |
| implement-now | F1, F3, F4 (preserve), F5 (outcome/settlement distinction only), F6, F7, F8, F10, F12, F13 (age/coordination only), F14 (ownership/reconciliation only), F17 (preserve), D1 (document), D4 |
| deferred-capability | F2 crash exercise, F9 host stop/time/token enforcement, F11 shared backoff, F15 notification transport, F16 external outage detector, D3 new cancellation/takeover authority |
| discuss | F18 changing report publication frequency, D2 exact notification route and permission |

Implemented items reduce risk; they do not fix missing controls or detection
in F2, F9, F11, F15 or F16. F18 remains unchanged in the live/candidate
publication contract during this iteration; proposed material-change
publication is not silently activated.

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
- A code-review subagent reduces review latency. Owners after the 3-role
  migration: post-merge verification and revisits → monitor; feedback
  consumption → fixer; missed work → supervisor (see the transfers table in
  [prompt-obligations.md](prompt-obligations.md)).

## Non-negotiable recovery and compatibility details

An authoritative result can establish one operation's outcome and allow its
existing result to be consumed. It does not permit adopting someone else's PR
or treating the executor as stopped. Fresh mutation of a claimed incident
still needs the existing verified settled checkpoint plus authoritative
release or confirmed server expiry. The alternative remains a pinned manager
decision, explicit owner/platform authority and independently verified
settlement evidence. F5 and D3 relax none of these requirements.

Use one group-wide `verifier/lease.json` for new reviewer publication, not a
new per-PR lock hierarchy. A reviewer encountering another unexpired holder
does not write protected records or poll; it can report the skip in its local
final response. Publication retries occur on a later eligible pass. Confirmed
server expiry permits reacquiring this artifact-publication lease with a
distinct nonce; it does not settle workers. Every protected artifact write by
new reviewers must carry the lease fence checked atomically with that write,
in addition to the target's compare-and-swap precondition. Reconcile previous
publication outcomes, current target ETags and conflicting verdicts first.
Do not claim that this prevents legacy reviewers from writing without fences.
Legacy writes remain evidence to reconcile, not permission to ignore blockers
or wait forever for old writers to produce new fields.

All new readers accept absent optional fields in legacy records, preserve
existing states and distinguish absence from false or resolved. New `parked`
semantics remain additive recovery-action metadata, not a replacement for
the existing top-level item/work lifecycle enums. New consumers must not
wait for fields legacy producers cannot publish. New-to-new deployment order
is therefore not a correctness dependency; mixed old consumers can still
repeat obsolete behavior and must be reported as adoption gaps. Never claim
an atomic rollout, forcibly drain sessions or retroactively change their rules.

Notification discovery is read-only. Manager attention tools are not proof
of automation access or an out-of-band alert. Verify each target's actual
selection, host support, recipient and delivery behavior before proposing
any addition. No added tools, triggers or grants are authorized by this plan.
Five admitted runs can multiply per-pass reads; a PR cap does not bound that
cost. Supervisor's omitted concurrency field remains unknown.

## Prompt size and preservation budget

The latest pre-expansion candidate checkpoint is `6f17c3dd0`. Code-point and
UTF-8 limits both matter; the existing worker emoji consumes extra bytes.

| Role | Candidate characters | Candidate UTF-8 bytes | Editing target |
| --- | --- | --- | --- |
| Monitor | 11639 | 11639 | at most 11400 where consolidation permits |
| Fixer | 11978 | 11981 | at most 11400 where consolidation permits |
| Reviewer | 11946 | 11946 | at most 11400 where consolidation permits |
| Supervisor | 11985 | 11985 | at most 11400 where consolidation permits |

After implementation (core v2), the rendered candidates measure: monitor
11901/11901, fixer 11987/11990, reviewer 11989/11989, supervisor 11907/11907
(after the GPT-6 Astra review fixes).
The per-family costs are in
[prompt-obligations.md](prompt-obligations.md#size-cost-by-change-family).

The hard limit is 12000; the smaller number is editing margin, not permission
to delete obligations. Replace redundant text instead of appending. Record
per-role before/after counts and the net cost of each coherent change family
(contention/verdicts, recovery/parking, compatibility and unchanged rules) in
the preservation map. Map every substantive cut to its equivalent retained
rule. If a required safeguard cannot fit, stop that target's deployment and
report the unmet requirement rather than dropping it or hiding it in a file
the runtime cannot read.

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

## Rollback and partial rollout

Keep exact authoritative predecessor prompts and their revision identities.
An observed new duplicate effect, false ready verdict, lost accepted work or
publication blocked by the new protocol requires immediate diagnosis against
source evidence. A worker's self-review alone is not proof of regression.
If a prompt regression is confirmed, propose restoring only affected targets
through the same owner-scoped prompt-only compare-and-swap path. Owner approval
of this rollout does not imply arbitrary runtime cancellation, settings
changes or removal of concurrent configuration updates. Reconcile current
generation/content and the original mutation outcome before rollback; require
the applicable approval if the platform returns a proposal.

Verify exact restored bytes and unchanged unrelated configuration. Record
partial rollback explicitly; already-admitted runs keep their pinned revision.
Do not revert the source history or erase the failed candidate/evidence. The
implementation must leave an exact restoration artifact ready even if no
rollback is needed.

## Acceptance and actual proof

Immediate completion means every finding above has an explicit disposition,
all supported edits preserve standalone constraints, the candidate source and
assembled text agree, review findings are resolved or explicitly retained, and
each live target is either verified applied or accurately identified as pending.
No unsupported runtime feature is a hidden prerequisite of the deployed prompts.
Verify the complete candidate source renders to the exact submitted body and
that authoritative readback matches its bytes/digest; character counts and
anchor matches alone are not equivalence checks.

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

- First expanded-plan audit: Opus 5.5 medium requested explicit dispositions,
  prompt budget, unchanged settlement requirements, reviewer lease scope,
  legacy-reader tolerance, rollback and read-only notification discovery.
  This revision incorporates those corrections and awaits re-audit.
- Audit corrections: a reviewer lease cannot constrain legacy writers, so
  expiry is not a blanket safety guarantee; new fenced artifact writes plus
  target CAS and reconciliation are required. Rollback decisions require
  source evidence rather than self-review claims. A recovered result does
  not authorize adoption of someone else's work.
- Plan re-audit: Opus 5.5 medium approved this plan at `14696ab02` with no
  remaining blockers.
- Candidate implementation: the implement-now dispositions are implemented as
  UNAPPLIED candidates. The changes:
  - the shared core is now RECOVERY CORE v2 (1,898 characters after review fixes);
  - the four sources and rendered candidates are updated;
  - the skill decision table and worked examples cover `fence_lost`, the
    missing-fence capability gap, mixed generations and absent optional
    fields;
  - the preservation map has 49 rows and 232 anchors, plus a disposition
    record, per-family size costs and a semantic-consolidation list.

  Rendered sizes (code points / UTF-8 bytes): monitor 11,901/11,901; fixer
  11,987/11,990; reviewer 11,989/11,989; supervisor 11,907/11,907. All are
  under the 12,000 hard limit and over the 11,400 target. F18 cadence, tools,
  configuration and schedules are unchanged. No hosted calls were made.
- GPT-6 Astra implementation review of `1ccb4a05e`: two deployment blockers
  and one checker defect. All three are fixed as UNAPPLIED candidates; the
  same reviewer's re-review is recorded below.

  | Finding | Severity | Disposition |
  | --- | --- | --- |
  | Fixer ready gate (`source/worker.md`) said "targeted validation and required checks" without requiring them to pass; other roles can't substitute for a standalone gate | blocker | fixed: "passing targeted validation and required checks" restored in the fixer ready step; map row anchors the exact gate |
  | Core 3 defined due only by retained deadlines, with no parked exception, so core 6, monitor two-pass escalation and reviewer missed-deadline sampling still forced new action on parked work | blocker | fixed: core 3 excludes parked actions from due/stalled until `reopenCondition` verifiably changes; core 6 excludes parked; monitor and reviewer two-pass clauses say "unparked"; core 7 keeps the cheap reopen check; core 2 keeps ages/deadlines/IDs. Supervisor overdue and fixer DUE clauses rely on the core-3 definition |
  | `check_prompts.py` silently skipped malformed obligation rows and could pass with zero anchors | defect | fixed: malformed rows, empty role cells, cells with neither anchor nor `n/a:` reason, an empty map and a map with zero anchors now fail. Exercised by negative cases (4-cell row, empty map, header-only, empty cell, unanchored prose cell, all-`n/a`) plus the normal full run. Still an authoring check only |

  Size offsets for the core growth (+46): wording-only abbreviations
  ("reproduction" → "repro", "directory" → "dir", article removals), the
  fixer parked hook now cites core 3, and the core uses "+" and
  "/awaited" in items 5 and 11. No safeguard was removed.
- GPT-6 Astra final re-review of `9095dd85c`: APPROVED. No unresolved
  blocker; all three findings are fixed; the normal checker run and seven
  negative cases are verified. It is safe for the documented limited prompt
  rollout; it is not runtime proof. The final reviewed commit is `9095dd85c`.
- Live rollout: BLOCKED before any mutation. The manager's monitor
  `automation_get` was aborted by the client tool, and the supervisor
  `automation_get` returned "no active client is registered to execute this
  client tool". No upsert, no proposal and no unknown mutation. Next: restore a
  connected hosted client, then a fresh get, prompt-only CAS upsert and
  readback per role. Candidate digests and rollback predecessors are in
  [prompts/README.md](prompts/README.md#candidate-review-and-deployment-status).
- Runtime recovery and external notification: not demonstrated.
- Reviewer-to-fixer migration (owner request after Opus 5.5 medium judgment:
  the separate review cron delays feedback, and circular audits/censuses are
  expensive). The reviewed 4-role set `9095dd85c` was never applied and is
  archived unchanged as NOT FOR ROLLOUT (`--historical` re-renders it). New
  UNAPPLIED 3-role candidates: fixer owns exact-head child review under
  `worker/reviews/<pr>/<head>/` and the ready gate; monitor owns
  `planner/outcomes/` and failure rates; supervisor owns readiness-handoff
  checks, one due blocked item per pass and a daily missed-work sample, with
  no audit-of-audit. Checker: exit 0, 54 rows, 27 reviewer
  transfers (4 retired); sizes 11,984/11,996/11,981 bytes. A narrow pre-deployment fix moved
  reviews to per-`<reviewId>` subdirectories (fresh same-head review after
  settlement), consumes native results immediately and adds the fixer's
  boundary checkpoint. A later owner-authorized supervisor STALE rule
  handles 12h/24h idle or unresponsive sessions (evidence first; stop/close
  only through an exposed authorized control, else record the missing control
  once and park; no such control is in today's catalog), and the fixer ready
  gate again verifies maintainer requests. Sizes now 11,984/11,995/11,993; 212
  anchors. Not yet
  independently reviewed; deployment and the reviewer retirement sequence are
  pending (hosted `automation_get`/list aborted again at 18:32 UTC) and
  recorded in [prompts/README.md](prompts/README.md#deployment-and-reviewer-retirement-status).
- Rejected suggestions (judge): `NuGetAudit=false`, a cap-exempt ready PR, a
  manager clearing a claim because its head is idle or unchanged, and
  replacing a timed-out child. NOT approved and unsafe; see
  [findings.md](findings.md).
