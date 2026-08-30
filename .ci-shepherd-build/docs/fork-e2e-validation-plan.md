# Fork End-to-End Validation Plan

This plan validates the CI shepherd against `radical/aspire` as a mutable staging
environment before it is allowed to make GitHub-visible changes to
`microsoft/aspire`.

It defines the threat model, the observable invariants, a prioritized scenario
matrix, the reusable harness, the staged execution order, and the go/no-go
criteria.

The design rationale for the behavior being validated is in
`production-readiness-design.md`. This document is the validation side.

## 1. Why this plan exists

Two observations from the recorded production dry run drove the initial scope.

Every proposal in that run was `eligible: false`. On 2026-08-30, the Stage 0
comment tracer and the full initial live gate completed on `radical/aspire`.
A cycle-generated proposal for issue `#74` produced one comment under one exact
grant; replay made no API call; concurrent edit, deletion, removed-label, and
post-POST crash scenarios failed closed or reconciled without a duplicate.
Pull request `#68` proved that a `ping-human` override survives an unchanged
cycle and is invalidated by a head change. The API audits named no production
write, and cleanup left all fixtures closed with no comments or branches.

The quarantine proposal contained candidates that were not valid quarantine
targets, and the only thing standing between those candidates and a real edit is
prose inside a generated worker prompt.

A staging fork is the only way to exercise the mutation and reconciliation paths
against real GitHub semantics without risking the production repository.

## 2. Threat model

### 2.1 Assets

| Asset | Loss mode |
| --- | --- |
| `microsoft/aspire` issue state | Wrong close, wrong comment, comment spam, overwritten human text |
| Contributor attention | False escalation, duplicate pings, noise on unrelated issues |
| Test-suite coverage | Bad quarantine silently removes real signal |
| Shepherd state ledgers | Corruption leading to replayed mutations or lost suppression |
| Reviewer trust | One visibly wrong automated close ends the pilot |

### 2.2 Actors

| Actor | Permitted power | Capability if buggy or compromised |
| --- | --- | --- |
| Coordinator | GET only | Writes state, can poison downstream selection |
| Assessment agent | No GitHub access | Arbitrary judgments, prompt-injectable from issue bodies |
| Investigation worker | GET only | Can emit `fixable`, can consume budget |
| Quarantine worker | Local edit, fork push | Can delete tests, touch unrelated files |
| Executor | Three issue operations, grant-bound | The only path to a production mutation |
| Human maintainer | Everything | Concurrent editor, primary source of time-of-check races |
| Other bots | Comment and label | Can post text carrying shepherd-looking markers |
| GitHub API | Not controlled | 404, 422, 5xx, eventual consistency, deleted objects |

### 2.3 Threats

- **T1 Production mutation.** Any write reaching `microsoft/aspire` before
  go-live.
- **T2 Grant scope creep.** Executing an action the grant did not enumerate, a
  dependent close authorized by approving only its comment, or a budget reset by
  copying the grant or rebinding the state directory.
- **T3 Wrong target.** Bare `#1234` provenance, a duplicate cluster selecting the
  wrong canonical issue, or issue-number collision across repositories.
- **T4 Comment identity collision.** An idempotency key matching a comment the
  shepherd does not own, or an edit overwriting human-authored or human-edited
  text.
- **T5 Bad closure.** Closing a reopened issue, closing when the paired comment
  did not succeed, or closing after a human posted contradicting evidence.
- **T6 Time-of-check races.** Label removed, issue closed, or comment deleted
  between proposal generation and execution.
- **T7 Coverage loss.** Quarantining an already-quarantined test, a nonexistent
  method, a shard or job label, an infrastructure failure, or a deterministic
  product defect.
- **T8 Authorization replay or forgery.** Expired grant, mutated proposal bytes,
  wrong state directory, cross-snapshot reuse, or reused grant ID.
- **T9 Identity confusion.** Configured `shepherd-author` disagreeing with the
  authenticated login, or a third party forging shepherd markers.
- **T10 State corruption.** Non-append-only history, `current.json` advanced by a
  failed cycle, or concurrent recorders against one state directory.
- **T11 Silent degradation.** Partial collection reading as complete, or a
  truncated open-bot scan producing a clean-looking report.
- **T12 Self-feedback.** The shepherd's own status comment re-entering evidence.
- **T13 Runaway.** Investigation fan-out beyond the plan cap, or proposals beyond
  the per-issue cap.
- **T14 Environment bleed.** Staging fixtures leaking into a production cycle, or
  cleanup failing and leaving debris that changes the next run's inventory.

## 3. Observable invariants

Every invariant is checkable from public CLI output, recorded artifacts, ledger
files, or a live read-only GET. None require reading private helpers.

| ID | Invariant | Where observed |
| --- | --- | --- |
| I1 | No non-GET call and no action event ever targets `microsoft/aspire` during staging | `api-calls.jsonl`, `action-events.jsonl` |
| I2 | Terminal action events sharing a grant ID never exceed the grant mutation budget | `$STATE/action-events.jsonl` |
| I3 | Every executed action ID appears verbatim in the grant `allowedActionIds` | ledger against grant |
| I4 | A `close-issue` has no terminal event unless its `dependsOn` comment has a terminal success first | ledger ordering |
| I5 | Re-running an identical cycle and execute creates zero new GitHub objects | live GET plus ledger |
| I6 | `edit-comment` targets only a comment whose author equals the authenticated login and whose body still matches the recorded hash | intent event preconditions |
| I7 | A proposal whose live target state differs from `expectedIssueState` fails closed with no mutation | execute output, ledger |
| I8 | `collectionComplete: false` implies `eligible: false` for affected proposals; scoped errors block only named issues | `action-proposals.json` |
| I9 | A failed or interrupted cycle does not advance `current.json` | `$STATE/current.json` |
| I10 | Ledgers are append-only: each prior byte prefix survives every later cycle | prefix hash comparison |
| I11 | An unchanged replay selects zero cases and carries forward every non-default judgment, for issues and pull requests alike | `cycle.json`, `judgments.json`, `pull-request-judgments.json` |
| I12 | `quarantine-session.json` contains no already-quarantined, nonexistent, or non-method-shaped target | proposal document |
| I13 | Shepherd-authored comments never appear in `allowedEvidence` or any `evidenceIds` | `assessment-input.json`, proposals |
| I14 | `investigation-plan.json` holds at most five requests, overflow in `deferredRequests` | plan document |
| I15 | Action grants and quarantine grants both hard-deny `microsoft/aspire` | non-zero exit, stderr |
| I16 | Every posted body begins with `[automated] ` | rendered body, live comment |
| I17 | A surviving `intent` or `indeterminate` event permits reconciliation only | ledger plus rerun |
| I18 | Every artifact produced by a staging cycle names `radical/aspire` | all artifacts |
| I19 | The report is rendered from reconciled live state, not from the proposal document | `report.md` after partial execution |
| I20 | Cleanup leaves zero namespaced issues, comments, pull requests, and labels | post-cleanup GET |
| I21 | Every quarantine batch entry records an evidence class and its reason; entries at class B or C additionally record a named corroborating signal from the closed list | `quarantine-session.json` |
| I22 | No candidate reaches quarantine eligibility on class D evidence, regardless of how many class D rows exist | proposal document, `blockedTargets` |
| I23 | After a quarantine edit, the exact method carries exactly one quarantine attribute whose issue URL is byte-equal to the batch entry's original issue URL | post-change inspection output |
| I24 | After a quarantine edit, the exact method is absent under the quarantined-trait filter and present without it | discovery output, both filters |
| I25 | A quarantine session is recorded `completed` only after the attribute is observed on the target branch | session ledger, live GET of the merged tree |
| I26 | Every initial source resolution runs independently through the same QuarantineTools path used for mutation | invocation log and per-candidate diff |

