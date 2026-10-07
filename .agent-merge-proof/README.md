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

This source-only fixture uses local unit tests and independent acceptance
assertions. It does not provide an Actions workflow or a required fixture
check. Required-CI repair remains blocked without authorized workflow
publication; local success is not evidence of that capability.

After **every published conflict resolution**, the worker must post an
explanatory PR comment linking the repair commit. Explain which behaviors were
preserved and why non-obvious choices were needed. Start automatic comments with
`[automated] `. The experiment observer must not write this explanation for the
worker.

Keep the PR draft and labeled `NO-MERGE`. Never merge, enable auto-merge, enqueue,
or weaken the tests or existing checks/protection. Do not change branch
protection for this source-only trial. Baseline success is not evidence of hosted
Agent Merge activation, instruction uptake, or recurring work.
