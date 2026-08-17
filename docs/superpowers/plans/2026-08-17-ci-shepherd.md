# CI Failure Shepherd Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a manually invoked, read-only user-level Copilot skill that collects authoritative Aspire CI issue evidence and produces validated local JSON and Markdown action reports.

**Architecture:** A dependency-free Python package shells out to the authenticated `gh api` CLI, normalizes GitHub facts, and atomically stores each collection run. The `ci-shepherd` skill guides the agent to reason only from that snapshot, write one decision per open issue, and pass those decisions through a deterministic validator/reporter before updating the latest-run pointer.

**Tech Stack:** Copilot user skills, Python 3.14 standard library, GitHub CLI REST API, `unittest`, JSON, Markdown.

---

## File map

- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/SKILL.md`: invocation contract, reasoning protocol, evidence rules, and concise chat output.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/models.py`: schema enums, normalization helpers, and input/report validation.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/github.py`: read-only `gh api` subprocess client, pagination, retry, and error classification.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/collector.py`: issue union, timeline/reference/run/PR/commit enrichment, log excerpts, and ownership evidence.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/storage.py`: run layout, manifests, atomic JSON writes, and latest-run pointer.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/reporter.py`: decision validation, previous-run comparison, Markdown rendering, and report finalization.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/collect.py`: collector CLI.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/finalize.py`: report validation/finalization CLI.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/verify.py`: independent inventory, corpus, report, and GET-only audit.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/install.py`: staged personal-skill installation, backup, and rollback guidance.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_models.py`: schema and high-risk safety tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_github.py`: subprocess, pagination, retry, and fatal/partial failure tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_collector.py`: inventory, reference, timeline, log, and ownership fixture tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_storage.py`: atomic run and latest pointer tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_reporter.py`: report completeness, comparison, ordering, and Markdown tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_corpus.py`: executable named-case constraints.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_install.py`: collision, backup, discovery, and removal guidance tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/`: compact GitHub API responses for deterministic tests.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/api-map.json`: endpoint-to-fixture map for deterministic CLI runs.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/input.json`: normalized evidence for the named audited cases.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/expected-report.json`: valid decisions and relationships for the named corpus.
- `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/expected-decisions.json`: mandatory decision constraints for the audited Aspire cases.

### Task 1: Define stable schemas and validation

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/__init__.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/models.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_models.py`

- [ ] **Step 1: Write failing schema tests**

Create tests that pin enum values, stable JSON ordering, one decision per open issue, evidence reference integrity, and the rule that high-risk actions cannot be high-confidence without current primary-source evidence:

```python
class ModelTests(unittest.TestCase):
    def test_report_requires_one_decision_per_open_issue(self):
        snapshot = make_snapshot(open_issue_numbers=[10, 11])
        report = make_report(decisions=[make_decision(10)])
        with self.assertRaisesRegex(ValidationError, "missing decisions.*11"):
            validate_report(snapshot, report)

    def test_high_risk_action_requires_current_primary_evidence(self):
        snapshot = make_snapshot(open_issue_numbers=[10])
        report = make_report(decisions=[
            make_decision(
                10,
                state="resolved",
                action="close",
                confidence="high",
                evidence=[{"kind": "previous-report", "id": "10"}],
            )
        ])
        with self.assertRaisesRegex(ValidationError, "current primary-source"):
            validate_report(snapshot, report)

    def test_close_requires_fix_or_recovery_and_post_fix_green_run(self):
        snapshot = make_snapshot(open_issue_numbers=[10])
        report = make_report(decisions=[
            make_decision(
                10,
                state="resolved",
                action="close",
                confidence="high",
                evidence=[evidence("issue:10:comment:5", "issue-comment")],
            )
        ])
        with self.assertRaisesRegex(ValidationError, "close requires.*verification"):
            validate_report(snapshot, report)

    def test_evidence_ids_are_composite_and_resolve_exactly(self):
        snapshot = make_snapshot(open_issue_numbers=[10])
        snapshot["evidence"] = {
            "run:42": {"kind": "workflow-run", "collectedAt": FIXED_NOW},
            "run:42:job:7": {"kind": "workflow-job", "collectedAt": FIXED_NOW},
        }
        report = make_report(decisions=[
            make_decision(10, evidence=[{"id": "job:7", "kind": "workflow-job"}])
        ])
        with self.assertRaisesRegex(ValidationError, "unknown evidence id"):
            validate_report(snapshot, report)

    def test_stable_json_sorts_keys_and_ends_with_newline(self):
        self.assertEqual(stable_json({"b": 1, "a": 2}), '{\n  "a": 2,\n  "b": 1\n}\n')
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_models.py' -v
```

Expected: FAIL because `ci_shepherd.models` does not exist.

- [ ] **Step 3: Implement schema constants and validators**

Define:

```python
ISSUE_KINDS = frozenset({"incident", "root-cause", "tracker", "transient"})
STATES = frozenset({
    "observing", "actionable", "needs-human", "fix-in-progress",
    "awaiting-verification", "resolved", "regression", "duplicate",
    "insufficient-evidence",
})
ACTIONS = frozenset({
    "wait", "investigate", "fix", "ping-human", "merge-duplicate",
    "close", "open-regression",
})
CONFIDENCE = frozenset({"high", "medium", "low"})
RELATIONSHIPS = frozenset({
    "exact-duplicate", "probable-duplicate", "canonical-tracker", "fixed-by",
    "regression-of", "supersedes", "same-incident", "related",
})
HIGH_RISK_ACTIONS = frozenset({"close", "merge-duplicate", "open-regression"})
PRIMARY_EVIDENCE_KINDS = frozenset({
    "issue-event", "issue-comment", "workflow-run", "workflow-job",
    "workflow-log", "pull-request", "commit", "source-path", "codeowners",
})
VALID_STATE_ACTIONS = {
    "observing": {"wait"},
    "actionable": {"investigate", "fix"},
    "needs-human": {"ping-human"},
    "fix-in-progress": {"wait"},
    "awaiting-verification": {"wait"},
    "resolved": {"close"},
    "regression": {"open-regression"},
    "duplicate": {"merge-duplicate"},
    "insufficient-evidence": {"wait", "investigate", "ping-human"},
}
```

Implement `ValidationError`, `stable_json(value)`, `validate_snapshot(snapshot)`,
and `validate_report(snapshot, report)`. Validation checks schema version,
required fields, enum membership, unique open issue decisions, evidence IDs
resolving to snapshot records, bounded `nextCondition`, owner reasons, and
high-risk evidence rules.

Evidence IDs are composite and globally unique:

```text
issue:{number}
issue:{number}:comment:{comment_id}
issue:{number}:event:{event_id}
run:{run_id}
run:{run_id}:attempt:{attempt}:job:{job_id}
run:{run_id}:attempt:{attempt}:job:{job_id}:log
run:{run_id}:check:{check_run_id}:annotation:{annotation_id}
pr:{number}
commit:{full_sha}
source:{percent_encoded_path}
codeowners:{percent_encoded_path}:{line_number}
```

Every evidence record carries `kind`, `url`, `collectedAt`, `availability`,
and its normalized factual payload. Action-specific validation requires:

- `close`: a current merged-fix or recovery record, a current post-fix green
  workflow run, and no newer contradictory failing run.
- `merge-duplicate`: a current canonical issue record plus an
  `exact-duplicate` relationship supported by a shared deterministic marker or
  matching normalized failure facts.
- `open-regression`: a current failing run, a prior resolved issue episode,
  and evidence that the normalized cause matches the resolved episode.

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2.

Expected: all model tests PASS.

### Task 2: Build the read-only GitHub API client

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/github.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_github.py`

- [ ] **Step 1: Write failing client tests**

Use an injected subprocess runner and fake clock. Cover:

```python
def test_paginated_get_flattens_array_pages():
    runner = FakeRunner(pages=['[{"number": 1}]', '[{"number": 2}]', '[]'])
    client = GitHubClient(runner=runner, sleep=lambda _: None)
    self.assertEqual(client.get_pages("repos/o/r/issues"), [{"number": 1}, {"number": 2}])
    self.assertIn("page=2", runner.calls[1])

def test_keyed_pagination_retains_second_jobs_page():
    runner = FakeRunner(pages=[
        '{"total_count": 2, "jobs": [{"id": 1}]}',
        '{"total_count": 2, "jobs": [{"id": 2}]}',
        '{"total_count": 2, "jobs": []}',
    ])
    jobs = GitHubClient(runner=runner, sleep=lambda _: None).get_pages(
        "repos/o/r/actions/runs/42/jobs?per_page=1",
        key="jobs",
    )
    self.assertEqual([job["id"] for job in jobs], [1, 2])

def test_retryable_server_failure_retries_then_succeeds():
    runner = FakeRunner(
        failures=[GhFailure(1, "HTTP 502")],
        stdout='{"ok": true}',
    )
    self.assertEqual(GitHubClient(runner=runner, sleep=lambda _: None).get("rate_limit"), {"ok": True})
    self.assertEqual(len(runner.calls), 2)

def test_authorization_failure_is_not_retried():
    runner = FakeRunner(failures=[GhFailure(1, "HTTP 403: Resource not accessible")])
    with self.assertRaisesRegex(GitHubApiError, "authorization"):
        GitHubClient(runner=runner, sleep=lambda _: None).get("repos/o/r/actions/runs/1")
    self.assertEqual(len(runner.calls), 1)

def test_secondary_rate_limit_honors_retry_after_with_bound():
    runner = FakeRunner(
        responses=[
            response(403, headers={"retry-after": "2"}, body={"message": "secondary rate limit"}),
            response(200, body={"ok": True}),
        ]
    )
    sleeps = []
    result = GitHubClient(runner=runner, sleep=sleeps.append, max_retry_delay=60).get("rate_limit")
    self.assertEqual(result, {"ok": True})
    self.assertEqual(sleeps, [2])

def test_all_requests_are_pinned_gets():
    runner = FakeRunner(stdout='{"ok": true}')
    GitHubClient(runner=runner).get("repos/o/r")
    command = runner.calls[0]
    self.assertIn("--method", command)
    self.assertIn("GET", command)
    self.assertIn("--hostname", command)
    self.assertIn("github.com", command)
    self.assertNotIn("--field", command)
    self.assertNotIn("--input", command)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_github.py' -v