## 4. Scenario matrix

The matrix is a long-term regression catalog. Its historical P0/P1/P2 labels
describe impact, but do not make every row an initial go-live prerequisite.
Section 4.0 defines the smaller gate for the current delivery stage.

Placement: **L** is a deterministic local integration test with fixtures and a
fake actor client, **E** is a live fork test, **L+E** is local first with one
live confirmation.

Each row states setup, the external mutation applied between cycles, the expected
observable artifacts or live state, the exact safety assertion, cleanup, and the
regression it catches.

### 4.0 Initial go-live gate

These 28 scenarios are the mandatory gate before a bounded production
**comment-only pilot**. All other rows remain future hardening or gates for later
capabilities and unattended operation.

**Status on 2026-08-30:** 28 of 28 pass. Deterministic coverage passes from an
index-only export, and the live subset passed on `radical/aspire` with complete
fixture cleanup and no production write.

| Capability | Required scenarios |
| --- | --- |
| Read-only cycle and state continuity | D1, D4, D5, D6, D7, D9, G1, J1, J1a, J2, J3, K1, K2, K4 |
| One-action comment pilot | A1, B1, B2, B3, B5, B7, C1, C4, C5, C6, C7, C10, C13, L6 |

Closure additionally requires A4, A5, A6, and A8. Class A quarantine
additionally requires H1 through H17 where applicable, H22 through H29, I4, and
M1 through M3. These later gates do not block comment automation. The production
repository guard remains in place for every capability until its own gate
passes.

### 4.1 Suite A — Issue lifecycle (P0)

| ID | Setup | Mutation between cycles | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | Fixture issue open with `ci-failure-cause` and `test-failure`, body carrying one parsed occurrence | none | Cycle 1 selects it as first-seen; one `create-comment` proposal with `eligible: true` | `actor-dry-run.json` reports `wouldExecute: true`; after execute exactly one live comment exists and its body begins `[automated] ` | Delete the comment, close the issue | Eligibility has never been true in production; the mutation path is unproven | L+E |
| A2 | A1 executed | Close the fixture issue through the API | Cycle 2 produces no executable proposal for it; executing the stale action ID fails closed | `action-events.jsonl` gains no terminal event; execute exits non-zero naming a state mismatch | Reopen | Executing against a closed issue (T6) | E |
| A3 | A2 | Reopen the issue | Cycle 3 re-selects it; an unchanged body yields no new comment | Live comment count unchanged when the rendered body is byte-identical | Delete comment, close | Reopen causing a duplicate comment (T4) | E |
| A4 | Issue with a `review-close` judgment plus duplicate evidence | none | Two proposals, `review-close-comment` and `review-close`, the second carrying `dependsOn` | `create_authorization.py` exits non-zero when only the close action ID is named | Reopen | Dependent close authorized alone (T2, I4) | L |
| A5 | A4 with the comment action recorded as a terminal failure | none | The close refuses | No close event in the ledger; the refusal names the unmet dependency | Reopen | Dependent close proceeding on any terminal event rather than a terminal success (T5, I4) | L |
| A6 | A4 fully approved | Human closes the issue first | The close is a clean no-op or a clean refusal | Live issue records exactly one close transition and is never reopened by the shepherd | Reopen | Redundant close and state thrash | E |
| A7 | Two fixture issues, one of which is deleted or transferred | Delete one | Cycle degrades with a `CollectionError`; the surviving issue still produces a proposal | Document is `partially-eligible`; the surviving action remains previewable | Recreate fixture | Whole-cycle abort caused by one missing target (T11) | L |
| A8 | Fixture issue with a `review-close` proposal | Human adds a comment presenting contradicting evidence | Close refuses because new human activity postdates the evidence fingerprint | No close event | Delete comment | Closing over fresh human input (T5) | E |

### 4.2 Suite B — Comments, idempotency, concurrency (P0)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | A1 comment posted under a grant with budget 1 | Re-invoke with the same grant and action ID | Second invocation refuses | Live comment count stays 1; terminal event count for that grant ID stays 1 (I2) | Delete comment | Budget reset by sequential invocation (T2) | L+E |
| B2 | Shepherd comment posted | A second identity edits the shepherd comment body | Next `edit-comment` fails closed | No write occurs; the refusal names a body-hash mismatch (I6) | Delete comment | Silently overwriting a human edit (T4) | E |
| B3 | Shepherd comment posted | Delete the comment | Next cycle proposes `create-comment`, not an edit against a dead ID | No 404-producing write attempt; exactly one new comment afterward | Delete comment | Edit against a deleted comment | E |
| B4 | A second identity posts a comment containing the exact shepherd markers | Forge markers | Shepherd refuses to adopt it as owned | `edit-comment` blocked on author mismatch; a separate owned comment is created instead | Delete both | Marker forgery and identity confusion (T9, I6) | E |
| B5 | Shepherd status comment exists on a fixture issue | none | The comment does not appear in evidence | Its comment ID is absent from `allowedEvidence` and every `evidenceIds` array (I13) | Delete comment | Self-feedback loop (T12) | L |
| B6 | Two cycles started concurrently against one state directory | none | Second recorder fails loudly or serializes | Every ledger line parses; no interleaved partial line; event count equals the intended sum | Reset state directory | Concurrent recorder corruption (T10) | L |
| B7 | Execute killed between the fsynced `intent` event and the API call | SIGKILL | Rerun reconciles only | No second comment created; the surviving event resolves to reconciled, never to a new mutation (I17) | Delete comment | Double-post after a crash | L+E |
| B8 | Grant authorizing two comment actions; the second target is deleted mid-run | Delete target | First applies, second fails | Ledger shows one success and one failure; `report.md` reflects live state, not the proposal document (I19) | Recreate fixture | Partial execution misreported as complete | E |

### 4.3 Suite C — Labels, permissions, authorization (P0)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | Eligible proposal generated for a labeled issue | Remove every executable CI label after generation | Execute fails closed | Refusal names `missing-ci-label` derived from live state, not the snapshot; no mutation | Restore labels | Stale label state (T6) | E |
| C2 | Issue carrying only `area-*` labels | none | Proposal generated but never executable | `eligible: false` with `missing-ci-label` | Close | Acting on non-CI issues | L |
| C3 | Issue with `test-failure` only, proposal generated | Add `ci-failure-cause` | The pre-existing action stays ineligible; only a fresh cycle can promote it | Old action ID still refuses | Remove label | A label addition silently upgrading a stale grant | E |
| C4 | Valid grant | Alter one byte of `action-proposals.json` | Execute rejects on digest mismatch | Non-zero exit, no mutation | none | Document-integrity bypass (T8) | L |
| C5 | Valid grant | Advance past `expiresAt` | Reject | Non-zero exit, no mutation | none | Expired grant acceptance (T8) | L |
| C6 | Valid grant | Point `--state-dir` at a different directory | Reject | Non-zero exit, no mutation | none | State-directory rebinding to dodge the budget (T2) | L |
| C7 | Grant for action X | Pass `--action-id` for action Y | Reject | Non-zero exit, no mutation (I3) | none | Action substitution (T2) | L |
| C8 | Grant with budget 1 covering two actions | Approve both | Second stops on budget | Exactly one terminal event (I2) | Delete comment | Budget bypass across invocations | L+E |
| C9 | Grant copied to a second path | Run with the copy | Budget already exhausted | No second mutation | none | Budget reset by copying the grant (T8) | L |
| C10 | `--repository microsoft/aspire` | none | Both `create_authorization.py` and `authorize_quarantine.py` hard-deny | Non-zero exit, no grant file written (I15) | none | Production write (T1) | L |
| C11 | Shared executable-label policy | none | Proposer and executor use one constant and one normalizer | A mixed-case executable label is accepted and canonicalized in both paths | none | Label-set or case-handling drift between proposal and execution | L |
| C12 | Token downgraded to read-only mid-run | Swap credentials | Execute fails cleanly | Failure is a clean refusal with a terminal failure event, not a partial write or a hang | Restore token | Permission change during execution | E |
| C13 | Grant issued for snapshot S1, proposals from snapshot S2 | none | Reject | Non-zero exit, no mutation | none | Cross-snapshot grant replay (T8) | L |

