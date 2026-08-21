# CI Shepherd Assessment Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the collector-to-actor artifact pipeline with issue #19149, without allowing conversation-side analysis to override the generated judgment.

**Architecture:** Replay the frozen live snapshot through a focused evidence request, expansion, preparation, compact assessment, fresh assessor, finalizer, validator, and deterministic action proposal renderer. Treat validated `judgments.json` as the only decision source and `action-proposals.json` as the only source of proposed external effects.

**Tech Stack:** Python 3 standard library, existing `ci_shepherd` modules, `unittest`, JSON artifacts, GitHub GET APIs through `expand.py`, and isolated assessment agents.

---

## Scope and Persistence

This plan exercises one regression case from the approved full-cycle design:
`docs/superpowers/specs/2026-08-21-ci-shepherd-full-cycle-poc-design.md`.

The frozen source snapshot is:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-live-cycle-1/input.json
```

The replay writes only under:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-regression-19149/
```

Do not recollect GitHub state during the replay. The only network step is the
validated expansion of evidence IDs already present in the frozen snapshot.
Do not close or comment on #19149 until the final generated proposal is shown
and each external effect is approved separately.

`.ci-shepherd-build/` remains intentionally excluded by `.git/info/exclude`.
Do not force-add it. Commit plan or design files only after showing the user the
draft commit message.

## File Map

- Modify: `.ci-shepherd-build/scripts/ci_shepherd/actions.py`
  - Render a resolved `review-close` judgment as separate comment and close
    proposals.
  - Reject closure when the prepared state does not contain deterministic
    resolution evidence.
- Modify: `.ci-shepherd-build/scripts/propose_actions.py`
  - Generate all implemented proposal types from validated judgments.
- Modify: `.ci-shepherd-build/tests/test_actions.py`
  - Cover resolved closure rendering, separate approvals, and unsafe closure
    rejection.
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
  - Lock the artifact-pipeline testing contract into the prompt.
- Modify: `.ci-shepherd-build/SKILL.md`
  - Document the frozen-input replay process and prohibit out-of-band judgment
    substitution.
- Create in session artifacts: `ci-shepherd-regression-19149/`
  - Preserve every focused replay JSON and the final report/proposals.

### Task 1: Freeze the #19149 baseline

**Files:**
- Read: `ci-shepherd-live-cycle-1/input.json`
- Read: `ci-shepherd-live-cycle-1/agent-input.round-1.json`
- Create: `ci-shepherd-regression-19149/baseline.json`
- Create: `ci-shepherd-regression-19149/agent-input.json`

- [ ] **Step 1: Record the observed failing pipeline result**

Run:

```bash
SOURCE=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-live-cycle-1
REPLAY=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-regression-19149
mkdir -p "$REPLAY"
chmod 700 "$REPLAY"

jq '{
  issue: (.issues[] | select(.issueNumber == 19149)),
  recoveryRun: (
    .issues[]
    | select(.issueNumber == 19149)
    | .allowedEvidence[]
    | select(.id == "run:31211923676")
  )
}' "$SOURCE/agent-input.round-1.json" > "$REPLAY/baseline.json"
chmod 600 "$REPLAY/baseline.json"
```

Expected: `baseline.json` records `investigate`, `reviewRequired: true`, and
`run:31211923676` with `availability: partial`.

- [ ] **Step 2: Confirm the request planner omitted #19149**

Run:

```bash
jq '[.requests[] | select(.sourceIssueNumber == 19149)]' \
  "$SOURCE/evidence-requests.round-1.json"
```

Expected:

```json
[]
```

- [ ] **Step 3: Create a focused compact planner input**

Run:

```bash
jq '.issues |= map(select(.issueNumber == 19149))' \
  "$SOURCE/agent-input.json" > "$REPLAY/agent-input.json"
chmod 600 "$REPLAY/agent-input.json"
jq '{issueCount: (.issues | length), issueNumber: .issues[0].issueNumber}' \
  "$REPLAY/agent-input.json"
```

Expected:

```json
{
  "issueCount": 1,
  "issueNumber": 19149
}
```

### Task 2: Replay evidence planning and expansion

**Files:**
- Read: `.ci-shepherd-build/SKILL.md`
- Read: `ci-shepherd-regression-19149/agent-input.json`
- Create: `ci-shepherd-regression-19149/evidence-requests.json`
- Create: `ci-shepherd-regression-19149/input.json`
- Create: `ci-shepherd-regression-19149/expansion-errors.json`
- Create: `ci-shepherd-regression-19149/api-calls.jsonl`

