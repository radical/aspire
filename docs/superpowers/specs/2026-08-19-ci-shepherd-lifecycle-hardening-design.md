# CI Shepherd Lifecycle Hardening Design

## Summary

The CI shepherd will stop asking an assessment agent to rediscover lifecycle
state from the complete evidence snapshot. A deterministic preparation stage
will classify each issue producer, summarize its authoritative occurrence
ledger, identify safety blockers, propose a conservative candidate action, and
create a bounded issue-scoped evidence bundle. The assessment agent may confirm
or downgrade that candidate, but never upgrade it.

This iteration remains read-only. It may recommend human closure review, but it
will not comment, ping, close, reopen, edit, label, or assign GitHub issues.

## Producer Contracts

The collector must identify the producer before interpreting lifecycle data.

| Producer | Identity | Occurrence ledger | Closure policy |
|---|---|---|---|
| `ci-failure-cause` | `<!-- ci-failure-cause:... -->` | Body `Occurrences` table | Missing `autoclose` means human closure only |
| `tracking-issue` | Known tracking marker in the issue body | Failure comments carrying `<!-- run:N -->` | Respect `autoclose:true`, `false`, or missing |
| `ci-health-dashboard` | `_Filed from the CI Health dashboard._` footer | No deterministic ledger currently available | Human review only |
| `unknown` | No recognized producer contract | None | Data-quality queue; no automated action |

Producer-specific canonical rules remain authoritative. The shepherd must not
invent one cross-producer canonical-selection rule.

## Ledger Completeness

Every issue receives a ledger summary:

```json
{
  "source": "body-table",
  "schema": "occurrences-v2",
  "schemaRecognized": true,
  "sourceRecordCount": 1,
  "parsedRowCount": 1,
  "complete": true,
  "rows": []
}
```

`complete` requires a recognized schema, at least one source record, and every
source record parsing successfully. Empty or unrecognized ledgers are blockers,
never evidence that recurrence did not happen.

The body-table parser accepts the two observed Aspire schemas:

- `Date | Build | Job | PR`
- `Date | Build | Branch | Job | Triggering merge`

The tracking-issue parser reads run markers from comments. Comment collection
must succeed before that ledger can be complete.

## Episode Awareness

The snapshot records whether close/reopen episodes are complete. When timeline
collection is disabled, the synthetic current episode remains useful context
but `episodesComplete` is `false`.

No future write action may rely on a synthetic episode. A write-enabled phase
must collect timelines for selected candidates and key comment identity by
`(issueNumber, episodeOrdinal)`.

## Deterministic Assessment Preparation

A new preparation stage consumes the full immutable snapshot and emits a small
assessment document. Each issue entry contains:

- Producer and autoclose policy.
- Tiered identity.
- Ledger completeness and latest occurrence.
- Episode completeness.
- Candidate lifecycle state and candidate action.
- Automation eligibility and approval requirement.
- Explicit blockers and missing prerequisites.
- A bundle of at most 25 evidence records.
- A code-generated completeness proof summarizing excluded scoped evidence.

The preparation stage scans all scoped evidence for blockers before pruning the
bundle. The assessment agent therefore does not need to enumerate hundreds of
source-path or ownership records.

## Candidate Authority

The candidate engine is conservative:

- Unknown or unparsed producers become `insufficient-evidence`.
- `autoclose:true` trackers remain `observing`; the existing watchdog owns
  closure.
- `autoclose:false` or missing policy is never automation-eligible.
- Recurrent complete ledgers become `actionable` / `investigate`.
- A merged fix with a commit-anchored successful run may become
  `resolved` / `recommend-close`, but always requires human approval in this
  iteration.
- Old single-occurrence issues may be routed for closure review, but age alone
  cannot satisfy resolution.
- Probable semantic duplicates remain approval-gated.

`recommend-close` is deliberately advisory and is not a high-risk executable
action. Existing `close-*` actions remain high risk and are disallowed by the
assessment candidate contract for this iteration.

The assessment agent receives an explicit `allowedActions` list. Validation
rejects any action outside it. This makes the agent a confirmer or veto, not an
authority escalation point.

## Operational Queues

Markdown queues are derived deterministically from validated decisions:

- Approval needed
- Investigate and post findings
- Ping human
- Waiting
- Unchanged
- Data-quality blockers

Queue membership is never authored by the agent.

## Performance

Routine runs use history and refresh planning. The assessment agent reads only
the prepared assessment document, not the multi-megabyte raw snapshot.

A warm live run must be measured before claiming reuse. The report records
wall-clock duration, GitHub GET count, refreshed evidence count, reused evidence
count, and the number of issues requiring semantic assessment.

## Future Write Contract

Write enablement is a separate design and implementation phase. It requires:

- Explicit producer opt-in to shepherd writes.
- Shadow mode and an execute-off-by-default switch.
- A two-phase intent/outcome action journal.
- Conditional pre-write issue re-read.
- Per-run, per-issue, and per-person budgets and cooldowns.
- Episode-aware idempotency keys.
- A kill switch.
- Protection against the `ci-failure-cause` producer's 500-closed-issue lookup
  cap.

No current issue is eligible for autonomous shepherd closure.

## Acceptance Criteria

- Unknown or empty ledgers cannot support resolution.
- The five-column #19149 table is recognized without treating its absence
  window as complete workflow history.
- Tracking issues derive occurrences from run-marker comments.
- Dashboard-produced issues have unknown lane state and no automated action.
- `autoclose:false` and missing policy never permit executable closure.
- Assessment bundles contain at most 25 evidence records.
- The agent cannot select an action outside deterministic `allowedActions`.
- Markdown queues are deterministic.
- A warm run reports actual reuse rather than assuming it.
- Every GitHub request remains GET-only.
