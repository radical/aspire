# Scout additional instructions

These instructions supplement the Scout prompt for `microsoft/aspire`.
They do not expand its scope, permissions, or approval boundaries.
If they conflict with the saved prompt, follow the saved prompt.

## Turn workflow failures into actionable issues

1. **Start with the workflow run, not the tracker.** For a failure on `main` or the latest tracked release branch, find the recurring tracker for that workflow and branch. Look through its recent comments for the run link or commit SHA, then verify the exact run, attempt, and failed job in Actions. A tracker comment can be late, missing, or shared by multiple attempts. Do not delay an actionable failure just because the tracker has not caught up. Leave the tracker itself alone.
2. **Search broadly, match precisely, reuse first.** For every verified actionable workflow failure, query each known relevant issue label independently: `ci-failure-cause`, `ci-automation-broken`, `automation-broken`, `main-ci-break`, `cif`, and other relevant failure labels learned from the repository. These are OR searches, never an AND requirement. Also search without labels using verified test/error text, run links and affected branch/workflow/job/scope. Missing a producer label does not exclude an existing matching issue. Match actual signature and scope, not title alone; keep distinct failures distinct and inspect active fixes/closed history. Record query coverage and cursors. Incomplete or ambiguous searches remain pending, never a claim that no match exists.
   Reuse a matching eligible dedicated issue. For an unmatched failure, prepare the exact public title/body, with source evidence and acceptance criteria, for explicit owner approval through #22302. Use the repository's failing-test format for a named test and `[release/x.y]` prefix for release work; omit internal tool details. The general instruction to open issues does NOT waive exact-draft approval. Save the draft/version/search evidence; do not file while approval is pending. After approval of that exact text, refresh matching/ownership/pause checks and reconcile old filing intents before a single create. Unknown create outcome must be reconciled, never blindly retried.
   Apply `cif` and `cif:p0` to eligible matching/new workflow-failure issues under their claims and verified handoffs, with `cif:ready` last. Do not require a producer label. Preserve other labels, human pauses/closures, active external ownership and unresolved writes; defer an unsafe item without blocking unrelated work.
3. **Rolling first, even before other P0.** Main/latest-release push/scheduled and Outerloop/Quarantine breaks are checked and acted on ahead of other work, including other workflow-failure P0 items. Any unresolved rolling break is **P0**, including flakes; its tracking issues, fixes, mitigations, quarantine/disable PRs and release backports inherit P0. Other actionable workflow failures are P0 too, never ahead of rolling. Verified recovered transient-only failures are recorded without fixes. Skipped cases cannot clear a break; passing retries never erase attempt one.
4. **Close only confirmed duplicates, if authorized.** Keep the issue already being worked on; otherwise keep the oldest matching open issue. Preserve any unique evidence on the canonical issue and link it before closing an unowned, confirmed duplicate. Do not close the recurring tracker or an issue covering a different cause or additional scope. If ownership or equivalence is unclear, report it instead of closing it.

## Reuse evidence and publish safely

- Generate JSON once and measure its UTF-8 bytes before publication: the head is at most 28,672 bytes with 4,096 bytes reserved for the next transition; evidence shards are at most 16,384 bytes. Publish unchanged bytes only through an authorized supported path that preserves the required lease fence and matching ETag compare-and-swap on replacement. Pin readback to the committed ETag and verify bytes before advancing. Never transcribe generated bulk JSON, replace the head without its fence, or retry a denied path. If blocked, leave the head and history intact; write independent small records only when separately safe, and report the blocked record, exact continuation, and unresolved effect. No migration without approval. Use the trusted server session ID in receipts, or record it as unknown.
- First inspect the last five eligible rolling runs per branch/workflow and save their current/first-attempt results, red/green/unknown state and linked issue/PR. Then continue intake within the 30-minute budget. Leave seven-day rate computation to Supervisor; persist continuation for incomplete coverage. Page by creation time with a two-hour overlap, refresh pending runs, and reconcile late retries. Reuse only verified provider/workflow/run/attempt evidence; receipts and coarse signatures cannot establish missing jobs or causes. Approval gates stay separate. Never cache mutable eligibility, ownership or PR heads.
- Keep evidence completeness separate from severity: an observed unresolved rolling failure is P0 even before root cause is known. Missing evidence alone is unknown, not a failure or a pass.
- CI Insights is optional and was unavailable in the audited session. If it becomes available, use it only for streams and attempts it demonstrably covers, behind its reported refresh watermark. The roughly 15-minute refresh interval is an expectation, not a measured guarantee. Use live GitHub for newer, missing, incomplete, or contradictory records and before decisions that depend on current state. Missing index data is never green; no CI Insights dependency or grant change is implied.

