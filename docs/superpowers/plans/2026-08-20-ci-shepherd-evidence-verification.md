# CI Shepherd Evidence Verification POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one bounded evidence-expansion pass and lightweight cross-run fingerprint history so CI shepherd trials can verify recurrence and recovery without performing root-cause investigation.

**Architecture:** Deterministic defaults provide preliminary triage. A request-planning agent emits one allowlisted expansion round, existing GET-only code expands the snapshot, and the existing assessment contract runs fresh over regenerated compact input. An append-only JSONL ledger carries normalized occurrences across trials; deep diagnosis is represented only as a bounded handoff.

**Tech Stack:** Python 3.14, `unittest`, JSON/JSONL artifacts, existing CI shepherd adaptive expansion

---

## POC Scope

Implement only:

- one expansion round;
- `issue-reference` and `workflow-run` requests;
- append-only fingerprint history without rollups or retention GC;
- bounded PR/run summaries in compact verifier input;
- closure verification and explicit-human-decision gating;
- one offline/live trial.

Defer:

- a second expansion round;
- 30/90-day retention tiers;
- test-result-level pass history;
- `source-check` and canonical-search requests;
- automatic investigation-session launching;
- broad schema migration or exhaustive test coverage.

### Task 1: Add append-only fingerprint history

**Files:**
- Create: `.ci-shepherd-build/scripts/ci_shepherd/poc_history.py`
- Create: `.ci-shepherd-build/scripts/fingerprints.py`
- Modify: `.ci-shepherd-build/scripts/compact.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/poc.py`
- Test: `.ci-shepherd-build/tests/test_poc_history.py`
- Test: `.ci-shepherd-build/tests/test_poc.py`

- [x] **Step 1: Add one focused history test**

Create a test that records two prepared issues sharing an exact normalized cause
but different issue numbers, then builds compact input containing only the newer
open issue.

```python
rows = collect_fingerprint_rows(prepared_with_issues(old_issue, current_issue))
compact = build_compact_poc_input(
    prepared_with_issues(current_issue),
    history_occurrences=rows,
)

self.assertEqual(
    2,
    compact["issues"][0]["historyOccurrenceSummary"]["independentRunCount"],
)
```

Add a second assertion proving repeated recording does not double-count the same
`fingerprint`, `runId`, `attempt`, and `issueNumber`.

- [x] **Step 2: Run the focused test and verify the missing module failure**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_poc_history
```

Expected: fail because `ci_shepherd.poc_history` does not exist.

- [x] **Step 3: Implement the minimal JSONL ledger**

Use this row shape:

```python
{
    "fingerprint": "cause:network-connection-reset",
    "issueNumber": 19500,
    "runId": 32300000001,
    "attempt": 1,
    "date": "2026-08-20",
    "job": "Tests / Hosting",
    "testName": None,
}
```

Choose the fingerprint in this order:

```python
tier2TestName -> tier3ErrorCode -> tier1CauseId
```

Skip rows without one of those stable identities. Normalize case and whitespace.
Append only rows whose identity tuple is not already present in the file. Do not
store logs, titles, bodies, or endpoint values.

- [x] **Step 4: Merge history into compact recurrence summaries**

Add an optional `history_occurrences` argument to `build_compact_poc_input()`.
Expose `historyOccurrenceSummary` on each issue and use it for thresholds only
when the fingerprint is exact. Keep `occurrenceSummary` unchanged so the report
can distinguish current issue evidence from retained history.

Add `--fingerprints PATH` to `compact.py`; omitting it preserves current behavior.

- [x] **Step 5: Run focused tests**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_poc_history test_poc.PocValidationTests
```

Expected: pass.

### Task 2: Surface expanded recovery evidence and harden escalation

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/poc.py`
- Test: `.ci-shepherd-build/tests/test_poc.py`

- [x] **Step 1: Add three regression tests**

Cover:

```python
# A compile failure with no explicit human decision is investigated.
self.assertEqual("investigate", disposition(compact_issue))

# A later directly referenced successful main run changes it to closure review.
self.assertEqual("review-close", disposition(expanded_compact_issue))

# An agent cannot turn a generic ownership question into ping-human.
self.assertNotEqual("ping-human", finalized_disposition)
```

The expanded run fixture must include `conclusion: "success"`, `headBranch:
"main"`, and a timestamp later than the issue's last occurrence.

- [x] **Step 2: Run the focused tests and verify all three fail**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q \
  test_poc.PocValidationTests.test_blocking_build_without_explicit_decision_is_investigate \
  test_poc.PocValidationTests.test_later_referenced_main_success_supports_closure \
  test_poc.PocValidationTests.test_finalizer_rejects_generic_human_escalation
```

Expected: current unconditional `blocking-build -> ping-human` behavior fails the
tests.

- [x] **Step 3: Project bounded PR and run summaries**

Extend `_project_allowed_evidence()` only for pull requests and workflow runs:

