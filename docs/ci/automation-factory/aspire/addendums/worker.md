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

- A deterministic bug on a P0 workflow inherits P0 priority. Use the shared
  prompt's P0 workflow definition. A deterministic test regression on another
  workflow follows that workflow's established priority.
- For a suspected or confirmed flaky test, use or update its canonical
  `flaky-test` work item and signature evidence; do not create a second queue or
  duplicate issue. Keep each occurrence tied to its source workflow, run,
  attempt, test signature, and commit.
- Order flaky-test work by descending frequency of distinct matching
  run-attempts in the shared 14-day signature window. Count a run-attempt only
  once for the same signature; multiple SHAs in one attempt do not add hits.
  Break ties by the older work item. This frequency ordering applies within the
  flaky-test list; it never outranks deterministic P0 work.
- A flaky test remains below deterministic P0 priority. Keep its explicit
  flaky classification and frequency evidence current as work is investigated.
  Do not lower another item's priority or close issues solely to reorder the
  list.