### 4.4 Suite D — Evidence freshness and transitions (P1)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | Issue citing a run whose logs return 404 without a collection error | none | `unavailable-evidence` names the run while `collectionComplete` remains true | `eligible: false`; evidence availability and collection completeness remain distinct signals | none | Acting on unavailable evidence, or conflating it with collection completeness | L |
| D2 | D1 | Evidence becomes available | Blocking reason clears next cycle | `eligible: true`, body regenerated and separately approvable | Delete comment | A stale block that never clears | L+E |
| D3 | Eligible proposal with a live grant | Evidence becomes unavailable | Action becomes ineligible | Grant fails closed, no mutation | none | A grant outliving its evidence (T6) | L |
| D4 | Issue-scoped collection error on issue A, clean issue B | none | A blocked, B previewable | Document is `partially-eligible` (I8) | none | Scoped error blanket-blocking everything | L |
| D5 | Unscoped collection error | none | Every action blocked | Document is ineligible | none | Unscoped error leaking through | L |
| D6 | Open-bot scan budget reduced below inventory size | Shrink budget | Warning plus `open_bot_scan: truncated` in the report | `report.md` never reads as a clean inventory | Restore budget | Truncated inventory read as complete (T11) | L |
| D7 | Open-bot scan page returns 500 | Inject failure | `failed` plus a `CollectionError` with stage `open-bot-scan` | Error recorded, cycle degrades rather than aborts | none | Failed scan degrading silently | L |
| D8 | Fixture issue with a stable fingerprint | Push a new failing workflow run citing the same test | Fingerprint changes and the issue is re-selected | `cycle.json` reports a non-zero issue review count | Delete run | Material change not triggering reassessment | E |
| D9 | Issue reviewed and stable | Advance the clock past the seven-day reassessment deadline | The issue is re-selected without any evidence change | It appears in `review-selection.json` | none | Reassessment backstop not firing | L |

### 4.5 Suite E — Duplicate clusters (P1)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | Two fixture issues for the same test with explicit cross-links | none | One canonical, one `review-close` with reason duplicate | The close targets the duplicate and never the canonical; selection is deterministic across reruns | Reopen | Closing the canonical issue (T3) | L |
| E2 | E1 | Human marks the other issue canonical | Shepherd defers or re-derives | Either no close, or a close matching the human's designation | Reopen | Bot and human fighting over canonical choice | E |
| E3 | Two issues linked only by a bare `#1234` mention | none | `untrusted-reference-provenance` | No close proposal is execution-eligible | none | Bare-mention provenance authorizing a close (T3) | L |
| E4 | Two issues superficially similar but naming different tests | none | No cluster formed | No close proposal exists | none | Over-eager clustering | L |
| E5 | Cluster of three, middle one closed externally | Close the middle issue | Cluster re-derives without it | No close proposal targets the already-closed issue | Reopen | Redundant close on a closed cluster member | E |
| E6 | Candidate whose evidence includes an issue reference outside the collected snapshot | none | The unrelated reference is dropped or marked untrusted | Every `evidenceId` resolves inside the snapshot or an explicitly trusted reference set | none | Recurrence attributed to unrelated issues or runs | L |

### 4.6 Suite F — Positive recovery (P1)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | Issue with an active shepherd escalation comment | Underlying failure stops recurring | A retire status edit is proposed with terminal wording | Exactly one `edit-comment`, and the live body changes | Delete comment | An escalation that never retires | L+E |
| F2 | F1 executed | none | Next cycle proposes nothing for that issue | Zero proposals for it; live comment unchanged (I5) | Delete comment | Edit churn on unchanged state | E |
| F3 | Retired issue | Failure recurs | A new escalation edit is proposed | Live body reflects the new occurrence | Delete comment | Retired state sticking through a regression | E |
| F4 | Issue whose fix merged | Merge a fork pull request referencing the issue | `review-close` with resolved reason and resolution-context provenance | The close cites the merged pull request, not a bare mention | Reopen | Closing on an unmerged or unrelated pull request | E |

### 4.7 Suite G — Investigation budgets (P1)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| G1 | Twelve `investigate` recommendations | none | At most five requests, the rest deferred | `len(requests) <= 5` and the remainder appear in `deferredRequests` (I14) | none | Runaway investigation fan-out (T13) | L |
| G2 | A started session | Record a result under a different session ID | Reject | No ledger completion event | none | Cross-session result injection | L |
| G3 | A started session | Record the same result twice | Second call returns the persisted result | Terminal event count stays 1 | none | Duplicate terminal events (T10) | L |
| G4 | A started session whose worker died | Record `failed` with a reason | The same request becomes proposable again | The request reappears in a later plan | none | A permanently stuck investigation slot | L |
| G5 | A completed result | Source-evidence fingerprint changes | The stale result is withheld and a new request is created | The stale conclusion is absent from the agent input | none | Stale conclusions poisoning judgment | L |
| G6 | A `fixable` result | none | No code change, assignment, or pull request is proposed | Proposal set is unchanged by the result | none | `fixable` escalating into autonomous code changes | L |

### 4.8 Suite H — Quarantine candidate validation (P0)

Every row is a new deterministic gate. None of these checks exist today.

