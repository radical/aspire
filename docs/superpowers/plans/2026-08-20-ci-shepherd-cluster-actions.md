# CI Shepherd Cluster Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one substantive recommendation per repeated failure target while routing superseded issue records to closure review, and make the watch/investigate boundary explicit.

**Architecture:** The compact-input builder will derive an `actionCluster` from already-validated relationships. Exact same-test and same-error-code clusters retain the oldest issue as the durable owner; compatible repeated gh-aw issue-title clusters retain the newest occurrence. Superseded members receive deterministic review-close defaults, while the assessment prompt reasons only about the canonical member.

**Tech Stack:** Python 3.14, `unittest`, JSON compact handoff, Markdown skill prompt

---

### Task 1: Derive canonical action clusters

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/poc.py`
- Test: `.ci-shepherd-build/tests/test_poc.py`

- [x] **Step 1: Write failing tests**

Add assertions that:

```python
self.assertEqual(
    {
        "relationship": "same-test",
        "canonicalIssueNumber": 311,
        "memberIssueNumbers": [311, 312],
        "role": "canonical",
    },
    issues[311]["actionCluster"],
)
self.assertEqual("review-close", issues[312]["defaultJudgment"]["recommendations"][0]["disposition"])
```

Cover same-test, same-error-code, and same-workflow clusters. Verify workflow clusters require a compatible normalized issue title and use the newest issue as canonical.

- [x] **Step 2: Run focused tests and confirm failure**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q \
  test_poc.PocValidationTests.test_build_compact_poc_input_aggregates_compatible_same_test \
  test_poc.PocValidationTests.test_build_compact_poc_input_aggregates_equivalent_error_codes
```

Expected: failure because `actionCluster` does not exist and duplicate defaults remain quarantine/retry actions.

- [x] **Step 3: Implement action-cluster derivation**

Add a helper with this contract:

```python
def _build_action_contexts(
    issues: list[Mapping[str, Any]],
    related_issues: Mapping[int, list[dict[str, Any]]],
) -> dict[int, dict[str, Any] | None]:
    ...
```

Build connected components for:

```python
{"same-workflow-failure", "same-test", "same-error-code"}
```

Require matching normalized issue-title signatures for workflow components. Select the newest issue number for workflow clusters and the oldest for test/error clusters.

- [x] **Step 4: Route superseded defaults to closure review**

For `role == "superseded"`, replace the issue's default recommendation with:

```python
{
    "disposition": "review-close",
    "target": {"kind": "issue", "value": issue_number},
    "confidence": "medium",
    "summary": f"Review closure as a superseded duplicate of canonical issue #{canonical_issue_number}.",
    "evidenceIds": [f"issue:{issue_number}"],
    "missingEvidence": [],
    "reassessWhen": f"If canonical issue #{canonical_issue_number} is closed without resolving the shared failure target.",
}
```

Do not classify duplicate closure as recovery and do not require a later successful run.

- [x] **Step 5: Run focused tests**

Run the focused command from Step 2. Expected: all selected tests pass.

### Task 2: Make the assessment prompt cluster-first

**Files:**
- Modify: `.ci-shepherd-build/SKILL.md`
- Test: `.ci-shepherd-build/tests/test_scripts.py`

- [x] **Step 1: Add failing prompt-contract assertions**

Require the prompt to contain:

```python
self.assertIn("Evaluate `actionCluster` before evaluating individual issue rows", text)
self.assertIn("duplicate closure is not recovery", text)
self.assertIn("useful investigation work can happen now", text)
self.assertIn("only a future event can change the decision", text)
```

- [x] **Step 2: Run the focused prompt test**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_scripts.ScriptTests.test_skill_documents_offline_assessment_contract
```

Expected: failure because the cluster-first rules are absent.

- [x] **Step 3: Update the prompt**

Document these rules:

```text
Evaluate actionCluster before individual rows.
Only the canonical member may retain quarantine, retry, or investigation work.
Preserve review-close defaults for superseded members.
Duplicate closure is not recovery and does not require a green run.
Investigate when useful evidence gathering or reconciliation can happen now.
Watch only when current evidence is exhausted and only a named future event can change the decision.
```

- [x] **Step 4: Run the focused prompt test**

Expected: pass.

### Task 3: Regenerate and reassess the frozen fixture

**Files:**
- Create: session artifact `ci-shepherd-offline-trial-4/agent-input.json`
- Create: session artifact `ci-shepherd-offline-trial-4/agent-judgments.json`
- Create: session artifact `ci-shepherd-offline-trial-4/judgments.json`
- Create: session artifact `ci-shepherd-offline-trial-4/report.md`

- [x] **Step 1: Run all prototype tests**

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest discover -s .ci-shepherd-build/tests -q
```

Expected: all tests pass.

- [x] **Step 2: Generate a new compact handoff**

Run `compact.py` against the bot-inclusive frozen prepared evidence and write trial 4.

- [x] **Step 3: Run the existing offline assessor**

Require a fresh read of the updated `SKILL.md` and trial-4 input. Keep the run offline and write only `agent-judgments.json`.

- [x] **Step 4: Finalize and render**

Run `finalize.py`, `validate.py`, and `render.py`.

- [x] **Step 5: Verify the corrected queues**

Confirm:

```text
#18840 retains one quarantine review; #19143 is a duplicate closure candidate.
#18720 retains one retry review; #19144 and #19477 are duplicate closure candidates.
#19463 owns the Analyze CI Failure investigation; #19458 and #19459 are duplicate closure candidates.
No superseded member retains the same substantive action as its canonical issue.
```

### Task 4: Harden finalization and relationship precedence

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/poc.py`
- Test: `.ci-shepherd-build/tests/test_poc.py`

- [x] **Step 1: Add regressions for superseded overrides, overlapping relationships, and incompatible workflow aggregation**

- [x] **Step 2: Verify all three regressions fail for the expected reason**

- [x] **Step 3: Preserve superseded defaults during finalization**

- [x] **Step 4: Prefer exact test and error identities over workflow identity**

- [x] **Step 5: Exclude incompatible workflow failure shapes from recurrence aggregation**

- [x] **Step 6: Run the complete prototype suite**
