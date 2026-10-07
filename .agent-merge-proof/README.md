# Disposable Agent Merge behavior fixture

This directory belongs only to a fork experiment. It is not production Aspire
code and must not be landed in the default branch or upstream.

Run the baseline from the repository root:

```shell
python3 -B -m unittest discover -s .agent-merge-proof -p 'test_*.py' -v
```

`canonical_service` trims surrounding whitespace, normalizes ASCII casing, and
maps known aliases. Unknown names remain normalized rather than disappearing.
`parse_retry_count` accepts only nonempty ASCII decimal digits, including leading
zeros. Signs, separators, whitespace, and Unicode digits are invalid.
`normalize_label` trims surrounding whitespace, lowercases, and preserves internal
spacing.

The dedicated workflow runs only for this fork and its disposable base, on
fixture/workflow changes. It runs on pushes to the disposable base/head as well
as pull requests: GitHub does not run `pull_request` workflows while a PR has
merge conflicts. Push runs can test each side during that interval. PR runs
check the exact head, not a synthetic merge. The check name is
`Agent Merge fixture`; verify its real check run and GitHub Actions app ID before
making that exact context required on the disposable base.

This dedicated fork-only lane does not use Aspire's ProjectGraph or conditional
test selector, so no production test-trigger-map edge is added.

After **every published conflict resolution**, the worker must post an
explanatory PR comment linking the repair commit. Explain which behaviors were
preserved and why non-obvious choices were needed. Start automatic comments with
`[automated] `. The experiment observer must not write this explanation for the
worker.

This app-only CI lane enables only CI repair. Review handling and conflict
repair remain disabled until separately authorized; merging stays disabled.

Keep the PR draft and labeled `NO-MERGE`. Never merge, enable auto-merge, enqueue,
or weaken the tests, workflow, required context, or protection. At each phase
boundary or timeout, disable Agent Merge and verify it is off before restoring
the disposable base's original protection. Verify restoration before continuing.
Baseline success is not evidence of hosted Agent Merge activation, instruction
uptake, recurring work, or remote required-check repair.