| ID | Setup | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- |
| H1 | `review-quarantine` for a method already carrying the quarantine attribute at the checkout commit | Candidate excluded | The test name is absent from `proposal.tests` and present in `blockedTargets` with reason `already-quarantined` | none | Re-quarantining an already-quarantined test | L |
| H2 | Source issue already carries `quarantined-test` | Candidate excluded | Reason `already-quarantined-by-label` | none | Label-path duplicate quarantine | L |
| H2a | The capped evidence bundle omits the source issue event or its labels are malformed | Candidate excluded | Reason `source-labels-unavailable`; no quarantine proposal contains the target | none | Missing label evidence failing open as “not quarantined” | L |
| H3 | Target value is a shard or job label containing spaces or parentheses | Candidate excluded | Reason `not-a-test-method`; no proposal entry contains whitespace in `testName` | none | Quarantining a job or shard label instead of a method | L |
| H4 | Well-formed name resolving to no method at the checkout commit | Candidate excluded | Reason `target-not-found-in-checkout` | none | Quarantining a nonexistent method | L |
| H5 | `review-quarantine` whose failure text matches the enumerated infrastructure signature corpus, such as runner loss or image pull failure | Candidate excluded | Reason `infrastructure-failure`; the reason cites the matched signature, not a test attribute | none | Quarantining an infrastructure failure | L |
| H5a | Valid flaky candidate whose method carries `[RequiresFeature(TestFeature.ContainerRuntime)]` and whose failure is an ordinary assertion failure | Candidate proceeds normally | `[RequiresFeature]` appears in the report as context only; it produces no exclusion reason and does not set `infrastructure-failure` | none | A capability gate being misread as proof of infrastructure failure, silently dropping real flaky candidates | L |
| H5b | `review-quarantine` for a method already carrying `[ActiveIssue]` | Candidate excluded | Reason `already-suppressed`, distinct from `infrastructure-failure`; `blockedTargets` names the existing attribute | none | Re-suppressing a test that is already disabled, and mislabelling that as an infrastructure failure | L |
| H6 | Deterministic failure that persists across a same-commit retry, with no observed recovery on any equivalent lane | Candidate excluded | Reason `suspected-product-defect`; the candidate routes to investigate | none | Quarantine hiding a real product bug | L |
| H6a | Flaky candidate whose occurrences all carry an identical assertion message and identical expected/actual values, but which recovered on a same-commit retry | Candidate proceeds | The stable-signature risk flag is present in `report.md`, and the candidate is **not** excluded; no `suspected-product-defect` reason is emitted | none | Treating a stable assertion signature as a determinism veto, which would block the most common real races and timeouts | L |
| H6b | Relevant source path changed immediately before the failure, and the same commit later failed again on retry | Candidate excluded | Reason `suspected-product-defect` citing the changed relevant path and the absence of same-code recovery | none | Quarantining a regression introduced by a recent change | L |
| H6c | Relevant source path changed before the failure, but a same-commit retry recovered | Candidate proceeds as Class A | The changed path is reported as context; it does not veto, because same-code recovery demonstrates nondeterminism | none | A path-change heuristic overriding direct retry evidence and blocking legitimate quarantine | L |
| H7 | Evidence rows citing runs whose jobs never executed the named test | Rows dropped | Surviving rows below threshold yield `insufficient-recurrence` | none | Recurrence attributed to unrelated jobs or runs | L |
| H8 | Two issues naming the same valid test | One batch entry | Both issue URLs present in `issueUrls`; the worker prompt names both | none | Two pull requests for one test | L |
| H9 | Every candidate excluded | No batch proposed | `batchId` is null, `workerPrompt` is null, `quarantineSessionProposed` is false | none | An empty quarantine pull request | L |
| H10 | Mixed valid and invalid candidates | Valid proceed, invalid reported | Every excluded target appears in `blockedTargets` and in `report.md` | none | Silent candidate loss | L |
| H11 | Quarantine grant requested for `microsoft/aspire` | Hard-deny | Non-zero exit, no grant written (I15) | none | Production quarantine (T1) | L |
| H12 | Approved single-test batch on the fork, worker opens a draft pull request | Result recorded only after verification | Recording rejects on any mismatch of repository, URL, draft state, or 40-character head SHA | Close the pull request, delete the branch | Ledger head diverging from the pushed commit | E |
| H13 | A pending draft pull request exists | No new batch proposed | A non-null `activeBatchId` suppresses every new quarantine proposal | Close the pull request | Parallel quarantine sessions | L+E |
| H14 | Pending pull request closed unmerged | Recorded `failed` | The affected tests become proposable again | Delete branch | A permanently suppressed test | E |
| H15 | Pending pull request merged | Recorded `completed` | The tests are absent from every later batch | Revert on the fork | Re-quarantining a merged test | E |
| H16 | Worker diff touches a file outside the expected set | Session recorded `failed`, nothing pushed | Post-action diff comparison reports only expected attribute additions and using directives | Reset the worktree | Unintended source changes | L+E |
| H17 | Source inspection cannot run | Every affected candidate excluded | Reason `source-inspection-unavailable`; no candidate is assumed valid | none | Inspection failure degrading to an unchecked proposal | L |
| H18 | Inspection result cached at commit `C` with `treeFingerprint: "clean"`; the worktree is then modified so the target method no longer exists, HEAD still `C` | Cache miss, re-inspection | The dirty tree fingerprint differs, so the cached entry is not reused; the candidate is excluded with `target-not-found-in-checkout` | Restore worktree | A dirty checkout serving a stale cached resolution and authorizing an edit to code that no longer exists | L |
| H19 | Inspection result cached, then the inspector tool version or schema version changes | Cache miss, re-inspection | The cache key includes inspector identity, so no entry produced by the previous inspector is reused | Clear cache | An inspector fix being silently masked by cached results from the buggy version | L |
| H20 | Eight quarantine candidates in one cycle, none cached | One inspection process | Exactly one inspector invocation is recorded, with eight entries in its response; the per-candidate results match a per-candidate control run | Clear cache | N process launches per cycle, and divergence between batched and single-name resolution | L |
| H21 | Eight candidates, five already cached at the same commit and clean fingerprint | One inspection process for three names | Only the three misses are sent; the five hits are byte-identical to their cached entries | Clear cache | Cache not being consulted before batching, wasting the cache entirely | L |
| H22 | Approved single-test batch; the worker applies the quarantine attribute | Post-change re-inspection passes | Re-inspection at the modified tree shows exactly one quarantine attribute on the exact method, and its issue URL is byte-equal to the batch entry's original issue URL | Reset worktree | An attribute applied with the wrong issue URL, which looks correct in a diff summary | L+E |
| H23 | Worker applies the attribute but also modifies an assertion in the same file | Session recorded `failed`, nothing pushed | AST and diff validation permits only quarantine attribute additions and required `using` directives; any statement, expression, signature, or other attribute change is a hard failure | Reset worktree | Test logic silently edited under cover of a quarantine change | L+E |
| H24 | Worker applies an attribute without the required `using` directive | Session recorded `failed`, nothing pushed | Every project containing a modified file is built, and the build failure aborts the session before push | Reset worktree | Pushing a quarantine that does not compile | L+E |
| H25 | Attribute applied and the projects build | Selection excludes the method | Discovery under the CI quarantined-trait filter does **not** contain the exact method | Reset worktree | An annotation that is present but does not actually exclude the test from CI | L+E |
| H26 | Worker deletes or renames the test instead of annotating it | Session recorded `failed` | Discovery without the trait filter must still contain the exact method; absence is a hard failure even though the exclusion check in H25 would pass | Reset worktree | Coverage destroyed by deletion while the exclusion assertion still passes | L+E |
| H27 | Validation passes, then the worktree changes before push | Push refused | The recorded validated diff digest no longer matches at push time, so nothing is pushed | Reset worktree | A TOCTOU window between validation and push | L |
| H28 | Draft pull request created after validation | Head SHA and diff verified | The refetched pull request's repository, draft state, 40-character head SHA, and diff digest all match the validated values, or the session records `failed` | Close pull request, delete branch | Recording a head SHA the shepherd never validated | E |
| H29 | Pull request merged on the fork | Recorded `completed` only after observing the merge | The target branch is refetched and the quarantine attribute is confirmed present on the exact method with the exact issue URL; pull-request state alone never records completion | Revert on the fork | Recording completion from pull-request metadata when the attribute never landed | E |
| H30 | Batch of three tests approved for mutation | Three separate tool invocations | `QuarantineTools` is invoked once per test with its own issue URL argument, even though inspection was batched into one call | Reset worktree | Batching the mutation path and losing per-test reviewability and per-test issue URLs | L+E |

