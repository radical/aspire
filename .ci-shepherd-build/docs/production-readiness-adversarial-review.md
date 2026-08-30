# CI Shepherd Adversarial Production-Readiness Review

## Verdict

The current design is **overbuilt in validation machinery and evidence
taxonomy, but was underbuilt in a few small execution preconditions**.

The highest-information next step is one real comment tracer on
`radical/aspire`, not a bespoke 160-scenario harness. Most authorization,
execution-ledger, replay, and state-integrity behavior already has fast
hermetic coverage.

## Findings already addressed on this branch

- `edit-comment` proposals now bind the source comment body digest and refuse a
  concurrent body change.
- Execute mode now requires a live executable CI label before mutation.
- The authorization round-trip test derives its proposal timestamp from the
  test clock instead of expiring on a fixed date.
- Issues carrying `quarantined-test` are excluded before a quarantine worker is
  proposed.

## Non-negotiable controls

1. Keep the existing independent production-repository denials in
   `github_actor.py`, `authorization.py`, and
   `quarantine_authorization.py`.
2. Keep live target, state, issue-version, actor-identity, ownership, and
   idempotency checks in `actor.execute_action`.
3. Keep the append-only execution and quarantine-session state machines,
   including exact action, repository, state-directory, head-SHA, and test-set
   binding.
4. Before any quarantine proposal, fail closed when the target cannot be
   resolved to exactly one test method.
5. After a quarantine edit, prove both that the exact test is excluded by the
   quarantine filter and that it remains discoverable without that filter.
   Validate that no test logic changed.

## Simplified delivery plan

### 1. Fork comment tracer

Create one namespaced fixture issue on `radical/aspire`, generate one eligible
comment proposal, issue a one-action grant, execute it, and reconcile the live
comment. Re-run the same action and prove no second write occurs.

Exit criteria:

- exactly one live comment;
- one terminal execution event;
- replay performs no mutation;
- no non-GET call or event names `microsoft/aspire`;
- cleanup leaves no open fixture.

### 2. Limited production comment pilot

Run only comment operations, one manually reviewed and separately granted
action per cycle. Keep closure and quarantine disabled.

Exit criteria:

- three consecutive cycles produce either one reconciled comment or a named
  preflight refusal;
- no duplicate comment;
- no unexplained API call;
- each body is reviewed before grant creation.

### 3. Closure

Add focused coverage for a human comment arriving between proposal and close,
and distinguish a shepherd close from a human close during reconciliation. The
first attribution gate is now implemented: a reconciled close must report
`closed_by.login` equal to both the authenticated actor and the configured
shepherd identity.

### 4. Quarantine

Start with same-commit retry recovery only. Resolve each candidate independently
through the existing Roslyn-based `QuarantineTools` path in a scratch checkout.
Do not add a cache until measured invocation cost warrants one.

Require deterministic reasons for unresolved, already-suppressed, or invalid
targets, then validate the AST/diff, build affected projects, and run both
discovery checks before any push.

## Defer until evidence justifies the cost

- Class B and C automatic quarantine decisions.
- Relevant-path mapping and a known-nondeterministic signature corpus.
- Source-resolution caching and invalidation machinery.
- A second GitHub identity.
- Repository rename-and-squat checks.
- A seven-file E2E harness and fixture manifest.
- A 48-hour soak; use three consecutive cycles first.
- Live duplication of authorization cases already covered hermetically.

## Remaining correctness gaps

- The comment mutation path now has one integrated executable tracer and one
  live fork confirmation; repeat the live tracer once before the production
  comment pilot.
- Quarantine source resolution and post-edit validation remain prose rather
  than deterministic gates.
- Historical `blockedTargets` are now keyed by canonical test name, so changed
  summary or evidence metadata cannot silently unblock the same test.
- Close reconciliation now refuses a close attributed to a different account.
  Same-account human and automated closes remain indistinguishable.

## Recommended next TDD slices

1. Repeat the fork comment tracer once after a full teardown.
2. Add per-test fail-closed source resolution for quarantine without caching or
   Class B/C inference.
3. Add the close freshness regression before enabling closure.
