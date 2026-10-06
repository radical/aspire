Repair scope and transient failures:
Work only on the adopted issue/PR's requested change, its review feedback, and
verified regressions caused by that change. Red CI is evidence to investigate,
not authorization to repair unrelated code. Establish the connection using
the diff, failure details and baseline/known-issue evidence before changing code.
An unknown cause requires diagnosis; a job name or failure outside a changed
file alone proves neither relevance nor irrelevance. Decline code repair for
confirmed unrelated failures, record the evidence and leave failing checks in
readiness. Never weaken or quarantine a test to obtain green CI.

Treat a suspected flaky/transient failure as a retry question, not a source
defect. For microsoft/aspire CI, the existing Auto rerun transient CI failures
workflow allows three reruns (four total attempts). Verify current-head run
attempts and retry progress from fresh evidence; do not assume every workflow
or fork is covered. Let eligible automatic reruns finish, then reassess their
results. Do not race the automation, reset its retry allowance, or start another
remote retry loop. Unknown retry status is not proof that retries are exhausted.
The native reasoner cannot execute tests. A worker may retry the affected test
locally once within its existing task budget, recording the exact command,
attempt count and outcome. A local pass does not prove remote CI is green.
Persistent unrelated failure requires an evidence-backed report, not a repair
of that unrelated defect. Remote workflow reruns remain outside this host's
capabilities; report a remaining rerun requirement without performing it.
