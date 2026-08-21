# CI Shepherd Thin POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and run a quickly adjustable, read-only CI issue assessment that emits evidence-linked JSON and deterministic Markdown queues.

**Architecture:** Reuse the existing incremental collector and bounded lifecycle bundles. Replace the legacy final report contract with a permissive POC judgment schema, then validate and render it without implementing production lifecycle or executor rules.

**Tech Stack:** Python 3 standard library, `unittest`, JSON, Markdown, existing GitHub GET-only collector.

---

### Task 1: Add the POC judgment contract

**Files:**
- Create: `.ci-shepherd-build/scripts/ci_shepherd/poc.py`
- Create: `.ci-shepherd-build/tests/test_poc.py`
- Modify: `.ci-shepherd-build/scripts/prepare.py`

- [ ] **Step 1: Write a failing contract test**

```python
judgments = {
    "schemaVersion": 1,
    "snapshotId": prepared["snapshotId"],
    "issues": [{
        "issueNumber": 12,
        "category": "flaky-test",
        "recommendations": [{
            "disposition": "review-quarantine",
            "target": {"kind": "test", "value": "Namespace.Type.Method"},
            "confidence": "medium",
            "summary": "The test failed in two independent runs.",
            "evidenceIds": ["run:100", "run:101"],
            "missingEvidence": [],
            "reassessWhen": "Another independent run executes the test."
        }]
    }]
}
validate_poc_judgments(prepared, judgments)
```

Also assert rejection of a missing issue, unknown vocabulary value, and
evidence outside the issue bundle.

- [ ] **Step 2: Verify the test fails**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_poc -v
```

Expected: FAIL because `ci_shepherd.poc` does not exist.

- [ ] **Step 3: Implement the minimal validator**

Use closed sets from the POC design. Require one record per open issue and at
least one recommendation. Validate target shape, confidence, nonempty summary
and reassessment condition, and evidence membership. Do not encode lifecycle
or executor policy.

- [ ] **Step 4: Add a stable snapshot ID to preparation**

```python
"snapshotId": (
    f"snapshot:{snapshot['repository']}:{snapshot['collectedAt']}"
),
```

Keep the existing bounded bundles and candidate metadata as agent context.

- [ ] **Step 5: Run tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_poc tests.test_lifecycle tests.test_scripts -v
```

Expected: PASS.

### Task 2: Render POC queues and simplify the prompt

**Files:**
- Modify: `.ci-shepherd-build/scripts/validate.py`
- Modify: `.ci-shepherd-build/scripts/render.py`
- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing CLI and queue tests**

Assert `validate.py --prepared ... --judgments ...` prints `valid`. Assert
Markdown contains:

```text
Investigate
Watch
Needs human
Quarantine review
Retry review
Rerun review
Closure review
No action
```

Queue membership must come from `disposition`, never summary prose.

- [ ] **Step 2: Verify the tests fail**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_scripts -v
```

- [ ] **Step 3: Implement POC validation and rendering**

Use:

```bash
python3 scripts/validate.py \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json"

python3 scripts/render.py \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json" \
  --output "$SCRATCH/report.md"
```

Render counts by category, disposition, and confidence, then one table per
queue with issue, target, summary, evidence IDs, missing evidence, and
reassessment condition.

- [ ] **Step 4: Replace the fresh-agent prompt**

The agent reads only `assessment-input.json`, processes batches of at most 10,
and writes `judgments.json`. Tell it to:

- Prefer `unknown` and `investigate` over unsupported certainty.
- Distinguish same-run reruns from independent runs.
- Surface missing positive execution coverage.
- Recommend quarantine/retry/rerun/closure only for human review.
- Emit multiple recommendations only when targets differ.
- Never access GitHub or perform writes.

- [ ] **Step 5: Run the complete prototype suite**

```bash
cd .ci-shepherd-build
TMPDIR="$PWD/tests/.tmp" \
  PYTHONPATH=scripts \
  python3 -m unittest discover -s tests -q
```

Expected: all tests PASS.

### Task 3: Run one watched live trial

**Files:**
- Create outside the repository: `input.json`, `assessment-input.json`,
  `judgments.json`, `report.md`, `progress.json`, and `api-calls.jsonl`

- [ ] **Step 1: Collect incrementally**

Use the latest valid private history with the existing bounded collector.
Record collection duration and GET count.

- [ ] **Step 2: Prepare bounded input**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 scripts/prepare.py \
  --input "$SCRATCH/input.json" \
  --output "$SCRATCH/assessment-input.json" \
  --max-bundle-records 25
```

- [ ] **Step 3: Run one fresh assessment agent**

Give it the simplified skill prompt and paths. Reuse the same agent for all
batches. Inspect `progress.json` after each reported batch; if one batch takes
materially longer than earlier batches, inspect that batch instead of waiting
indefinitely.

- [ ] **Step 4: Validate and render**

Run the Task 2 commands. Correct invalid JSON or unsupported citations without
weakening the validator.

- [ ] **Step 5: Evaluate the POC**

Record total time, collection GETs, queue counts, useful recommendations,
incorrect recommendations, missing recommendations, and concrete prompt or
projection changes for the next trial. Stop without performing GitHub writes.