### 4.9 Suite I — Retry and rerun (P2)

| ID | Setup | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- |
| I1 | Judgment recommending a workflow rerun | Never enters `action-proposals.json` | The executor rejects the operation type outright | none | Rerun becoming an executable action | L |
| I2 | Transient pattern inside the configured systemic window | Advisory only | Report-only, no proposal | none | Automatic retry without approval | L |
| I3 | A failing run retried by a human and now green | The issue moves onto the recovery path | Suite F behavior triggers on the next cycle | Delete comment | Green-after-retry not detected | E |
| I4 | Evidence contains a same-run, same-commit, same-lane failure on attempt 1 and success on attempt 2, with an artifact-derived exact canonical test name that resolves to one source method | The candidate reaches evidence class A **and enters the quarantine batch** | `proposal.tests` contains the exact method; the recorded class is `A` with its reason; the outcome is quarantine eligibility, not merely a recovery comment on the issue | Reset scratch | Strong same-run retry evidence being consumed only as generic issue recovery, so a legitimately flaky test is never quarantined and CI health is not restored | L+E |
| I5 | Same-run retry recovery, but the successful attempt's artifacts show a different test project or a different selection than the failing attempt | Class A is **not** awarded | The candidate falls back to the weaker classes and requires corroboration; the report states that selection equivalence could not be proven | Reset scratch | A green retry that never ran the failing test being read as proof the test passed | L |
| I6 | Same-run retry recovery where the test name is title-derived rather than artifact-derived | Not quarantine-eligible | Class D contributes zero weight; the candidate is excluded with `insufficient-evidence-class` even though a retry recovery exists | none | Strong retry evidence laundering an unverified test name into a quarantine | L |

### 4.10 Suite J — Pull-request judgment persistence (P0)

Retention of non-default overrides is already covered by earlier regression
tests, and the recorded replay's four dropped entries were all deterministic
defaults, so their disappearance is intended behavior rather than evidence of a
defect. These rows are regression and live-confirmation tests, not
defect reproductions.

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| J1 | Cycle 1 judges a pull request `ping-human`, differing from the deterministic default | none | Cycle 2 selects zero pull requests and still carries the override | `pull-request-judgments.json` still contains the `ping-human` entry after an unchanged replay (I11) | Delete comment | Regression: a non-default pull-request override being dropped on unchanged replay. Confirms existing local coverage holds against a real repository | L+E |
| J1a | Cycle 1 judges a pull request `no-action`, equal to the deterministic default | none | Cycle 2 does not retain the entry | The absence of a default entry is asserted as **correct** behavior, not a failure; retention is expected only for non-default dispositions | none | A future change that starts persisting defaults, inflating the ledger and making stale defaults look like deliberate judgments | L |
| J2 | J1 | Advance the pull-request head commit | Cycle 3 re-selects the pull request | It appears in `pull-request-review.json`; the stale override is not reused | Reset branch | A stale judgment surviving a head change | E |
| J3 | Pull request judged `no-action`, equal to the default, then replayed unchanged | none | No judgment is retained and no review is requested, but the item remains accounted for as excluded | The report says "0 selected; 1 excluded (unchanged-stable: 1)" and `pullRequestReviewCount` is `0` | none | Reporting continuity loss that makes a stable reviewed pull request indistinguishable from an item outside the inventory | L |
| J4 | Pull request whose current-state fetch fails | none | Only `watch` is permitted | The disposition set is exactly `{watch}` | none | Acting on incomplete pull-request evidence | L |
| J5 | Pull request with an empty or cancelled check set | none | Not treated as green | No `no-action` justified by green checks | none | Cancelled checks read as success | L |
| J6 | Pull request assigned to Copilot | Assign | Rejected at inventory, proposal, and execution | It appears in `rejectedCandidates` at all three layers | Unassign | Mutating a Copilot-assigned pull request | L+E |
| J7 | Active `ping-human` escalation | Human approves the pull request | A terminal status edit replaces the escalation | Exactly one edit; the live body is terminal | Delete comment | An escalation that never clears | E |

### 4.11 Suite K — State recovery and append-only history (P0)

| ID | Setup | Mutation | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K1 | Cycle interrupted after collection and before finalize | SIGKILL | `current.json` unchanged | The previous cycle ID is still current (I9) | Reset scratch | A failed cycle advancing persistent state | L |
| K2 | Three sequential cycles | none | Each ledger grows only by appending | The byte prefix of every ledger is preserved across cycles (I10) | none | Ledger rewrite | L |
| K3 | Scratch directory deleted between cycles | Remove scratch | Recurrence still correct | Occurrence counts derived from `fingerprints.jsonl` are unchanged | none | Recurrence loss on cleanup | L |
| K4 | Converged ledger replayed with unchanged evidence | none | No new case events | `case-events.jsonl` line count is unchanged | none | Case-event churn on a no-op cycle | L |
| K5 | Two recorders against one state directory concurrently | none | Fails loudly rather than corrupting | Every ledger line parses and the failure is explicit | Reset state | Silent state corruption (T10) | L |
| K6 | State directory is a symlink | Replace with symlink | Rejected | Non-zero exit before any write | Restore | Symlink redirection of state | L |
| K7 | State directory nested inside the scratch directory | Nest them | Rejected | Non-zero exit before any write | Restore | State wiped by scratch cleanup | L |
| K8 | Ledger with a truncated trailing line | Truncate mid-line | Earlier events still readable | Reads either tolerate the partial line or fail closed, never silently drop earlier events | Restore | Torn-write data loss | L |

### 4.12 Suite L — Actor identity, authorization, replay (P0)

| ID | Setup | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- |
| L1 | Grant containing duplicate `allowedActionIds` | Reject | Non-zero exit | none | Duplicate-field smuggling | L |
| L2 | Grant containing an unknown extra field | Reject | Non-zero exit | none | Schema laxity | L |
| L3 | Grant requested with a TTL above the cap | Reject | Non-zero exit | none | Long-lived grants | L |
| L4 | Grant enumerating an action whose `dependsOn` is not itself named | Reject at grant creation | Non-zero exit | none | Implicit chain authorization (T2) | L |
| L5 | Configured `shepherd-author` differing from the authenticated login | Reject before any mutation | Non-zero exit naming the identity mismatch | none | Adopting comments the shepherd did not author (T9) | L+E |
| L6 | Proposal targeting `microsoft/aspire` with a fork-scoped token | Denied twice, independently | Both the protected-repository check and the client allowlist refuse (I1, I15) | none | Single-point-of-failure production protection | L |
| L7 | `--results` supplied in execute mode | Reject | Non-zero exit | none | Result injection | L |
| L8 | Legacy schema v1 proposal document | Never executable | `wouldExecute: false` for every action | none | Legacy document execution | L |
| L9 | Dry run with a state directory present | No ledger side effects | `action-events.jsonl` is neither created nor modified | none | Dry run mutating state | L |
| L10 | Grant consumed, then replayed after a state directory restore from backup | Refuse | Consumption is derived from the grant ID in the restored ledger | Restore state | Budget reset by restoring an older state snapshot | L |

### 4.13 Suite M — Evidence classification and lane equivalence (P0)

These rows exercise the flaky-evidence model in section 4 of
`production-readiness-design.md`. None of this classification exists today.

Fixtures are frozen collector snapshots containing synthesized run, attempt, job,
and artifact rows, so classification is fully deterministic. Two rows carry a
live confirmation because artifact retention and attempt visibility are GitHub
behaviors that a fixture cannot prove.

