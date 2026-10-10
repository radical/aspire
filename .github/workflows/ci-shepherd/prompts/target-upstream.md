Investigate and repair one bounded batch of ordinary current-PR CI or review feedback. The shared \
repair-scope policy limits code changes to the adopted request and regressions caused by it; \
unrelated failures and suspected flakes do not authorize repairs. Failed job names alone are not a \
diagnosis or unsupported scope. Unknown failures require worker diagnosis of logs, artifacts and \
annotations before changing code. Repair only a verified cause; report a concrete human-only \
blocker when one exists. Infrastructure or cancellation-only failures require waiting/rerun, not an \
artificial code change or human handoff. For intentional generated workflow changes use pinned \
gh-aw v0.89.17. Never edit labeler workflows, change authentication, broaden permissions or weaken \
tests. Preserve the existing action-lock policy baseline; never bypass action-pin restrictions.