- [ ] **Step 1: Run a fresh request-planning agent**

Give the agent only `.ci-shepherd-build/SKILL.md` and
`ci-shepherd-regression-19149/agent-input.json`. Use this instruction:

```text
Follow the Request-planning agent contract exactly. Review the complete
one-issue input and write only evidence-requests.json. Request only evidence
whose expansion can change issue #19149's disposition. Do not emit judgments,
access GitHub, or use prior conversation analysis.
```

Expected: the request document includes a `workflow-run` request for
`run:31211923676` with `sourceIssueNumber: 19149` and
`decisionGate: recovery`.

- [ ] **Step 2: Validate the request against the frozen snapshot**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/validate_requests.py \
  --input "$SOURCE/input.json" \
  --requests "$REPLAY/evidence-requests.json"
```

Expected: `valid`.

- [ ] **Step 3: Expand only the requested frozen evidence**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/expand.py \
  --input "$SOURCE/input.json" \
  --requests "$REPLAY/evidence-requests.json" \
  --output "$REPLAY/input.json" \
  --errors "$REPLAY/expansion-errors.json" \
  --audit "$REPLAY/api-calls.jsonl"
```

Expected: `run:31211923676` is `available`, completed successfully on `main`,
and no request outside issue #19149 appears in `api-calls.jsonl`.

### Task 3: Regenerate and assess the focused candidate

**Files:**
- Create: `ci-shepherd-regression-19149/assessment-input.all.json`
- Create: `ci-shepherd-regression-19149/assessment-input.json`
- Create: `ci-shepherd-regression-19149/agent-input.final.json`
- Create: `ci-shepherd-regression-19149/agent-judgments.json`
- Create: `ci-shepherd-regression-19149/judgments.json`
- Create: `ci-shepherd-regression-19149/report.md`

- [ ] **Step 1: Prepare the expanded snapshot**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/prepare.py \
  --input "$REPLAY/input.json" \
  --output "$REPLAY/assessment-input.all.json" \
  --max-bundle-records 25

jq '.issues |= map(select(.issueNumber == 19149))' \
  "$REPLAY/assessment-input.all.json" > "$REPLAY/assessment-input.json"
chmod 600 "$REPLAY/assessment-input.json"
```

Expected: the focused prepared issue includes the expanded successful run.

- [ ] **Step 2: Regenerate compact assessor input**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/compact.py \
  --prepared "$REPLAY/assessment-input.json" \
  --fingerprints "$SOURCE/fingerprints.jsonl" \
  --output "$REPLAY/agent-input.final.json"

jq '.issues[0] | {
  issueNumber,
  reviewRequired,
  defaultJudgment,
  resolutionEvidence,
  allowedEvidence
}' "$REPLAY/agent-input.final.json"
```

Expected: `defaultJudgment` is `blocking-build` / `review-close`,
`resolutionEvidence` cites `run:31211923676`, and the run is citable in
`allowedEvidence`.

- [ ] **Step 3: Run a fresh assessment agent**

Give the agent only `.ci-shepherd-build/SKILL.md` and
`ci-shepherd-regression-19149/agent-input.final.json`. Use this instruction:

```text
Follow the Fresh assessment-agent contract exactly. Process the complete
one-issue input once. Copy its deterministic default unless cited evidence
requires an allowed override. Write only agent-judgments.json. Do not access
GitHub or use prior conversation analysis.
```

Expected: `agent-judgments.json` preserves the `review-close` default.

- [ ] **Step 4: Finalize, validate, and render**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/finalize.py \
  --agent-input "$REPLAY/agent-input.final.json" \
  --agent-judgments "$REPLAY/agent-judgments.json" \
  --output "$REPLAY/judgments.json"

PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/validate.py \
  --prepared "$REPLAY/assessment-input.json" \
  --judgments "$REPLAY/judgments.json"

PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/render.py \
  --prepared "$REPLAY/assessment-input.json" \
  --judgments "$REPLAY/judgments.json" \
  --output "$REPLAY/report.md"