```python
{
    "id": "run:31211923676",
    "kind": "workflow-run",
    "availability": "available",
    "summary": {
        "conclusion": "success",
        "headBranch": "main",
        "createdAt": "2026-08-07T19:40:00Z",
    },
}
```

For pull requests include only `state`, `mergedAt`, `mergeCommitSha`, and base
branch. Do not expose bodies, logs, source, or arbitrary payload fields.

- [x] **Step 4: Apply minimal deterministic gates**

Change `blocking-build` defaults:

```python
if has_later_referenced_success:
    return "review-close"
if human_context and human_context["decisionRequired"]:
    return "ping-human"
return "investigate"
```

During finalization, discard a `ping-human` override unless
`humanContext.decisionRequired` is true. Preserve existing superseded-duplicate
protection.

- [x] **Step 5: Run focused tests**

Run the command from Step 2. Expected: pass.

### Task 3: Wire one request-planning round into the prompt

**Files:**
- Modify: `.ci-shepherd-build/SKILL.md`
- Test: `.ci-shepherd-build/tests/test_scripts.py`

- [x] **Step 1: Add prompt-contract assertions**

Require the skill to name these artifacts and constraints:

```text
evidence-requests.round-1.json
input.round-1.json
assessment-input.round-1.json
agent-input.round-1.json
one expansion round
issue-reference and workflow-run only
do not include preliminary judgments in verifier input
do not investigate root cause
```

- [x] **Step 2: Update the artifact flow**

Document this exact POC flow:

```text
input.json
  -> prepare.py
  -> compact.py
  -> request-planning agent writes evidence-requests.round-1.json
  -> validate_requests.py
  -> expand.py writes input.round-1.json
  -> prepare.py writes assessment-input.round-1.json
  -> compact.py writes agent-input.round-1.json
  -> fresh assessment agent writes agent-judgments.round-1.json
  -> finalize.py / validate.py / render.py
```

The request planner may select only partial or not-enriched evidence IDs already
listed under `allowedEvidence`. It must use the existing
`EVIDENCE_REQUEST_DECISION_GATES` vocabulary and emit no judgments.

The final assessment uses the existing judgment contract, reasons only about
issues named in the expansion manifest, and copies all other defaults. It must
emit an investigation handoff rather than diagnosing a test or product failure.

- [x] **Step 3: Run the prompt test**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q \
  test_scripts.ScriptTests.test_skill_documents_offline_assessment_contract
```

Expected: pass.

### Task 4: Run the two-stage POC trial

**Artifacts:**
- Create: session artifact `ci-shepherd-offline-trial-7/fingerprints.jsonl`
- Create: session artifact `ci-shepherd-offline-trial-7/agent-input.json`
- Create: session artifact `ci-shepherd-offline-trial-7/evidence-requests.round-1.json`
- Create: session artifact `ci-shepherd-offline-trial-7/input.round-1.json`
- Create: session artifact `ci-shepherd-offline-trial-7/assessment-input.round-1.json`
- Create: session artifact `ci-shepherd-offline-trial-7/agent-input.round-1.json`
- Create: session artifact `ci-shepherd-offline-trial-7/agent-judgments.round-1.json`
- Create: session artifact `ci-shepherd-offline-trial-7/judgments.json`
- Create: session artifact `ci-shepherd-offline-trial-7/report.md`

- [x] **Step 1: Run only the relevant prototype tests**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_poc_history test_poc test_scripts
```

Expected: pass.

- [x] **Step 2: Build the baseline history and compact handoff**

Use the frozen bot-inclusive `input.json`, `assessment-input.json`, and
`related-issues.json`. Record fingerprints, then regenerate compact input with
`--fingerprints`.

- [x] **Step 3: Run the request-planning agent**

The agent reads the updated skill and baseline compact input once, writes only
`evidence-requests.round-1.json`, stays GET-free, and requests at most 25 partial
issue or run references.

Validate the request document before expansion.

- [x] **Step 4: Expand and regenerate verifier input**

Run the existing `expand.py`, then `prepare.py` and `compact.py` against
`input.round-1.json`. Confirm:

```text
agent-input.round-1.json contains no agent-judgments or provisional disposition fields
```

- [x] **Step 5: Run a fresh assessment agent and render**

The fresh agent reads only the current skill and `agent-input.round-1.json`.
Finalize, validate, and render the report with existing scripts.

- [x] **Step 6: Apply the POC go/no-go checks**

Confirm:

```text
#19149 is review-close, not ping-human.
No generic ownership question produces ping-human.
At least one disposition changed because of a cited expanded evidence ID.
No quarantine or retry action is duplicated across one action cluster.
Expansion made no GitHub writes and stayed within 25 requests.
The second assessment did not contain root-cause hypotheses.
```

If no disposition changes, stop rather than adding more infrastructure: inspect
which needed facts are still absent and revise the projection or request rules.
