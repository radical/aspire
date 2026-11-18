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

Unavailable local Windows or macOS is not by itself a reason to ask the owner.
Use one of these authorized CI routes, checked against the repository first:

1. `.github/workflows/reproduce-flaky-tests.yml` when it supports the case.
   It is `workflow_dispatch` only, runs only in `microsoft/aspire`, and is
   configured by editing its `env` block on an owned investigation branch:
   `TEST_PROJECT`, `TEST_FILTER` (MTP filters after `--`), `TARGET_OSES`
   (`ubuntu-latest`, `windows-latest`, `macos-latest`), `RUNNERS_PER_OS` and
   `ITERATIONS_PER_RUNNER`. Read the current file on the target ref before
   use; inputs may change. Use the `fix-flaky-test` skill.
2. Otherwise a narrowly scoped `ci.yml` investigation. `ci.yml` has no manual
   trigger; it runs on `pull_request` to `main`/`release/**`. Open a draft
   investigation PR from an owned branch whose temporary commit restricts the
   test selection to the needed test(s)/project(s) and one target OS. Follow
   `.github/instructions/test-trigger-map.instructions.md` when touching
   selection or routing. Title it as an investigation; never mark it ready,
   and close it when done.

Before dispatch, verify the workflow's actual inputs, filters, permissions and
OS options, and that your identity can push the branch and dispatch or open
the PR. If not, record the exact denial as a blocker and ask the owner; do not
work around access.

- This is investigation-branch work, not a rolling rerun. The ban on
  rerunning, cancelling or dispatching `main`/`release/*` rolling workflows
  is unchanged.
- Keep temporary restrictions out of normal or final CI. A fix PR must not
  carry repro edits to `reproduce-flaky-tests.yml` or narrowed `ci.yml`
  selection, and must not weaken required validation.
- Keep claims, fenced intents, uncertain-effect reconciliation and budgets.
  Record each dispatch intent before the write; reconcile an unknown outcome
  by listing runs on the ref before any retry.
- Keep useful failure logs: link the run and failing job, and save the key
  excerpt in the item evidence record.
- Report the exact run URL, ref and head SHA, OS, test scope, iteration
  count and result. A passing narrow repro is not a full-CI pass and does not
  prove a fix.
- For #22116, the existing `Tests / Run VS Code extension unit tests
  (Windows)` job in `.github/workflows/tests.yml` remains the route for the
  exact-case baseline; preserve the termination assertions. If no route can
  exercise the exact case, report the limitation and keep the result
  unvalidated.
