## Diagnose, repair and validate

Diagnose -> reproduce -> fix -> verify the assigned failure. Read instructions, issue/PR,
logs, artifacts, source/diff. A product-bug route may repair a test defect.
Use ci-test-failures AND fix-flaky-test for suspected flakes/quarantine; read their
execution/access guidance. Missing skill: use checked-in/native guidance and report it.
Separate evidence from hypotheses.

Local runs are optional; OS mismatch selects approved remote execution.
Use reproduce-flaky-tests.yml; inspect live eligibility/inputs. Other CI edits/executions
need explicit scope. Prefer authenticated MCP log/artifact reads; inspect exposed
dispatch/status schemas. gh auth status is diagnostic, not a gate; absent shell auth,
check existing tools. Never invent methods, log in, copy tokens or bypass policy.
Reads/pushes do not prove Actions-write. Report blockers and manual commands.
Approval pause is not 403 or missing tool. No receipt means uncertain, not dispatched.
Reconcile before retry/restore; unresolved submission stops writes.

Require approved total dispatch/runner/iteration/time-CI budgets; request missing limits.
Run sequentially. Rebuild/repeat before/after a minimal fix with regression coverage.
Preserve lane traits/build/concurrency when broadening; include assigned quarantine,
exclude unrelated quarantine/outerloop. MTP filters follow --. Require named method AND
arguments per iteration/OS/SHA; eight aggregate passes for eight InlineData cases do NOT
prove tuple passes. Zero/skipped/unrelated/old-head results prove nothing. Missing exact
evidence means unknown/inconclusive, not fixed. Retention changes need separate approval.

Refresh authority before EVERY write/cleanup. Restore exact saved configuration; preserve
unrelated edits. Revocation stops restoration too. Report outstanding state/incidental CI.
Verify final diff/tested-head equivalence or disclose gaps. Override skills: no extra
branch/PR, disabling CI, reruns/cancellation, force push, unquarantining, diagnostic comments
or credential/permission changes. Controller reports. Startup drafts are not fixes; no
ready-for-review without validated repair/cleanup. Otherwise no-fix/inconclusive with
exact attempts/errors and blocked validation.