```

Expected: FAIL because `ci_shepherd.github` does not exist.

- [ ] **Step 3: Implement the client**

Implement:

```python
class GitHubClient:
    def __init__(
        self,
        runner=subprocess.run,
        sleep=time.sleep,
        max_attempts=3,
        max_retry_delay=60,
    ):
        self._runner = runner
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_retry_delay = max_retry_delay

    def get(self, endpoint, *, accept=None):
        return json.loads(self._request(endpoint, accept=accept).body.decode())

    def get_pages(self, endpoint, *, key=None, accept=None):
        items = []
        for page in itertools.count(1):
            payload = self.get(with_query(endpoint, page=page, per_page=100), accept=accept)
            page_items = payload[key] if key else payload
            items.extend(page_items)
            if len(page_items) < 100:
                return items

    def get_text(self, endpoint, *, max_bytes=200_000):
        return self._stream_request(endpoint, max_bytes=max_bytes)
```

`_request` must always execute `gh api --method GET --hostname github.com
--include`, set `GH_PAGER=cat`, and add:

```text
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

Parse status and headers separately from the JSON body. Classify
authentication/authorization, not-found/expired, primary rate-limit, secondary
rate-limit, transient server, malformed JSON, and generic errors. Honor
`Retry-After`; otherwise use `X-RateLimit-Reset` when remaining is zero. If the
required wait exceeds `max_retry_delay`, fail explicitly as
`rate-limit-exhausted` instead of sleeping indefinitely. Retry transient 5xx
responses with one- and two-second delays.

`get_pages(endpoint, key=None)` manually advances `page=N`, supporting both
array responses and keyed arrays such as `jobs`, `workflow_runs`, `artifacts`,
and `check_runs`. Each endpoint gets a page-two retention test. Error objects
expose `category`, `endpoint`, `status`, `headers`, `retryable`, `attempts`, and
sanitized stderr. Append every invocation's method, endpoint, status, and
timestamp to the run's `api-calls.jsonl`; never record headers or tokens.

- [ ] **Step 4: Run the client tests**

Expected: all client tests PASS.

### Task 3: Collect and normalize the issue inventory

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_collector.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/issues-ci-failure-cause.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/issues-automation-broken.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/timeline-reopen.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/api-map.json`

- [ ] **Step 1: Write failing inventory and timeline tests**

Tests must prove:

```python
def test_open_issue_union_deduplicates_an_issue_with_both_labels():
    client = FixtureClient({
        issues_endpoint("ci-failure-cause", "open"): [issue(10), issue(11)],
        issues_endpoint("automation-broken", "open"): [issue(11), issue(12)],
    })
    result = Collector(client, "microsoft/aspire", now=FIXED_NOW).collect_inventory()
    self.assertEqual([item["number"] for item in result.open_issues], [10, 11, 12])

def test_closed_lookback_keeps_explicit_old_reference():
    result = collect_fixture_with_old_closed_issue_referenced_by_open_issue()
    self.assertIn(5, [item["number"] for item in result.supporting_issues])

def test_timeline_normalizes_close_and_reopen_episodes():
    episodes, warnings = normalize_timeline(
        issue_created_at="2026-07-01T00:00:00Z",
        events=load_fixture("timeline-reopen.json"),
    )
    self.assertEqual(
        episodes,
        [{"openedAt": "2026-07-01T00:00:00Z", "closedAt": "2026-07-02T00:00:00Z"},
         {"openedAt": "2026-07-03T00:00:00Z", "closedAt": None}],
    )
    self.assertEqual(warnings, [])

def test_timeline_ignores_duplicate_close_and_flags_reopen_without_close():
    episodes, warnings = normalize_timeline(
        issue_created_at="2026-07-01T00:00:00Z",
        events=[
            event("closed", "2026-07-02T00:00:00Z"),
            event("closed", "2026-07-02T00:01:00Z"),
            event("reopened", "2026-07-03T00:00:00Z"),
            event("reopened", "2026-07-03T00:01:00Z"),
        ],
    )
    self.assertEqual(len(episodes), 2)
    self.assertEqual([warning["category"] for warning in warnings],
                     ["duplicate-close", "duplicate-reopen"])

def test_closed_since_response_is_filtered_by_closed_at():
    inventory = collect_closed_fixture(
        issues=[
            issue(5, closed_at="2026-01-01T00:00:00Z"),
            issue(6, closed_at="2026-08-01T00:00:00Z"),
        ],
        cutoff="2026-05-19T00:00:00Z",
    )
    self.assertEqual([item["number"] for item in inventory.supporting_issues], [6])
```

- [ ] **Step 2: Run the collector tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_collector.py' -v
```

Expected: FAIL because `ci_shepherd.collector` does not exist.

- [ ] **Step 3: Implement inventory collection**

Implement `Collector.collect_inventory()` to query:

```text
repos/{owner}/{repo}/issues?state=open&labels={label}&per_page=100
repos/{owner}/{repo}/issues?state=closed&labels={label}&since={cutoff}&per_page=100
repos/{owner}/{repo}/issues/{number}/comments?per_page=100
repos/{owner}/{repo}/issues/{number}/timeline?per_page=100
```

Call each label independently, filter pull requests from issue inventories,
deduplicate by issue number, sort ascending, and retain all labels. Normalize
comments chronologically. Seed the first lifecycle episode from
`issue.created_at`, then apply ordered `closed` and `reopened` timeline events.
Duplicate, missing, or contradictory lifecycle events are retained as
collection warnings instead of synthesizing impossible state.

GitHub's `since` filter is based on update time, so filter the closed response
again using `closed_at >= cutoff`.

Use these collection budgets:

```text
max explicit-reference depth: 2
max supporting closed issues: 200
max references followed per issue: 50
max exact-marker candidates per marker: 20
max normalized-fact candidates per fact: 20
```

Within the 90-day closed set, index deterministic tracking markers and
normalized candidate facts (`testName`, `exceptionType`, `errorCode`,
`workflow`, `job`, and `step`). Add matching candidates to supporting issues.
Follow explicit old references outside the lookback up to depth two. Record
truncation and the exhausted budget; never silently stop traversal.

Extract repository-local `#123`, full issue/PR URLs, 7-40 character commit
SHAs, and Actions run URLs while preserving source URL and extraction method.
Explicit old references are fetched regardless of the 90-day lookback.

Fatal failure of either open-label query raises `InventoryError`. Secondary
resource failures append a structured collection error and leave an explicit
availability marker on the issue.

- [ ] **Step 4: Run inventory tests**

Expected: inventory and timeline tests PASS.

### Task 4: Enrich runs, pull requests, commits, logs, and ownership

**Files:**
- Modify: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Modify: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_collector.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/run-failed.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/jobs-failed.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/pr-merged.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/fixtures/codeowners.txt`

- [ ] **Step 1: Add failing enrichment tests**

Cover exact API references and failure behavior:

```python
def test_linked_run_includes_failed_jobs_steps_and_bounded_log_excerpt():
    snapshot = collect_linked_run_fixture(log_text="failure\n" * 100_000)
    run = snapshot["runs"][0]
    self.assertEqual(run["id"], 42)
    self.assertEqual(run["jobs"][0]["conclusion"], "failure")
    self.assertLessEqual(len(run["jobs"][0]["logExcerpt"].encode()), 200_000)
    self.assertTrue(run["jobs"][0]["logTruncated"])

def test_expired_job_log_is_recorded_not_silently_ignored():
    snapshot = collect_linked_run_fixture(log_error=GitHubApiError("not-found", "..."))
    self.assertEqual(snapshot["runs"][0]["jobs"][0]["logAvailability"], "expired-or-unavailable")
    self.assertEqual(snapshot["collectionErrors"][0]["effect"], "workflow-log evidence unavailable")

def test_merged_pr_records_files_merge_commit_and_current_codeowners():
    snapshot = collect_pr_fixture()
    self.assertEqual(snapshot["pullRequests"][0]["mergedAt"], "2026-08-10T06:53:25Z")
    self.assertEqual(snapshot["ownership"][0]["owners"], ["@aspnet/build"])

def test_run_enrichment_keeps_attempts_annotations_artifacts_and_recent_history():
    snapshot = collect_run_history_fixture()
    run = snapshot["runs"][0]
    self.assertEqual([attempt["number"] for attempt in run["attempts"]], [1, 2])
    self.assertEqual(run["artifacts"][0]["expired"], False)
    self.assertEqual(run["jobs"][0]["annotations"][0]["annotationLevel"], "failure")
    self.assertEqual(len(run["recentWorkflowHistory"]), 10)

def test_codeowners_uses_first_supported_location_and_last_matching_rule():
    owners = resolve_codeowners(
        path="docs/build/troubleshooting.md",
        candidates={
            ".github/CODEOWNERS": "* @global\n/docs/ @docs\n/docs/build/ @build\n",
            "CODEOWNERS": "* @ignored\n",
        },
    )
    self.assertEqual(owners, ["@build"])

def test_checkout_remote_must_match_requested_repository():
    with self.assertRaisesRegex(CollectionError, "checkout repository.*microsoft/aspire"):
        collect_with_checkout(remote="https://github.com/example/other.git")
```

- [ ] **Step 2: Run the tests and verify the new cases fail**

Run the Task 3 test command.

Expected: FAIL because evidence enrichment is absent.

- [ ] **Step 3: Implement evidence enrichment**

Fetch and normalize:

```text
repos/{owner}/{repo}/actions/runs/{run_id}
repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100
repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100
repos/{owner}/{repo}/actions/runs/{run_id}/artifacts?per_page=100
repos/{owner}/{repo}/check-runs/{check_run_id}/annotations?per_page=100
repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs?branch={branch}&per_page=10
repos/{owner}/{repo}/actions/jobs/{job_id}/logs
repos/{owner}/{repo}/issues/{number}
repos/{owner}/{repo}/pulls/{number}
repos/{owner}/{repo}/pulls/{number}/files?per_page=100
repos/{owner}/{repo}/commits/{sha}
```

For each linked run, retain workflow, event, branch, SHA, current attempt,
conclusion, timestamps, rerun relationships, every attempt up to the current
attempt, failed jobs, every step name/conclusion, check-run annotations,
artifact names/expiry, and the ten most recent runs of the same workflow and
branch. For a linked merged fix, retain same-workflow runs on the affected
branch whose creation time is after the merge. These bounded histories support
recurrence and post-fix verification without downloading all repository runs.

Read at most 200 KB plus one byte from each failed job log through a
`subprocess.Popen` pipe. If the extra byte exists, mark `logTruncated: true`,
terminate that exact child process, wait for it, and retain only the first
200 KB. This follows the documented one-minute redirect through `gh api`
without buffering the complete log in memory. Set `logAvailability` to `available`,
`expired-or-unavailable`, or `not-requested`.

For issue references, inspect the issue endpoint's `pull_request` field before
fetching PR detail. Record merge state, merge commit, changed files, and linked
issue references. Commits record SHA, author login/name, date, message, and
changed paths.

When a local checkout is supplied, verify its `origin` or `upstream` remote
normalizes to the requested `owner/repo` before reading it. Search CODEOWNERS in
GitHub's documented order: `.github/CODEOWNERS`, `CODEOWNERS`,
`docs/CODEOWNERS`; the first existing file wins. If no checkout is supplied,
fetch those paths through the contents API and decode base64.

Implement CODEOWNERS matching from the documented syntax rather than Python
`fnmatch`: case-sensitive paths, root-anchored leading slash, directory
patterns matching descendants, `*` not crossing `/`, `**` crossing
directories, unsupported `!` and character ranges skipped as invalid, empty
owner lists clearing ownership, and last matching rule winning. Record the
matched rule, line, owners, source path, and checkout commit.

For each evidence-backed affected path, run:

```bash
git -C "$CHECKOUT" --no-pager log -5 --format=%H%x09%aN%x09%aE%x09%aI -- "$PATH"
```

Store the five most recent path authors as secondary ownership evidence.

- [ ] **Step 4: Run all collector tests**

Expected: all collector tests PASS.

### Task 5: Add atomic run storage and collector CLI

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/storage.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/collect.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_storage.py`