| ID | Setup | Mutation between cycles | Expected observable | Exact safety assertion | Cleanup | Regression caught | Where |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M1 | Attempt 1 fails the exact test, attempt 2 of the same run at the same commit and same matrix coordinates succeeds, and both attempts' artifacts show the same test project and selection | none | Class A | The recorded `evidenceClass` is `A`, its reason names the proven selection equivalence, and the candidate is quarantine-eligible without corroboration | Reset scratch | Class A never being reached, so no candidate is ever quarantinable | L |
| M2 | As M1, but the successful attempt's artifacts list a different test project | none | Not Class A | `evidenceClass` is below `A` and the reason is `selection-equivalence-unproven`; quarantine requires corroboration | Reset scratch | A green retry that skipped the failing test being counted as a pass | L |
| M3 | As M1, but the successful attempt's matrix coordinates differ in OS, architecture, target framework, or shard index | none | Not Class A and not an equivalent lane | Lane equivalence compares every recorded matrix dimension, not a job-name substring | Reset scratch | An environment-specific product bug read as flaky because a different lane went green | L |
| M4 | Exact test fails at commit `C1`; a later build of an equivalent lane at `C2` succeeds; no relevant path changed between `C1` and `C2`; no other episode exists | none | Class B, **not** quarantine-eligible | The candidate is reported as a likely flaky candidate, and `blockedTargets` carries `insufficient-evidence-class` naming the missing corroborating signal | Reset scratch | Class B independently authorizing an automatic quarantine on doubly-indirect evidence | L |
| M5 | M4 plus a second independent failure-and-recovery episode for the same test | none | Class B corroborated, quarantine-eligible | The corroborating signal is recorded by name from the closed list; a generic "more occurrences" count is not accepted as corroboration | Reset scratch | Corroboration being satisfiable by unstructured recurrence | L |
| M6 | M4 plus a green equivalent build immediately before the failure and a deterministic match against the known-nondeterministic signature corpus | none | Class B corroborated, quarantine-eligible | The matched signature ID is recorded; a prose or model-derived match is rejected | Reset scratch | Corroboration inferred from prose rather than a deterministic corpus match | L |
| M7 | One exact-test failure with green equivalent builds on both sides and unchanged relevant paths | none | Class C, recognized but **not** quarantine-eligible | The report lists the issue as a likely flaky candidate **and** states the missing corroboration; both must be present | Reset scratch | Either silently discarding a genuine flaky signal, or quarantining on a single indirect episode | L |
| M8 | M7 plus one corroborating signal from the closed list | none | Class C corroborated, quarantine-eligible | The corroborating signal is one of the enumerated kinds and is named in the batch entry | Reset scratch | Class C being permanently unactionable, so real flakes never clear | L |
| M9 | Candidate whose evidence is only title-derived names, shard labels, unavailable logs, or jobs that never ran the test | none | Excluded | Class D contributes zero weight; the candidate is excluded even when many such rows exist | Reset scratch | Summing weak rows into a quarantine decision | L |
| M10 | A relevant path inside the test's project directory changed between the failing and passing builds | none | Class B and C both unavailable | `relevantPathsChanged` is true and the candidate is not classified B or C | Reset scratch | Calling a real regression flaky because a later unrelated build was green | L |
| M11 | Relevant-path computation unavailable, for example the project graph cannot be resolved | none | B and C withheld entirely | Only Class A can be reached; B and C are not attempted with an assumed-unchanged default | Reset scratch | An unresolvable path mapping silently defaulting to "nothing changed" | L |
| M12 | Live fork run where the target run's attempt artifacts have been retained | none | Class A reachable against real GitHub data | The evidence class and its reason are derived from live attempt artifacts, matching the fixture-derived expectation for the same shapes | Reset scratch | Classification that works only against synthesized fixtures | E |
| M13 | Live fork run where attempt artifacts have expired | none | Class A unavailable, reason recorded | The candidate falls back and the report distinguishes "no retry evidence exists" from "retry evidence exists and disagrees" | Reset scratch | Silent downgrade on artifact expiry, indistinguishable from contradicting evidence | E |

## 5. Local versus live placement

Roughly eighty-five percent of the matrix is deterministic and local. The whole
authorization, eligibility, evidence-classification, and quarantine-gating
surface is a pure function of the snapshot, the prepared assessment, the
judgments, the ledgers, and the checkout. None of it needs GitHub, all of it runs
in seconds, and it should gate every change.

Suite M is local by construction. Evidence classification takes run, attempt,
job, and artifact rows as input, so a frozen snapshot with synthesized rows
covers every class and every corroboration path without a single live run. Only
M12 and M13 go live, because artifact retention and attempt visibility are
GitHub behaviors that a fixture cannot prove.

Quarantine post-change validation splits. Inspection, AST and diff validation,
build, and discovery run against a local checkout and are local. Pull-request
creation, head-SHA verification, and merged-attribute confirmation need a real
repository, so H28 and H29 are live-only.

Live fork tests are reserved for cases where the realism of GitHub itself is the
thing under test:

- a mutation actually succeeding and then correctly refusing on rerun;
- real concurrent human edits, deletions, and forged markers;
- real state transitions the API mediates, including close and reopen;
- a real crash between the intent event and the API call;
- real draft pull-request reconciliation across merge, close, and abandon;
- real pull-request head and review transitions;
- a real merge landing a quarantine attribute on a target branch.

A full live pass should reuse one fixture set across roughly fifteen cycles,
keeping total live mutations near forty, all on the fork and all reversible.

## 6. Future harness design

This section is a design reserve for repeated unattended fork validation. It is
not required for the initial comment pilot. The Stage 0 run used the existing
authorization and executor CLIs plus a minimal audited `gh` wrapper; add the
larger harness only when repeated manual fixture setup becomes the bottleneck.

```text
.ci-shepherd-build/e2e/
  fixtures/manifest.json   declarative fixture set
  guard.py                 preflight tripwires
  provision.py             intent-first fixture creation
  mutate.py                external mutation driver
  run_cycle.py             cycle, approval, and execute wrapper
  assertions.py            artifact, ledger, and live-state assertions
  cleanup.py               namespace-scoped teardown
```

### 6.1 Run namespace

Each run allocates a namespace:

```text
NS = e2e-<UTC compact timestamp>-<6 hex>
```

The namespace appears in four independent places so orphans are always
discoverable even if the manifest is lost:

- issue and pull-request titles: `[automated] [shepherd-e2e][<NS>] <scenario-id> — <description>`
- a body marker: `<!-- shepherd-e2e:ns=<NS> scenario=<id> -->`
- labels: `shepherd-e2e` plus a per-run `shepherd-e2e-<NS>`
- state and scratch paths: `~/.copilot/ci-shepherd-e2e/<NS>/{state,runs}`

All visible fixture content must begin with `[automated] `. That includes issue
titles, issue bodies, fixture comments, and pull-request titles and bodies. A
human landing on a fixture from search must be able to tell at a glance that it
is machine-generated, and the prefix matches the convention the shepherd itself
uses for anything it posts.

Fixtures carry the real target labels, `ci-failure-cause`, `test-failure`, and
`automation-broken`, in addition to the namespaced labels, because the entire
selection path keys on them. The namespaced labels exist for cleanup and orphan
detection, never for selection.

### 6.2 Production protection

Guards are layered so that a bug in any one of them is still caught.

