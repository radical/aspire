# CI Shepherd Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce bounded, producer-aware, read-only CI lifecycle recommendations that distinguish investigation, waiting, human review, and closure-review candidates without permitting GitHub writes.

**Architecture:** Extend deterministic collection with producer and ledger metadata, then add a preparation stage that scans the complete snapshot and emits issue-scoped candidate bundles. The assessment agent may only choose from each candidate's allowed downgrade actions; validation and Markdown queue rendering remain deterministic.

**Tech Stack:** Python 3 standard library, `unittest`, GitHub CLI GET-only REST access, JSON, Markdown.

---

### Task 1: Parse producer and ledger contracts

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/signals.py`
- Modify: `.ci-shepherd-build/tests/test_signals.py`

- [ ] **Step 1: Add failing parser tests**

Add tests for the four-column body ledger, the five-column #19149 ledger,
annotated triggering merges, unrecognized tables, `**Type:**` formatting, and
run-marker comments:

```python
signals = extract_issue_signals(
    19149,
    "issue:19149",
    "https://github.com/microsoft/aspire/issues/19149",
    FIVE_COLUMN_BODY,
    "microsoft/aspire",
)
self.assertEqual("occurrences-v2", signals.occurrence_ledger.schema)
self.assertTrue(signals.occurrence_ledger.complete)
self.assertEqual(19090, signals.occurrences[0].pull_request)
```

- [ ] **Step 2: Run the tests and confirm failure**

Run:

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_signals -v
```

Expected: failures because `occurrence_ledger` and the broader table parser do
not exist.

- [ ] **Step 3: Implement ledger metadata**

Add immutable `OccurrenceLedger` metadata with:

```python
source: str
schema: str | None
schema_recognized: bool
source_record_count: int
parsed_row_count: int
complete: bool
```

Recognize both Aspire table schemas. Require every source row to parse before
setting `complete: true`. Accept a `#123 (annotation)` triggering-merge cell but
retain only issue number `123`.

- [ ] **Step 4: Run the parser tests**

Run the command from Step 2.

Expected: all `tests.test_signals` tests pass.

### Task 2: Attach producer, ledger, and episode metadata

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Modify: `.ci-shepherd-build/scripts/collect.py`
- Modify: `.ci-shepherd-build/tests/test_collector.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Add failing collector tests**

Cover:

```python
self.assertEqual("ci-failure-cause", issue["producer"])
self.assertEqual("body-table", issue["ledger"]["source"])
self.assertFalse(issue["episodesComplete"])
```

For `automation-broken`, assert that run-marker comments form the ledger and
that failed comment collection makes it incomplete.

- [ ] **Step 2: Run focused tests and confirm failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_collector tests.test_scripts -v
```

- [ ] **Step 3: Implement producer-aware metadata**

Classify only from body markers, labels, and the dashboard footer. Preserve
`autoclose` as `true`, `false`, or `null`. Add `episodesComplete` and do not
represent disabled timeline collection as authoritative episode history.

- [ ] **Step 4: Run focused tests**

Expected: all collector and script tests pass.

### Task 3: Build bounded assessment candidates

**Files:**
- Create: `.ci-shepherd-build/scripts/ci_shepherd/lifecycle.py`
- Create: `.ci-shepherd-build/scripts/prepare.py`
- Create: `.ci-shepherd-build/tests/test_lifecycle.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Add failing lifecycle tests**

Pin #19149-style resolved evidence, recurrent issues, tracking issues,
dashboard issues, empty ledgers, and a 178-record scoped issue:

```python
assessment = prepare_assessment(snapshot, max_bundle_records=25)
candidate = candidate_for(assessment, 19149)
self.assertEqual("recommend-close", candidate["candidateAction"])
self.assertTrue(candidate["approvalRequired"])
self.assertFalse(candidate["automationEligible"])
self.assertLessEqual(len(candidate["evidenceBundle"]), 25)
```

- [ ] **Step 2: Confirm failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_lifecycle -v
```

- [ ] **Step 3: Implement preparation**

The module must:

```python
def prepare_assessment(
    snapshot: Mapping[str, object],
    *,
    max_bundle_records: int = 25,
) -> dict[str, object]:
    ...
```

Scan all issue-scoped evidence for blockers, choose a conservative candidate,
build `allowedActions`, compact the evidence bundle, and emit a deterministic
completeness proof. `prepare.py` writes the private `assessment-input.json`.

- [ ] **Step 4: Run lifecycle and script tests**

Expected: all targeted tests pass.

### Task 4: Enforce candidate authority and render queues

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Modify: `.ci-shepherd-build/scripts/validate.py`
- Modify: `.ci-shepherd-build/scripts/render.py`
- Modify: `.ci-shepherd-build/tests/test_models.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Add failing validation and rendering tests**

Require `recommend-close`, reject agent action upgrades, and pin deterministic
queue membership:

```python
with self.assertRaisesRegex(ValidationError, "outside allowedActions"):
    validate_assessment_report(assessment, upgraded_report)

markdown = render_markdown(snapshot, report, assessment=assessment, snapshot_path=path)
self.assertIn("## Approval needed", markdown)
```

- [ ] **Step 2: Confirm failure**

Run:

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_models tests.test_scripts -v
```

- [ ] **Step 3: Implement validation**

Add `recommend-close` as an advisory action. `validate.py` accepts
`--assessment`; when supplied, it rejects any state/action pair outside the
candidate's allowed downgrade set. Existing executable close actions remain
high risk.

- [ ] **Step 4: Implement deterministic queues**

`render.py --assessment` derives Approval, Investigation, Human, Waiting,
Unchanged, and Data-quality sections from validated decisions and candidate
metadata.

- [ ] **Step 5: Run focused tests**

Expected: all model and script tests pass.

### Task 5: Rewrite the assessment contract and documentation

**Files:**
- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
- Modify: `docs/superpowers/specs/2026-08-17-ci-shepherd-design.md`
- Modify: `docs/superpowers/plans/2026-08-17-ci-shepherd.md`

- [ ] **Step 1: Add failing prompt contract assertions**

Require these phrases:

```python
required = (
    "assessment-input.json",
    "may downgrade but never upgrade",
    "producer contract",
    "empty or unrecognized ledger is a blocker",
    "recommend-close is advisory",
    "Never write to GitHub",
)
```

- [ ] **Step 2: Confirm failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_scripts -v
```

- [ ] **Step 3: Update the workflow**

The coordinator runs `prepare.py` after the newest expansion snapshot. The
fresh agent reads only `assessment-input.json` and writes `report.json`.
Validation and rendering receive both the full snapshot and assessment.

Remove the requirement that the agent enumerate every scoped evidence record.
Keep GET-only and read-only guarantees.

- [ ] **Step 4: Update the original design and plan**

Document producer contracts, ledger completeness, advisory closure, and the
future write contract. Correct Task 7 and Task 9 expectations for #19149,
#18784, and `autoclose:false`.

- [ ] **Step 5: Run prompt tests**

Expected: all script tests pass.

### Task 6: Verify and run a watched fresh trial

**Files:**
- No production file changes expected.
- Create private artifacts under the session artifact directory.

- [ ] **Step 1: Run the complete prototype suite**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 2: Launch a fresh assessment agent**

Run deterministic collection and preparation, then start one fresh agent with
the resulting `assessment-input.json`. Do not reuse a prior assessment agent.

- [ ] **Step 3: Watch durable progress**

Inspect `progress.json` after every stage. Treat three minutes without an
update as a stall. Preserve partial artifacts and resume from the latest
completed deterministic stage rather than restarting collection.

- [ ] **Step 4: Validate and inspect the report**

Validate with the full snapshot plus assessment, render Markdown, and compare:

- action and queue distribution;
- number of templated decisions;
- #19149 disposition;
- producer and ledger blockers;
- GET count and elapsed stage times;
- actual reuse counts.

- [ ] **Step 5: Record findings**

Report concrete remaining failure modes. Do not enable GitHub writes or weaken
validation to make the trial pass.