- [ ] **Step 1: Write failing storage tests**

Tests pin:

```python
def test_begin_run_uses_utc_timestamp_and_repository_slug():
    run = RunStorage(root, "microsoft/aspire", now=FIXED_NOW).begin()
    self.assertEqual(run.path, root / "microsoft-aspire" / "runs" / "2026-08-17T210328Z")

def test_begin_run_adds_suffix_on_same_second_collision():
    first = RunStorage(root, "microsoft/aspire", now=FIXED_NOW).begin()
    second = RunStorage(root, "microsoft/aspire", now=FIXED_NOW).begin()
    self.assertEqual(first.path.name, "2026-08-17T210328Z")
    self.assertEqual(second.path.name, "2026-08-17T210328Z-01")

def test_failed_collection_does_not_update_latest():
    run = make_run()
    run.write_manifest(status="collection-failed")
    self.assertFalse(run.latest_path.exists())

def test_atomic_json_never_leaves_temporary_file():
    atomic_write_json(target, {"ok": True})
    self.assertEqual(json.loads(target.read_text()), {"ok": True})
    self.assertEqual(list(target.parent.glob("*.tmp")), [])

def test_interrupted_report_write_preserves_previous_latest():
    previous = make_reported_run("2026-08-16T120000Z")
    run = make_run("2026-08-17T210328Z")
    with self.assertRaisesRegex(OSError, "injected"):
        run.finalize_report(report(), markdown(), fail_after="report-json")
    self.assertEqual(read_latest()["run"], previous.path.name)
    self.assertEqual(read_manifest(run.path)["status"], "report-failed")

def test_reported_run_recovers_missing_latest_pointer():
    run = make_reported_run("2026-08-17T210328Z", write_latest=False)
    recover_latest(run.path)
    self.assertEqual(read_latest()["run"], run.path.name)

def test_older_concurrent_finalizer_cannot_regress_latest():
    newer = make_reported_run("2026-08-17T220000Z")
    older = make_reported_run("2026-08-17T210000Z", write_latest=False)
    recover_latest(older.path)
    self.assertEqual(read_latest()["run"], newer.path.name)

def test_files_are_private_by_default():
    run = make_run()
    atomic_write_json(run.path / "input.json", {"ok": True})
    self.assertEqual(stat.S_IMODE(run.path.stat().st_mode), 0o700)
    self.assertEqual(stat.S_IMODE((run.path / "input.json").stat().st_mode), 0o600)

def test_pruning_deletes_only_reported_runs_older_than_retention():
    old = make_reported_run("2026-07-01T000000Z")
    current = make_reported_run("2026-08-17T210328Z")
    incomplete = make_collecting_run("2026-07-01T010000Z")
    pruned = prune_runs(root, now=FIXED_NOW, retain_days=30)
    self.assertEqual(pruned, [old.path])
    self.assertTrue(current.path.exists())
    self.assertTrue(incomplete.path.exists())
```

- [ ] **Step 2: Run storage tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_storage.py' -v
```

Expected: FAIL because `ci_shepherd.storage` does not exist.

- [ ] **Step 3: Implement storage and `collect.py`**

`collect.py` accepts:

```text
--repo OWNER/REPO               default microsoft/aspire
--checkout PATH                 optional
--lookback-days N               default 90
--output-root PATH              default ~/.copilot/ci-shepherd
--fixture-map PATH              tests only
--now ISO-8601                  tests only; defaults to current UTC time
```

It verifies `gh auth status`, creates the run directory, writes a collecting
manifest, invokes `Collector`, validates the snapshot, and atomically writes
`input.json`, `collection-errors.json`, and a `collected` manifest. On fatal
inventory failure it writes `collection-failed` and exits nonzero. On success
it prints only the absolute run directory so the skill can capture it.

Before collection, perform read-only endpoint preflights for repository
metadata, one issue page, one Actions run page, one pull-request page, one
commit page, and each supported CODEOWNERS contents path. A CODEOWNERS 404 is
allowed; authorization or authentication failure is fatal. Record the authenticated login from
`user`, repository node ID/default branch, API version, hostname, and preflight
results in the manifest. `gh auth status` alone is not sufficient.

Create run directories with mode `0700` using exclusive creation. Resolve
same-second collisions with `-01`, `-02`, and so on. Write JSON and Markdown to
same-directory temporary files with mode `0600`, flush and `fsync`, then
`os.replace`. Manifests follow:

```text
collecting -> collected -> reasoning -> reported
     |            |           |