1. `guard.py` preflight refuses to run unless the configured repository is
   `radical/aspire`, and refuses if the checkout's `origin` resolves to
   `microsoft/aspire`.
2. The actor client is constructed with an allowlist containing only the fork.
   An allowlist fails safe in a way a denylist does not.
3. The existing hard-denies in the action-grant, quarantine-grant, and actor
   layers remain in force.
4. After every scenario, an audit assertion greps `api-calls.jsonl` and
   `action-events.jsonl` for any non-GET entry naming `microsoft/aspire` and
   fails the run if one exists. This is the backstop that catches a bug in
   layers 1 through 3.
5. The run uses a token whose write scope covers only the fork, so a defect in
   every preceding layer still cannot mutate production.

The local suite adds a sixth guard: a fixture that patches the subprocess runner
to raise on any non-GET request naming `microsoft/aspire`, so a local test can
never mutate either repository.

**Repository identity check.** At the time of writing, `radical/aspire` resolves
to numeric repository ID `746880239` with parent `microsoft/aspire` at ID
`696529789`. The harness must treat these as values to verify live before each
run, not as constants baked into the code. Repositories can be renamed,
transferred, deleted and recreated, or forked again, and a stale hardcoded ID
would silently disable the guard exactly when it matters. `guard.py` should call
the repository endpoint, assert that the full name, the fork flag, the numeric
ID, and the parent full name all agree with the run configuration, and abort on
any mismatch. Comparing the numeric ID as well as the name is what defeats a
rename-and-squat scenario.

### 6.3 Fixture manifest

The manifest is declarative and carries the expected outcome, so assertions read
the contract rather than hardcoding numbers in test bodies.

```json
{
  "schemaVersion": 1,
  "namespace": "e2e-20260901T1400Z-a91f3c",
  "repository": "radical/aspire",
  "issues": [
    {
      "id": "A1",
      "title": "[automated] [shepherd-e2e][e2e-20260901T1400Z-a91f3c] A1 — single occurrence",
      "bodyTemplate": "occurrence-table-1.md",
      "labels": ["ci-failure-cause", "test-failure", "shepherd-e2e"],
      "state": "open",
      "expects": {
        "selected": true,
        "proposals": ["create-comment"],
        "eligible": true
      }
    }
  ],
  "pullRequests": [
    { "id": "J1", "branch": "e2e/j1", "draft": true, "checks": "green" }
  ],
  "workflowRuns": [
    { "id": "D8", "conclusion": "failure", "attachTo": "A1" }
  ]
}
```

### 6.4 Intent-first provisioning

`provision.py` appends an intent line to `provisioned.jsonl` and fsyncs it before
each creation, then creates the object, then appends the resulting identifier.

This mirrors the discipline the executor already uses, and it is the only reason
cleanup can be trusted. A crash mid-provision leaves an intent line that cleanup
reconciles against a live read, so an object created but never recorded is still
found.

### 6.5 Mutation driver

`mutate.py` implements the external-actor surface. Each verb corresponds to a
mutation column in the matrix.

```text
close-issue | reopen-issue
add-label | remove-label
add-comment --as human|bot|forged-marker
edit-comment --target shepherd|human
delete-comment
edit-issue-body
mark-duplicate --canonical <id>
assign-copilot | unassign-copilot
push-run --conclusion failure|success
expire-evidence | restore-evidence
advance-pr-head | approve-pr | request-changes
downgrade-token | restore-token
```

The `--as` flag matters. Scenarios B2 and B4 test real author mismatch, so the
harness needs a genuine second identity, a separate token on a different account,
rather than a simulated one. A simulated second author would pass while the real
defense is absent.

### 6.6 Cycle runner

`run_cycle.py` shells out to the real command-line surface and never imports
private helpers.

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/cycle.py" start \
  --repository radical/aspire \
  --state-dir "$NS_STATE" \
  --work-dir "$NS_RUNS/cycle-$N" \
  --checkout "$CHECKOUT" \
  --shepherd-author "$GITHUB_LOGIN"

python3 "$CI_SHEPHERD_ROOT/scripts/cycle.py" finish \
  --work-dir "$NS_RUNS/cycle-$N" \
  --agent-judgments "$FIXTURE_JUDGMENTS" \
  --pull-request-judgments "$FIXTURE_PR_JUDGMENTS"

python3 "$CI_SHEPHERD_ROOT/scripts/execute_actions.py" \
  --proposals "$NS_RUNS/cycle-$N/action-proposals.json" \
  --state-dir "$NS_STATE"

python3 "$CI_SHEPHERD_ROOT/scripts/create_authorization.py" \
  --proposals "$NS_RUNS/cycle-$N/action-proposals.json" \
  --action-id "$ACTION_ID" \
  --state-dir "$NS_STATE" \
  --output "$NS_RUNS/cycle-$N/authorization-grant.json"

python3 "$CI_SHEPHERD_ROOT/scripts/execute_actions.py" \
  --proposals "$NS_RUNS/cycle-$N/action-proposals.json" \
  --authorization "$NS_RUNS/cycle-$N/authorization-grant.json" \
  --state-dir "$NS_STATE" \
  --action-id "$ACTION_ID" \
  --execute
```

The assessment agent is replaced by fixture judgment files so end-to-end runs are
deterministic. Model behavior is evaluated separately and offline against a
frozen prepared assessment, which is the correct place for it. Mixing model
nondeterminism into an infrastructure test makes every failure ambiguous.

### 6.7 Assertions

`assertions.py` reads only public surfaces: `cycle.json`,
`action-proposals.json`, `actor-dry-run.json`, `quarantine-session.json`,
`investigation-plan.json`, `report.md`, `api-calls.jsonl`, the ledger files under
the state directory, and live read-only GETs.

No assertion may reach into a private helper. The failures this plan targets are
integration failures, and a unit-level assertion will happily pass while the
integrated behavior is broken.

### 6.8 Cleanup

Per scenario, revert the specific mutation: reopen the issue, restore the label,
delete the comments the shepherd created.

Per run, `cleanup.py --namespace <NS>` queries `label:shepherd-e2e-<NS>`, closes
every issue, deletes every comment authored by the shepherd login, closes every
pull request and deletes its branch, removes the per-run label, and then asserts
that the namespace query returns nothing (I20).

Cleanup finishes by reconciling `provisioned.jsonl` intents against live state and
reporting anything unresolved.

**Cleanup failure is a test failure, not a warning.** Leftover fixtures change the
inventory the next run collects, which makes subsequent results untrustworthy in
a way that is very hard to notice. A partial cleanup that closes issues but
leaves shepherd comments is especially dangerous, because those comments are
correctly ignored as owned control state and therefore leave no visible symptom.

### 6.9 Crash recovery

`run_cycle.py --resume <NS>` reads `provisioned.jsonl` and the state directory,
reconciles any surviving intent or indeterminate action events through the
existing reconciliation path, and classifies the namespace as clean,
reconcilable, or requiring manual repair.

A weekly sweep queries `label:shepherd-e2e` across the fork regardless of
namespace, so an abandoned run cannot accumulate indefinitely.

## 7. Staged execution

### Stage 0 — Tracer bullet

Completed and repeated after teardown on 2026-08-30 with
`radical/aspire#74`. The repeated run started from a real cycle-generated
proposal. One exact grant produced one live comment beginning `[automated] `;
replay returned byte-identical output without another API call; stale evidence,
a competing comment edit, and missing labels refused without mutation; deletion
produced a fresh create proposal; and a SIGKILL after GitHub accepted the POST
reconciled the fsynced intent without a duplicate comment.

