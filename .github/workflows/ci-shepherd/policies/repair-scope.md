Repair only the adopted issue/PR's requested change, reviews and verified
regressions. Establish relevance using diff, failure and baseline/known-issue
evidence. Red CI, a job name or an unchanged file proves neither cause nor
irrelevance. Diagnose unknown failures; report confirmed unrelated failures
without repairing them or hiding red checks. Never weaken/quarantine tests.

Suspected flakiness is a hypothesis, not proof of a defect or irrelevance.
Repair verified in-scope test defects using fix-flaky-test.
Aspire's Auto rerun transient CI failures allows three reruns (four total
attempts); verify current-head coverage, attempts and progress. Let eligible
automatic reruns finish. Never race/reset that allowance or start a retry loop;
unknown retry status does not mean exhausted retries.

Native reasoners cannot test. Cloud workers may repeat focused tests within
their budget, recording commands, counts and outcomes. Authorized focused
reproduction dispatches stay on the assigned branch with temporary settings
restored in the final diff; they are not reruns of existing CI. Local passes
do not prove remote CI green. Host workflow reruns remain unsupported:
report their necessity without executing them.