collection-failed |      report-failed
                  reasoning-failed
```

Rerunning collection never reuses a run directory. Finalization is idempotent
only for a `collected`, `reasoning`, or `report-failed` run whose input hash
matches the manifest. Test interruption before and after every manifest,
report file, and `latest.json` transition. `latest.json` changes only after
both validated report files and a `reported` manifest exist.

Serialize finalization and recovery with an `fcntl.flock` on a private
repository-state lock file. If a run is already `reported` but `latest.json`
was not updated, revalidate both report files and recover the pointer. Compare
run timestamps while holding the lock so an older concurrent finalizer cannot
replace a newer pointer.

At collection startup, prune only `reported` run directories older than 30
days beneath the resolved repository state root. Preserve collecting, failed,
and current-latest runs regardless of age. Reject symlinks and any resolved
prune target outside that exact `runs` directory.

- [ ] **Step 4: Run storage and collector tests**

Expected: all model, GitHub, collector, and storage tests PASS.

### Task 6: Validate decisions and render reports

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/ci_shepherd/reporter.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/finalize.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_reporter.py`

- [ ] **Step 1: Write failing reporter tests**

Cover report sections, comparison, and latest pointer behavior:

```python
def test_markdown_prioritizes_changed_high_priority_items():
    report = report_with(
        decision(10, action="close", changed=True),
        decision(11, action="wait", changed=False),
    )
    markdown = render_markdown(snapshot(), report)
    self.assertLess(markdown.index("# Changed and high-priority"), markdown.index("# Observing"))
    self.assertIn("#10", markdown)

def test_previous_report_is_context_not_primary_evidence():
    compared = compare_reports(previous_report(), current_report())
    self.assertEqual(compared[0]["priorAction"], "investigate")
    self.assertNotIn("previous-report", current_report()["decisions"][0]["evidence"])

def test_missing_or_corrupt_latest_starts_without_comparison():
    for latest in (None, "{not-json"):
        result = load_previous_report(root, latest_contents=latest)
        self.assertIsNone(result.report)
        self.assertIn(result.warning, {"no previous report", "invalid latest.json"})

def test_incompatible_previous_report_is_rejected():
    previous = previous_report(repository="example/other", schema_version=99)
    result = compare_reports(previous, current_report())
    self.assertEqual(result.warning, "previous report is incompatible")

def test_comparison_reports_all_material_deltas():
    changes = compare_reports(
        previous_report_fixture(),
        current_report_fixture_with_open_close_timeline_evidence_owner_and_relationship_changes(),
    )
    self.assertEqual(
        {change["type"] for change in changes},
        {
            "opened", "disappeared", "timeline-changed", "evidence-available",
            "owner-changed", "confidence-changed", "relationship-changed",
            "recommendation-blocked",
        },
    )

def test_latest_updates_only_after_valid_report_files_exist():
    finalize_run(run_dir, decisions_path)
    latest = json.loads((root / "microsoft-aspire" / "latest.json").read_text())
    self.assertEqual(latest["run"], run_dir.name)
    self.assertTrue((run_dir / "report.json").exists())
    self.assertTrue((run_dir / "report.md").exists())
```

- [ ] **Step 2: Run reporter tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_reporter.py' -v
```

Expected: FAIL because `ci_shepherd.reporter` does not exist.

- [ ] **Step 3: Implement reporting and `finalize.py`**

`finalize.py` accepts:

```text
--run-dir PATH
--decisions PATH
--previous-report PATH          optional; defaults via latest.json
```

The decisions file is an object with `decisions`, `relationships`, and
`reportWarnings`. The finalizer adds metadata, summary counts, and
`changesSincePreviousRun`; validates the complete report; writes `report.json`
and `report.md`; changes the manifest status to `reported`; and atomically
updates `latest.json`.

Load a previous report only when `latest.json` parses, points beneath the same
repository state root, has a `reported` manifest, uses a supported schema, and
matches the current repository. Missing, corrupt, stale, or incompatible
previous state produces a warning and first-run behavior.

Comparison covers newly opened issues, disappeared/closed issues, lifecycle
episode changes, new occurrences, state/action/confidence changes, owner
changes, relationship changes, evidence becoming available/unavailable, and
recommendations blocked by contradictory evidence. Previous decisions are
copied only into comparison fields and can never satisfy current evidence
validation.

Render Markdown sections in this fixed order: metadata/collection health,
changed and high-priority, safe close candidates, duplicates, actionable
investigations/fixes, human escalations, observing/awaiting verification,
insufficient evidence, previous-run changes, and evidence appendix. Every row
includes issue, state, action, confidence, next condition, and evidence links.

- [ ] **Step 4: Run reporter and full unit tests**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -v
```

Expected: all tests PASS.

