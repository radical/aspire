## Diagnose, repair and validate

Diagnose -> reproduce -> fix -> verify, not classification alone. Read repository instructions,
issue/PR, logs, artifacts, source and diff; establish the same in-scope failure.
A product-bug route can repair a verified test defect.

Invoke fix-flaky-test for suspected flaky/quarantined tests; otherwise use the applicable skill.
Use minimal changes and regression coverage. Missing skill: read checked-in guidance or report
its absence and continue native diagnosis. Separate evidence from hypotheses; one failure
does not prove flakiness.

Try local reproduction on a compatible OS, rebuild, verify and repeat flaky scenarios.
Record OS, commands, counts and before/after results. Check tests ran: skipped/zero-test/
unrelated passes prove nothing. Aspire MTP filters go after --; exclude unrelated quarantine/
outerloop tests but explicitly include an assigned quarantined test.

If scope permits, dispatch reproduce-flaky-tests.yml for the assigned test/branch/repository.
Inspect the live workflow; invent no inputs. Temporarily configure only project/filter/OS/
iterations within modest skill bounds; restore settings so the final diff is only the repair.
Refresh authority before pushes/dispatches. No existing CI reruns/cancellation or changes
to workflow permissions/credentials.

Override conflicting skill steps: no investigation branch/PR, disabling ci.yml, force push,
unquarantining or diagnostic comments. Controller reports. Give exact tool/OS/Actions/branch
blockers and a manual validation command.

Publish real changes with meaningful validation; disclose blocked CI in the draft, never claim
fully fixed. Otherwise report no-fix/inconclusive evidence, attempts and blockers. Empty
platform drafts are not fixes.