```

Expected: validation prints `valid`; `judgments.json` is the only decision
source for Task 4.

### Task 4: Add deterministic resolved-closure proposals

**Files:**
- Modify: `.ci-shepherd-build/tests/test_actions.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/actions.py`
- Modify: `.ci-shepherd-build/scripts/propose_actions.py`

- [ ] **Step 1: Write the failing resolved-closure tests**

Add tests that build a `review-close` judgment and a prepared issue with:

```python
{
    "candidateState": "resolved",
    "candidateAction": "recommend-close",
    "resolutionEvidence": {
        "runEvidenceId": "run:777",
    },
}
```

Assert the general proposal builder emits two separately approvable effects:

```python
self.assertEqual(
    ["create-comment", "close-issue"],
    [proposal["operation"] for proposal in result["proposals"]],
)
self.assertEqual("completed", result["proposals"][1]["closeReason"])
self.assertTrue(result["proposals"][1]["requiresSeparateApproval"])
self.assertEqual(
    result["proposals"][0]["actionId"],
    result["proposals"][1]["dependsOn"],
)
```

Add a second test where `candidateState` is `observing` and
`resolutionEvidence` is empty. Assert:

```python
with self.assertRaisesRegex(
    ValueError,
    "review-close requires deterministic resolution evidence",
):
    build_action_proposals(snapshot, prepared, judgments, "ankj")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v \
  test_actions.WatchActionTests.test_build_action_proposals_renders_resolved_review_close \
  test_actions.WatchActionTests.test_build_action_proposals_rejects_unresolved_review_close
```

Expected: FAIL because `build_action_proposals` does not exist.

- [ ] **Step 3: Implement the minimal proposal renderer**

Add `build_action_proposals(...)` as the public entry point. Preserve existing
watch behavior and add `review-close` handling that:

```python
if (
    prepared_issue.get("candidateState") != "resolved"
    or prepared_issue.get("candidateAction") != "recommend-close"
    or not prepared_issue.get("resolutionEvidence")
):
    raise ValueError(
        f"Issue {issue_number} review-close requires deterministic resolution evidence."
    )
```

Render a canonical `[automated]` status comment from the recommendation summary
and cited evidence. Emit a dependent close proposal with:

```python
{
    "operation": "close-issue",
    "closeReason": "completed",
    "requiresSeparateApproval": True,
    "dependsOn": comment_action_id,
    "expectedIssueState": "open",
}
```

The renderer must not inspect uncited issue prose or invent a different
disposition.

- [ ] **Step 4: Wire the CLI to the general renderer**

Replace the `build_watch_proposals` import/call in `propose_actions.py` with
`build_action_proposals`. Do not add GitHub mutation code.

- [ ] **Step 5: Run the focused and full prototype tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actions test_scripts

PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_*.py'
```

Expected: all tests pass.

### Task 5: Generate the actor output from the final judgment

**Files:**
- Create: `ci-shepherd-regression-19149/action-proposals.json`

- [ ] **Step 1: Generate proposals from validated artifacts only**

Run:

```bash
PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/propose_actions.py \
  --snapshot "$REPLAY/input.json" \
  --prepared "$REPLAY/assessment-input.json" \
  --agent-input "$REPLAY/agent-input.json" \
  --judgments "$REPLAY/judgments.json" \
  --shepherd-author ankj \
  --output "$REPLAY/action-proposals.json"
```

Expected: one comment proposal and one dependent `close-issue` proposal for
#19149. No GitHub write occurs.

- [ ] **Step 2: Present the generated effects separately**

Show the exact comment proposal, its cited evidence, and expected visible
result. Ask for approval for that comment only.

After an approved comment is observed, re-preflight the issue and separately
show the generated close proposal. Ask for approval for closure only.

Do not rewrite either proposal using conversation-side analysis.

### Task 6: Lock the artifact-pipeline testing protocol

**Files:**
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
- Modify: `.ci-shepherd-build/SKILL.md`

- [ ] **Step 1: Add a failing prompt-contract test**

Assert `SKILL.md` contains these requirements:

```python
self.assertIn("validated `judgments.json` is the only decision source", skill)
self.assertIn("Do not substitute conversation-side analysis", skill)
self.assertIn("`action-proposals.json` is the only source of external effects", skill)
self.assertIn("Locate the earliest incorrect artifact and replay from frozen input", skill)
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v \
  test_scripts.PrototypeScriptTests.test_skill_defines_artifact_pipeline_regression_protocol
```

Expected: FAIL because the protocol is not yet explicit.

- [ ] **Step 3: Document the protocol**

Add an `Artifact-pipeline regression protocol` section to `SKILL.md` that
states:

```text
Freeze source inputs and preserve every generated artifact. Inspect
intermediate artifacts only to locate information loss or transformation
errors. Validated judgments.json is the only decision source. Do not
substitute conversation-side analysis. action-proposals.json is the only
source of external effects. When an outcome is wrong, locate the earliest
incorrect artifact, change that stage or prompt, and replay from frozen input.
```

- [ ] **Step 4: Run the full prototype suite**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_*.py'
```

Expected: all tests pass.
