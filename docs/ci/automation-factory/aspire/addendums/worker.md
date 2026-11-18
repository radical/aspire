# Worker additional instructions

These instructions supplement the Worker prompt for `microsoft/aspire`.
They do not expand its scope, permissions, or approval boundaries.
If they conflict with the saved prompt, follow the saved prompt.

## Classify and prioritize test failures

When an admitted work item concerns a test failure, determine whether the
evidence supports a flaky test, a deterministic failure, or an unknown cause.
Inspect the exact test and comparable source runs, including attempt-one
results, later retries, and whether the same test actually executed. A skipped
test or a run that did not reach the test is not a pass. Do not call a test
flaky merely because it failed more than once; repeated failures without
comparable passes can indicate a deterministic regression. If evidence is
incomplete or conflicting, record the classification as unknown and ask for
help rather than guessing.

- Any unresolved rolling break inherits P0, including flaky failures.
  Its tracking issues, fix/mitigation/quarantine/disable PRs and release
  backports use P0 slots and review allowances. Reconcile stale priorities
  under the item claim before counting capacity.
- For a suspected or confirmed flaky test, use or update its canonical
  `flaky-test` work item and signature evidence; do not create a second queue or
  duplicate issue. Keep each occurrence tied to its source workflow, run,
  attempt, test signature, and commit.
- Order flaky-test work by descending frequency of distinct matching
  run-attempts in the shared 14-day signature window. Count a run-attempt only
  once for the same signature; multiple SHAs in one attempt do not add hits.
  Break ties by the older work item. This frequency ordering applies within the
  flaky-test list; it never outranks actionable rolling P0 work.
- Flakiness does not lower an unresolved rolling break below P0. Keep the
  classification and frequency evidence current; do not lower unrelated
  priorities or close issues merely to reorder the list.

## Windows and macOS reproduction

Use an existing relevant Aspire CI job through its normal investigation-branch
or PR path rather than requiring access to a developer's local Windows or macOS
machine or adding a new workflow. For #22116, use the existing `Tests / Run VS
Code extension unit tests (Windows)` job in `.github/workflows/tests.yml` to
capture the exact-case baseline and reproduce the affected test. Preserve the
termination assertions. Never dispatch, cancel, or rerun a `main` or
`release/*` rolling workflow. If the existing job cannot exercise the exact
case, report the limitation and keep the result unvalidated.