GitHub updated the issue timestamp shortly after two fixture mutations rather
than atomically with the immediately preceding read. The actor safely returned
`source-evidence-changed`. A reusable harness must poll until two consecutive
fixture reads agree before collecting the next snapshot; this stabilizes setup
without weakening the actor's freshness check.

### Stage 1 — Smoke, read-only

D1, D4, D5, G1, K1, K2, L6, L9, C10, and C11, plus a complete collection pass
against the fork with zero mutations. Confirms staging inventory works and that
no write escapes.

### Stage 2 — Quarantine gates and evidence classification, local only

Two tracks, both local, both written failing first and implemented one behavior
at a time.

Track 1, candidate gating: H1 through H11, H5a, H5b, H6a through H6c, and H17,
one exclusion reason at a time.

Track 2, evidence classification: M1 through M11, then I4 through I6. Class A
and its selection-equivalence proof come first, because Class A is the only
class that authorizes quarantine without corroboration and therefore the only
one that can restore CI health promptly.

Source resolution initially runs once per candidate without caching or batching.
H18 through H21 are deferred optimization tests and are not part of this gate.

The stage gate has three parts. Replaying the recorded production judgments
through the new gate must exclude every invalid candidate, with each exclusion
reason visible in `blockedTargets` and in the report. That frozen input is the
best regression corpus available because it already contains the failure
classes. No Class B or C candidate is quarantine-eligible in the initial version. A
synthesized same-run retry recovery with an artifact-derived name must reach
quarantine eligibility, proving the gate is not simply refusing everything.

### Stage 3 — Mutation matrix, live and non-destructive

B1 through B8, C1, C3, C12, D2, D8, F1 through F3, J1, J2, and J7. Comment
creation, editing, and deletion only. No closures.

The initial comment-pilot subset is complete on the fork: A1, B1, B2, B3, B7,
C1, J1, and J2 passed against live GitHub state. The remaining Stage 3 rows gate
later capabilities or unattended operation, not the bounded one-action pilot.

### Stage 4 — Destructive and closure

A2 through A8, E2, E5, F4, and J6. Closures and reopens, on fixture issues only.
Runs only after Stage 3 is fully green.

### Stage 5 — Quarantine, live

H12 through H16 against the fork. Start with a single-test batch derived through
the staged single-test option, then a two-test batch.

Then post-change validation end to end: H22 through H30. Run them in order,
because each later step assumes the earlier one holds. H26 and H29 are the two
that must not be skipped: H26 is the only check that distinguishes "excluded from
selection" from "deleted", and H29 is the only check that confirms the attribute
actually reached the target branch rather than trusting pull-request metadata.

### Stage 6 — Incremental stability

Run three consecutive cycles with a scripted mutation stream, including one
deliberate mid-cycle kill and one deliberate cleanup abort. Expand to a longer
soak only if these runs expose timing-dependent behavior or if unattended
operation is proposed.

Assert that invariants I5, I9, I10, and I11 and scenario K4 hold on every cycle
and that ledgers grow monotonically. Also assert that evidence classes are stable
across replays of unchanged evidence: a candidate must not drift between classes
when nothing about its evidence changed.

## 8. Go and no-go criteria

Go requires all of the following.

- Stage 0 green and reproducible after a full namespace teardown.
- One hundred percent of the capability-specific initial gate green.
- Across all staged runs, zero non-GET calls against `microsoft/aspire`, proven
  by the audit assertion rather than by inspection.
- Zero unexplained GitHub objects surviving cleanup across all runs.
- I2, I3, I4, I5, I9, I10, I11, and I19 hold on every soak cycle without
  exception.
- Every invalid quarantine candidate class deterministically excluded, with the
  reason visible in the report.
- No Class B or Class C candidate quarantine-eligible in the initial version,
  and every recorded evidence class carries its reason.
- At least one Class A candidate, with proven selection equivalence, driven all
  the way to a merged quarantine on the fork and confirmed on the target branch
  by H29.
- H26 green, meaning a quarantined test is still discoverable and was not
  deleted or renamed.
- J1 green, meaning a non-default pull-request judgment survives an unchanged
  replay, and J1a green, meaning a defaulted judgment is correctly not retained.
- J3 green, meaning a defaulted pull request remains visible as reviewed in the
  report.
- A documented rollback: the exact commands that reverse every operation type the
  executor can perform.

No-go on any single occurrence of the following.

- A write reaching `microsoft/aspire`.
- A double-post of the same comment.
- A close executed without a successful paired comment.
- A quarantine target silently dropped without a recorded reason.
- A quarantine recorded `completed` without observing the attribute on the target
  branch.
- A test deleted or renamed by a quarantine session.
- A ledger rewrite rather than an append.
- A cleanup failure requiring manual repair.

## 9. Residual risk after the artifacts look correct

These are the failure modes that survive a fully green matrix, and the scenario
that addresses each.

**Time-of-check races inside the execution window.** The dry run reports
eligible, and a maintainer closes the issue milliseconds later. The refetch
before mutation narrows the window but does not eliminate it. Scenario A2 should
be run with the close issued from a second thread while execution is in flight,
and must yield either a clean success or a clean refusal, never a mutation
against a closed issue.

**An overwrite that looks idempotent.** The idempotency key matches, the author
matches, and a human edited the body in between. Re-posting the frozen body
destroys their text while recording a clean success. Only the body-hash
precondition in B2 detects this. Without it every invariant still passes and data
is still lost.

**Snapshot-derived eligibility at execution time.** Labels are read during
collection and execution can be minutes later. C1 must mutate the label after
proposal generation and assert live re-derivation, otherwise it passes against a
snapshot replay and proves nothing.

**Actor identity drift.** `--shepherd-author` is operator-supplied. If it
disagrees with the token login, the shepherd treats foreign comments as its own
and the forgery defense in B4 evaporates. L5 must assert refusal, and every live
scenario should assert the two agree.

**Authorization replay across state directories.** Budget consumption lives in a
specific ledger. A fresh state directory plus a copied grant is a fresh budget
unless the directory is bound into the grant. C6, C9, and L10 must all pass;
any one alone is insufficient.

**Dependent close inversion.** The dangerous case is not a failed close after a
successful comment. It is a close whose comment was reconciled rather than
created. A5 must distinguish a terminal success from any terminal event.

**Partial execution reported from the proposal document.** With a budget of two
and a crash after the first, the state is half-applied. B8 asserts the report
reconciles to live state, which is the only way the operator sees the truth.

**Cleanup that reports success while leaking.** Covered above; the assertion must
cover comments and pull requests, not just issue state.

**Fixture bleed into a production cycle.** A state directory that recorded fork
fixtures carries fork issue numbers, and numbers collide across repositories.
Ledger entries must be repository-qualified, and a state directory bootstrapped
on the fork must refuse a production cycle outright.

**The fork is not the production repository.** Different labels, no production
bot population, different retention. A green fork run does not prove production
inventory behavior. Keep running production cycles in collect-and-dry-run mode
throughout Stages 3 to 6 and diff proposal counts and blocking reasons against
the staging expectations.

**Deterministic harness, nondeterministic model.** All of the above holds the
model fixed. The invalid quarantine candidates came from a real model run, so the
gate must live in the pipeline. Prompt changes are not a substitute for a
deterministic exclusion, and no scenario in this plan should be considered
covered by a prompt adjustment.
