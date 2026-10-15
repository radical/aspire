## Diagnose, repair and validate

Diagnose -> reproduce -> fix -> verify the exact assigned failure. Read instructions,
issue/PR, logs, artifacts, source and diff. A product-bug route may repair a test defect.
Use ci-test-failures for CI diagnosis AND fix-flaky-test for suspected flakes/quarantine;
read their execution/access/evidence guidance. Missing skill: read checked-in guidance or
report absence and continue native diagnosis. Separate evidence from hypotheses.

Compatible local runs are optional; OS mismatch selects approved remote execution.
Use reproduce-flaky-tests.yml for repeated tests. Inspect live eligibility/inputs first.
Other CI edits/executions need explicit scope, not inferred flaky-workflow permission.
Before diagnostic pushes inspect actual dispatch/status/log/artifact tool schemas.
gh auth status is diagnostic, not a gate: check existing authenticated exposed tools if
shell auth is absent. Never invent methods, log in, copy tokens or bypass policy denial.
Reads/pushes do not prove Actions-write. Report missing capability and manual commands.
Reconcile uncertain dispatch before retry/restore; unresolved submission stops writes.

Establish approved total dispatch/runner/iteration/time-CI budgets; request missing limits.
Run one at a time. Rebuild and repeat before/after a minimal fix with regression coverage.
Preserve lane traits/build/concurrency when broadening; include assigned quarantine cases,
exclude unrelated quarantine/outerloop. MTP filters follow --. Require exact theory-case
counts per iteration/OS/SHA; aggregate/zero/skipped/unrelated/old-head passes prove nothing.
Missing target evidence means inconclusive, not fixed; propose retention changes separately.

Refresh authority before EVERY write, including cleanup. Restore exact saved configuration,
preserve unrelated edits; revoked authority stops restoration too. Report outstanding state
and incidental CI effects. Verify final diff and tested/final-head equivalence or disclose gaps.
Override skills: no extra branch/PR, disabling CI, reruns/cancellation, force push,
unquarantining, diagnostic comments or credential/permission changes. Controller reports.
Startup drafts are not fixes: no ready-for-review without validated repair and cleanup.
Otherwise return no-fix/inconclusive with exact attempts/errors and blocked validation.
