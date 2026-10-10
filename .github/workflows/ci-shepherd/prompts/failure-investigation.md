Investigate the pinned failure without assuming its reported classification is correct. A workflow \
failure can be infrastructure, a flaky-test candidate, a product-bug candidate or inconclusive. \
Transience is a separate conclusion; one failure is not proof of flakiness.
Verify the latest comparable occurrence on the same workflow, branch, lane/matrix, job and test. \
Report current unresolved failure, a later matching pass, or unknown/incomplete. No newer run \
means unresolved, not proof of regression or flakiness. Green unrelated jobs or lanes do not \
establish recovery. A skipped, quarantined or unexecuted test is not a passing baseline.
Locate the last passing comparable build/test. Verify exact before/after SHAs and inspect the \
intervening commits for candidate causes, separating correlation from confirmed root cause. \
If comparison is unavailable, say so explicitly. Account for pipeline, package/feed and \
external-service causes rather than assuming a code regression.
Reuse a matching test tracker before proposing a new one. Record exact canonical test name and \
workflow file, not just a title. Proposed comment_issue/create_issue/link_existing_tracker \
actions must carry target, draft title/body, evidence and justification. They are proposals only: \
they grant no write capability. Keep the original occurrence issue open and separate from an \
aggregate test tracker. No PR is required to report a failure or recommend follow-up.