### Task 7: Author the `ci-shepherd` reasoning skill

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/SKILL.md`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_corpus.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/input.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/expected-report.json`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/corpus/expected-decisions.json`

- [ ] **Step 1: Add the expected-decision corpus**

Create constraints for issues #19166, #18755, #18794, #19143, #18840,
#19379, #19363, #18657, #18629, #18608, #18880, #18897, and #18898. Each
entry declares allowed states/actions, required relationships, and forbidden
claims. Example:

```json
{
  "19143": {
    "allowedStates": ["duplicate"],
    "allowedActions": ["merge-duplicate"],
    "requiredRelationship": {"type": "exact-duplicate", "target": 18840}
  },
  "18657": {
    "allowedKinds": ["tracker"],
    "forbiddenRelationships": ["exact-duplicate"],
    "requiredReasoningText": ["multiple", "Outerloop"]
  },
  "19166": {
    "allowedStates": ["awaiting-verification", "resolved", "insufficient-evidence"],
    "forbiddenStateWithoutPostFixGreenRun": "resolved"
  }
}
```

- [ ] **Step 2: Write failing executable corpus tests**

Use `tests/corpus/input.json` as a complete, minimal normalized snapshot for
the named cases and `tests/corpus/expected-report.json` as the valid baseline
decisions and relationships. Each issue includes the exact evidence records
required by its constraint. Tests call
`validate_corpus(snapshot, report, constraints)`:

```python
def test_named_corpus_accepts_expected_decisions():
    validate_corpus(load_input(), load_expected_report(), load_constraints())

def test_named_corpus_rejects_forbidden_resolution_without_green_run():
    report = load_expected_report()
    decision_for(report, 19166).update(state="resolved", proposedAction="close")
    remove_post_fix_green_run(report, 19166)
    with self.assertRaisesRegex(CorpusError, "19166.*post-fix green run"):
        validate_corpus(load_input(), report, load_constraints())

def test_named_corpus_rejects_lost_duplicate_relationship():
    report = load_expected_report()
    report["relationships"] = [
        relationship for relationship in report["relationships"]
        if relationship["source"] != 19143
    ]
    with self.assertRaisesRegex(CorpusError, "19143.*exact-duplicate.*18840"):
        validate_corpus(load_input(), report, load_constraints())
```

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_corpus.py' -v
```

Expected: FAIL until `validate_corpus` and the fixture are implemented.

- [ ] **Step 3: Implement corpus validation**

Add `validate_corpus` to `models.py` and call it from `finalize.py` whenever
the repository is `microsoft/aspire` and the named issue is present in both the
live snapshot and constraints. Validate allowed/forbidden states, actions,
kinds, relationships, required evidence kinds, required reasoning terms, and
conditional prohibitions. Every failure names the issue number and violated
constraint.

- [ ] **Step 4: Write `SKILL.md`**

Frontmatter:

```yaml
---
name: ci-shepherd
description: "Read-only shepherd for microsoft/aspire issues labeled ci-failure-cause or automation-broken. Collects current GitHub evidence, classifies every open issue, and writes local JSON/Markdown proposed-action reports. Use when asked to shepherd, triage, summarize, or recommend actions for Aspire CI failure issues."
---
```

The skill must instruct the agent to:

1. Run `scripts/collect.py` and capture its single run-directory output.
2. Read `input.json`, `collection-errors.json`, the previous `report.json`, and
   the corpus constraints.
3. Produce `decisions.draft.json` with exactly one decision per open issue.
4. Use only evidence IDs present in the current snapshot for factual claims.
5. Apply the approved lifecycle/action/confidence rules and never equate a
   merged PR with verified resolution.
6. Run `scripts/finalize.py`.
7. If validation fails, correct only the invalid decisions and rerun.
8. Return a concise summary of changed/high-priority items and absolute report
   paths.

Include exact decision JSON shape, evidence citation syntax, bounded-next-
condition examples, owner evidence order, corpus-check instructions, and the
prohibition on all GitHub write commands.

- [ ] **Step 5: Run the corpus and full unit tests**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -v
```

Expected: all tests PASS, including named-case violations that identify their
issue numbers.

### Task 8: Install and discover the personal skill safely

**Files:**
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/install.py`
- Create: `/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests/test_install.py`

- [ ] **Step 1: Write failing installation tests**

```python
def test_install_refuses_collision_without_backup_flag():
    existing = personal_skills / "ci-shepherd"
    existing.mkdir()
    with self.assertRaisesRegex(InstallError, "already exists"):
        install(source, personal_skills, backup_existing=False)

def test_install_backs_up_existing_and_atomically_replaces():
    existing = make_existing_skill(version="old")
    installed = install(source, personal_skills, backup_existing=True)
    self.assertEqual((installed / "SKILL.md").read_text(), source_skill_text())
    self.assertTrue(any(personal_skills.glob(".ci-shepherd.backup-*")))
    self.assertEqual(list(personal_skills.glob(".ci-shepherd.staging-*")), [])

def test_removal_guidance_uses_verified_cli_command():
    self.assertIn("copilot skill remove ci-shepherd", removal_guidance())
```

- [ ] **Step 2: Run installation tests and verify they fail**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/tests -p 'test_install.py' -v
```

Expected: FAIL until `install.py` exists.

- [ ] **Step 3: Implement staged installation**

`install.py` accepts `--source`, `--personal-skills-root` defaulting to
`~/.copilot/skills`, and `--backup-existing`. It validates source frontmatter
and required scripts, copies into `.ci-shepherd.staging-{pid}` with private
permissions, runs the unit tests from staging, optionally renames an existing
target to `.ci-shepherd.backup-{UTC timestamp}`, and atomically renames staging
to `ci-shepherd`. On failure it removes only its named staging directory and
restores a backup if replacement had started.

Print the verified rollback command:

```text
copilot skill remove ci-shepherd
```

The implementation is authored first under:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/
```