## Private decision report (schema v1)

Each run writes one private report so the owner can see what Scout considered,
what it decided and why it stopped. It is evidence, not proof of success.
Reuse what the run already verified; do not spend extra GitHub requests on it.

- Tag `cif`. Name `scout/reports/<runId>.json`, where `<runId>` is the
  64-hex automation run ID. Create-only and immutable; never rewrite it. Size
  <=16384 UTF-8 bytes measured before the write; verify by readback.
- Then update `scout/reports/latest.json`, a pointer <=1024 bytes:
  `{"schemaVersion":1,"runId","name","status","finishedAt"}`. Create it
  if absent, otherwise CAS with the read ETag. Advance it only when this
  report's `finishedAt` is newer. On conflict reread; never overwrite a newer
  pointer. Fence both writes with the Scout lease.
- Report fields:
  - `schemaVersion`: 1. `runId`; `sessionId` or null when unknown;
    `automationId`; `revisionId`; `generation` (string).
  - `reconstructed`: false for a live run. A report rebuilt later sets true,
    adds `reconstruction` `{source,by,at,omissions[]}`, and states what is
    unknown. Never invent details.
  - `startedAt`, `finishedAt`: UTC ISO-8601.
  - `status`: `completed` only when every planned scope was examined;
    `partial` when stopped early with saved continuation; `blocked` when a
    precondition (lease, ownership, access, storage) prevented the planned
    scan; `failed` for an unexpected tool or run error.
  - `coverage`: `{summary, scanned[], omitted[]}` in plain language. Name
    every branch/workflow/label/search actually covered and each omission.
  - `items[]`: one entry per candidate actually examined this run:
    `{kind, title, url, priority, decision, reason, evidence[]?, issueUrl?,
    prUrls[]?, nextAction?}`.
    - `kind`: `rolling_run`, `workflow_failure`, `issue`, `pr`, `search` or
      `draft`.
    - `priority`: `P0`..`P3` or `unknown`.
    - `decision`: `matched`, `admitted`, `draft_pending_approval`,
      `duplicate`, `no_action`, `deferred`, `unverified`, `pending` or
      `tracked_transient`.
    - `reason`: one or two human-readable sentences with the concrete fact
      that drove the decision. No internal tool names.
    - Group identical-signature failures in one item only when every grouped
      source run/job is listed in `evidence`.
  - `truncated`: `{omittedItems, note}` when the size cap forces dropping
    items; drop lowest-priority items first and keep counts. Absent otherwise.
  - `stop`: `{reason, detail, continuation?}`. `reason` is one of
    `exhausted` (normal end of planned scope), `budget` (time or request
    budget), `rate_limit`, `evidence_unavailable`, `lease_or_ownership`,
    `storage`, `tool_failure` or `other`. `continuation` names the
    remaining scope in plain language.
- Never list an item you did not examine. Unexamined scope belongs in
  `coverage.omitted` or `stop.continuation`.
- If either write fails or its outcome is unknown, record the exact error and
  the unpublished name in the end log `blockers`; do not retry blindly, delete,
  or move the report elsewhere. Reconcile an unknown outcome by reading the
  name first.
- Reports are private. Never copy them or tool details into public issues or
  PRs.
