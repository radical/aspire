# CI Shepherd Actor Design

## Purpose

The CI shepherd already converts collected evidence into validated judgments
and deterministic `action-proposals.json`. The missing final boundary is a
small actor that can show exactly what those proposals would do and execute one
explicitly selected proposal when requested.

The actor is dry-run by default. GitHub mutation requires both `--execute` and
one exact `--action-id`.

## Scope

The proof-of-concept actor supports only proposal operations already emitted by
the current renderer:

- `create-comment`
- `edit-comment`
- `close-issue`

It does not schedule work, execute a whole plan, infer approvals, reinterpret a
judgment, or generate new mutation text. Batch execution remains deferred until
single-action execution is proven safe and useful.

## Command Contract

```bash
python3 .ci-shepherd-build/scripts/execute_actions.py \
  --proposals action-proposals.json \
  --results action-results.json
```

This is dry-run mode. It validates the proposal document and prints every
operation, including action ID, target issue, exact comment body or close
reason, evidence IDs, dependency, expected state, and the fact that no
preflight or mutation occurred. It does not access GitHub and does not write a
result record.

An optional `--action-id` limits dry-run output to one proposal.

```bash
python3 .ci-shepherd-build/scripts/execute_actions.py \
  --proposals action-proposals.json \
  --results action-results.json \
  --action-id snapshot:...:issue:19149:review-close-comment \
  --execute
```

Execute mode requires `--action-id`. Omitting it is an error; `--execute` never
means execute every proposal.

## Components

### Proposal validation

The actor accepts only schema version 1, a nonempty `owner/repository`, unique
action IDs, known operations, and operation-specific fields. It rejects
arbitrary API paths or methods. Comment operations require an `[automated] `
body and expected issue state. Edit operations require a numeric comment ID.
Close operations require a known GitHub close reason.

### Dry-run rendering

Dry-run uses the same parsed proposal that execute mode would receive. It emits
stable JSON so the exact action can be reviewed or diffed. A dry-run record has
`mode: "dry-run"` and `wouldExecute: true`; it is output only and is not mixed
with durable execution results.

### Execute mode

Execute mode selects exactly one proposal and performs these steps:

1. If `dependsOn` is present, require an `executed` result for that action ID.
2. Refetch the target issue or comment.
3. Verify expected issue state and operation-specific material state.
4. Check the proposal idempotency key or existing target state.
5. Execute the known GitHub operation.
6. Refetch the target and verify the visible result.
7. Append one terminal result to `action-results.json`.

The actor never edits `action-proposals.json`.

### Results

`action-results.json` is owner-only and contains one terminal record per
attempted action:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "results": [
    {
      "actionId": "snapshot:...:issue:19149:review-close",
      "attemptedAt": "2026-08-21T20:00:21Z",
      "outcome": "executed",
      "preflight": {
        "issueState": "open"
      },
      "result": {
        "issueState": "closed",
        "stateReason": "completed"
      }
    }
  ]
}
```

Terminal outcomes are `executed`, `stale`, and `failed`. A duplicate action ID
is not attempted again.

## Operation Rules

### Create comment

Preflight requires the issue to match `expectedIssueState` and no owned status
comment with the same idempotency key. Execution posts the proposal body.
Verification requires a comment authored by the authenticated user with the
same body and marker.

### Edit comment

Preflight requires the issue state, the exact comment ID, authenticated
ownership, and the proposal idempotency marker. Execution replaces the body.
Verification requires the live body to match the proposal.

### Close issue

Preflight requires the issue to be open and any `dependsOn` action to have an
`executed` result. Execution closes with the proposal's `closeReason`.
Verification requires the closed state and matching state reason.

## Failure Behavior

Changed or already-satisfied target state is recorded as `stale`; no mutation
occurs. API, authentication, or verification failures are recorded as
`failed`. Invalid proposal or result documents stop before GitHub access.

The actor does not retry mutations because an uncertain response must first be
reconciled against GitHub state.

## Testing

Tests use a scripted GitHub client and prove:

- dry-run is the default and performs no client calls;
- execute mode requires one action ID;
- unknown operations and malformed proposals fail before GitHub access;
- dependencies must have an `executed` result;
- idempotency and state mismatches abort before mutation;
- each supported operation uses its exact proposal fields;
- verified executions append owner-only result records; and
- repeated action IDs are not executed twice.