Then install it with `install.py`; do not overwrite a pre-existing personal
skill without a timestamped backup.

- [ ] **Step 4: Verify skill discovery and frontmatter**

Run:

```bash
python3 /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build/scripts/install.py \
  --source /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-build
python3 - <<'PY'
from pathlib import Path
text = Path("/Users/ankj/.copilot/skills/ci-shepherd/SKILL.md").read_text()
assert text.startswith("---\nname: ci-shepherd\n")
assert "gh issue edit" not in text
assert "gh issue close" not in text
print("skill contract valid")
PY
copilot skill list --json \
  | python3 -c 'import json,sys; data=json.load(sys.stdin); assert any(item.get("name") == "ci-shepherd" and item.get("enabled") for item in data)'
```

Expected: `skill contract valid` and exit 0 from the discovery assertion.

### Task 9: Run the live read-only prototype and verify acceptance

**Files:**
- Create: `/Users/ankj/.copilot/skills/ci-shepherd/scripts/verify.py`
- Runtime output only: `/Users/ankj/.copilot/ci-shepherd/microsoft-aspire/runs/YYYY-MM-DDTHHMMSSZ/`

- [ ] **Step 1: Run all unit tests**

Run:

```bash
PYTHONPATH=/Users/ankj/.copilot/skills/ci-shepherd/scripts \
  python3 -m unittest discover -s /Users/ankj/.copilot/skills/ci-shepherd/tests -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run the live collector**

Run:

```bash
mkdir -p /Users/ankj/.copilot/ci-shepherd/scratch
python3 /Users/ankj/.copilot/skills/ci-shepherd/scripts/collect.py \
  --repo microsoft/aspire \
  --checkout /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine \
  | tee /Users/ankj/.copilot/ci-shepherd/scratch/current-run-dir
```

Expected: exit 0 and one absolute run-directory path.

- [ ] **Step 3: Generate decisions using the skill contract**

Invoke the newly discovered `ci-shepherd` skill with the collected run
directory. Analyze the current snapshot in bounded batches, merge them into
`decisions.draft.json`, and enforce the corpus constraints. For any case whose
current evidence no longer supports the historical expected relationship,
record `insufficient-evidence` and explain the mismatch rather than forcing the
fixture's old conclusion.

- [ ] **Step 4: Finalize and inspect the report**

Run:

```bash
RUN_DIR="$(cat /Users/ankj/.copilot/ci-shepherd/scratch/current-run-dir)"
python3 /Users/ankj/.copilot/skills/ci-shepherd/scripts/finalize.py \
  --run-dir "$RUN_DIR" \
  --decisions "$RUN_DIR/decisions.draft.json"
python3 /Users/ankj/.copilot/skills/ci-shepherd/scripts/verify.py \
  --run-dir "$RUN_DIR" \
  --live-inventory
```

Expected: exit 0; `report.json`, `report.md`, and `latest.json` exist; every
open issue has one decision; high-risk actions pass primary-evidence
validation; Markdown section ordering matches the specification.

`verify.py` independently fetches the current open union for both labels,
compares issue numbers with `input.json`, checks report and corpus invariants,
and audits `api-calls.jsonl`. It fails if an open issue is missing/extra, a
duplicate exists, any API method is not GET, any endpoint is outside the
allowlisted read-only endpoint shapes, a named corpus constraint fails, or a
report evidence ID does not resolve.

- [ ] **Step 5: Run a second fixture collection to verify stability**

Run the collector twice with the same fixture map and fixed `--now`, then
compare `input.json` after removing the manifest-relative timestamp:

```bash
FIRST_RUN="$(python3 /Users/ankj/.copilot/skills/ci-shepherd/scripts/collect.py \
  --fixture-map /Users/ankj/.copilot/skills/ci-shepherd/tests/fixtures/api-map.json \
  --output-root /Users/ankj/.copilot/ci-shepherd/scratch/fixture-one \
  --now 2026-08-17T21:03:28Z)"
SECOND_RUN="$(python3 /Users/ankj/.copilot/skills/ci-shepherd/scripts/collect.py \
  --fixture-map /Users/ankj/.copilot/skills/ci-shepherd/tests/fixtures/api-map.json \
  --output-root /Users/ankj/.copilot/ci-shepherd/scratch/fixture-two \
  --now 2026-08-17T21:03:28Z)"
diff -u "$FIRST_RUN/input.json" "$SECOND_RUN/input.json"
```

Expected: no differences.

- [ ] **Step 6: Record implementation status**

Update this plan's checkboxes, retain the local live report, and report:

- Total open issues inventoried by label.
- Collection warnings and unavailable evidence.
- Counts by state/action/confidence.
- Named acceptance cases that passed or were conservatively blocked.
- Absolute Markdown and JSON report paths.

Do not post, edit, close, reopen, label, or assign any GitHub issue.

- [ ] **Step 7: Document retention and rollback**

Add a final section to `SKILL.md` stating that log excerpts and reports remain
local with private permissions, are retained for 30 days by default, and are
pruned only beneath `~/.copilot/ci-shepherd/{repository}/runs/`. Provide:

```bash
copilot skill remove ci-shepherd
```

as the supported uninstall command and explain how to restore the newest
`.ci-shepherd.backup-*` directory if installation replaced an earlier version.
