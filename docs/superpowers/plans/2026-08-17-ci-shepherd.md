# CI Failure Shepherd Implementation Plan

> **Status:** The original read-only foundation and subsequent lifecycle,
> assessment, pull-request, and actor work are implemented through commit
> `443e989126`. Current validation and remaining constraints are recorded in
> [CI Shepherd Implementation Status](../status/2026-08-28-ci-shepherd-implementation.md).
> Continue from
> [CI Shepherd Continuation Plan](2026-08-28-ci-shepherd-continuation.md)
> instead of replaying this historical task list.

The bounded producer-aware lifecycle follow-up is tracked in
[CI Shepherd Lifecycle Hardening Implementation Plan](2026-08-19-ci-shepherd-lifecycle-hardening.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a manually invoked, read-only user-level Copilot skill that collects authoritative Aspire CI issue evidence and produces validated local JSON and Markdown action reports.

**Architecture:** A dependency-free Python package shells out to the authenticated `gh api` CLI, normalizes GitHub facts, and atomically stores each collection run. The `ci-shepherd` skill guides the agent to reason only from that snapshot, write one decision per open issue, and pass those decisions through a deterministic validator/reporter before updating the latest-run pointer.

**Tech Stack:** Copilot user skills, Python 3.14 standard library, GitHub CLI REST API, `unittest`, JSON, Markdown.

---

## File map

- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/SKILL.md`: invocation contract, reasoning protocol, evidence rules, and concise chat output.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/models.py`: schema enums, normalization helpers, and input/report validation.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/github.py`: read-only `gh api` subprocess client, pagination, retry, and error classification.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/collector.py`: issue union, timeline/reference/run/PR/commit enrichment, log excerpts, and ownership evidence.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/storage.py`: run layout, manifests, atomic JSON writes, and latest-run pointer.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/reporter.py`: decision validation, previous-run comparison, Markdown rendering, and report finalization.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/collect.py`: collector CLI.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/finalize.py`: report validation/finalization CLI.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/verify.py`: independent inventory, corpus, report, and GET-only audit.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/install.py`: staged personal-skill installation, backup, and rollback guidance.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_models.py`: schema and high-risk safety tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_github.py`: subprocess, pagination, retry, and fatal/partial failure tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_collector.py`: inventory, reference, timeline, log, and ownership fixture tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_storage.py`: atomic run and latest pointer tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_reporter.py`: report completeness, comparison, ordering, and Markdown tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_corpus.py`: executable named-case constraints.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_install.py`: collision, backup, discovery, and removal guidance tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/`: compact GitHub API responses for deterministic tests.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/api-map.json`: endpoint-to-fixture map for deterministic CLI runs.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/input.json`: normalized evidence for the named audited cases.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/expected-report.json`: valid decisions and relationships for the named corpus.
- `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/expected-decisions.json`: mandatory decision constraints for the audited Aspire cases.

### Task 1: Define stable schemas and validation

**Files:**
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/__init__.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_models.py`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_models.py' -v
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
issue:{owner}/{repo}:{number}
issue:{number}:comment:{comment_id}
issue:{number}:event:{event_id}
run:{run_id}
run:{run_id}:attempt:{attempt}:job:{job_id}
run:{run_id}:attempt:{attempt}:job:{job_id}:log
run:{run_id}:check:{check_run_id}:annotation:{annotation_id}
pr:{number}
pr:{owner}/{repo}:{number}
commit:{full_sha}
commit:{owner}/{repo}:{full_sha}
source:{percent_encoded_path}
codeowners:{percent_encoded_path}:{line_number}
```

The compact issue, pull request, and commit forms identify evidence from the
snapshot repository. Cross-repository references use the repository-qualified
forms so same-number issues and pull requests cannot overwrite one another.

Every evidence record carries `kind`, `url`, `collectedAt`, `availability`,
and its normalized factual payload. Action-specific validation requires:

- `close`: compatibility syntax with the same requirements as
  `close-resolved`: a current merged-fix or recovery record, a current post-fix
  green workflow run, a current `no-newer-matching-failure` record, and no
  newer contradictory failing run.
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
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/github.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_github.py`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_github.py' -v
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
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_collector.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/issues-ci-failure-cause.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/issues-automation-broken.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/timeline-reopen.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/api-map.json`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_collector.py' -v
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
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_collector.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/run-failed.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/jobs-failed.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/pr-merged.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/fixtures/codeowners.txt`

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
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/storage.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/collect.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_storage.py`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_storage.py' -v
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
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/reporter.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/finalize.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_reporter.py`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_reporter.py' -v
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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -v
```

Expected: all tests PASS.

### Task 11: Add bounded adaptive evidence expansion

**Files:**
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/expand.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/adaptive.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_adaptive.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/SKILL.md`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_models.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_scripts.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/docs/superpowers/specs/2026-08-17-ci-shepherd-design.md`

- [ ] **Step 1: Write failing request-schema and expansion tests**

Add fixtures and tests for this request document:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "round": 1,
  "requests": [
    {
      "type": "issue-reference",
      "sourceIssueNumber": 19149,
      "evidenceId": "issue:19148",
      "decisionGate": "merged-fix",
      "reason": "Verify the referenced fix."
    },
    {
      "type": "workflow-run",
      "sourceIssueNumber": 19149,
      "evidenceId": "run:31203621605",
      "decisionGate": "post-fix-green",
      "reason": "Collect the source run and covered branch history."
    },
    {
      "type": "canonical-search",
      "sourceIssueNumber": 18592,
      "evidenceId": "issue:18592",
      "factField": "testName",
      "decisionGate": "canonical-search-complete",
      "reason": "Find an existing canonical flaky-test issue."
    }
  ]
}
```

Tests must prove:

- only `issue-reference`, `workflow-run`, `canonical-search`, and
  `source-check` are accepted;
- `round` is one or two;
- each document has at most 25 unique requests, at most 10 canonical searches,
  and at most five requests for one source issue;
- `sourceIssueNumber` is open in the snapshot;
- `evidenceId` resolves to evidence scoped to that source issue;
- issue/run requests target partial, `not-enriched`, or budget/depth-excluded
  evidence rather than refetching available detail;
- canonical searches use an exact factual `field`, `value`, and `normalized`
  tuple already present in the cited issue evidence; the agent cannot supply
  arbitrary query text;
- source checks use an existing scoped `source-path`, pull-request, commit, job,
  annotation, or log record containing the requested path;
- repository mismatches, duplicate requests, invented facts, arbitrary
  endpoints, and write-shaped fields are rejected;
- expansion output does not mutate the baseline snapshot object;
- partial failures are recorded per request while independent requests
  continue.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
TMPDIR="$PWD/.ci-shepherd-build/tests/.tmp" \
  PYTHONPATH=.ci-shepherd-build/scripts \
  python3 -m unittest discover -s .ci-shepherd-build/tests \
  -p 'test_adaptive.py' -v
```

Expected: FAIL because `ci_shepherd.adaptive` and `expand.py` do not exist.

- [ ] **Step 3: Implement allowlisted adaptive requests**

In `models.py`, add:

```python
EVIDENCE_REQUEST_TYPES = frozenset({
    "issue-reference",
    "workflow-run",
    "canonical-search",
    "source-check",
})
EVIDENCE_REQUEST_DECISION_GATES = frozenset({
    "merged-fix",
    "recovery",
    "post-fix-green",
    "no-newer-matching-failure",
    "no-recent-matching-failure",
    "canonical-issue",
    "canonical-search-complete",
    "obsolete-surface",
    "current-failing-run",
    "prior-resolved-episode",
})
```

Implement `validate_evidence_requests(snapshot, request_document)` with the
limits and grounding rules from Step 1. It returns normalized requests sorted
by source issue, request type, and evidence ID/fact field. Validation never
accepts a URL, endpoint, HTTP method, repository override, or arbitrary search
string from the agent.

Implement `AdaptiveEnricher` in `adaptive.py`:

- Deep-copy and validate the baseline snapshot.
- Execute normalized requests using the existing GET-only `GitHubClient`.
- Reuse collector normalization and association helpers rather than creating
  incompatible evidence shapes.
- For `issue-reference`, GET the referenced issue and, when it is a pull
  request, its pull-request detail and changed files. Preserve the source
  association and replace the partial/not-enriched record with available
  evidence.
- For `workflow-run`, use the existing bounded run enrichment with current
  attempt, at most 10 failed jobs, three logs, and one first-page history
  request with at most 10 results.
- For `canonical-search`, construct the search query from the cited exact fact:
  `repo:{repository} is:issue "<value>"`. Fetch one page with `per_page=20`.
  Record total count, returned count, truncation, exact query fact, and
  `complete: true` only when the total count is at most 20 and the response is
  well formed. Normalize results as issue evidence associated with the source
  issue. When a result collides with baseline evidence, preserve every baseline
  `referencedBy` association and merge the request-derived association without
  replacing or duplicating an existing source association. Do not treat search
  execution alone as a canonical match.
- For `source-check`, inspect the evidence-backed path beneath the supplied
  checkout without accepting an arbitrary path from the request. Record
  existence, current checkout commit, and whether repository history shows a
  removal or replacement. Missing checkout or ambiguous history is partial,
  not proof of obsolescence. Before every success, partial, or error write,
  merge the request-derived associations with all `referencedBy` associations
  from any colliding baseline source record.
- Merge new records deterministically, preserve all baseline evidence and
  collection errors, and append an `expansion` manifest containing round,
  normalized requests, completion status, and errors.

- [ ] **Step 4: Add the expansion CLI and immutable artifacts**

`expand.py` accepts:

```text
--input PATH
--requests PATH
--output PATH
--errors PATH
--checkout PATH
--audit PATH
```

It creates private files, refuses an output path equal to the input path,
validates the request document before GitHub access, runs only GET requests,
validates the expanded snapshot, and prints the absolute output path.

Add tests proving the exact CLI:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/expand.py" \
  --input "$SCRATCH/input.json" \
  --requests "$SCRATCH/evidence-requests.round-1.json" \
  --output "$SCRATCH/input.round-1.json" \
  --errors "$SCRATCH/expansion-errors.round-1.json" \
  --checkout "$CHECKOUT" \
  --audit "$SCRATCH/api-calls.jsonl"
```

uses private permissions, preserves the baseline input byte-for-byte, and
rejects a third round.

- [ ] **Step 5: Update the skill to use at most two adaptive rounds**

After the first draft assessment, require the shepherd to:

1. Identify missing evidence that could change a decision.
2. Write only grounded allowlisted requests.
3. Prefer explicit referenced fixes/runs before canonical searches.
4. Request canonical search only for a recurring/known signature.
5. Run `expand.py`, read the new immutable snapshot, and reassess every issue.
6. Stop when remaining gaps cannot change an action or after round two.
7. Validate the final report against the newest snapshot.

The prompt must explicitly prohibit direct agent-authored `gh api`, arbitrary
web/GitHub searches, user-supplied endpoints, unbounded traversal, and using an
attempted or truncated expansion as completed evidence.

- [ ] **Step 6: Verify #19149 and canonical-search fixtures end to end**

Add an integration fixture where the first pass leaves #19149's linked fix and
run partial. Round one requests those records; the expanded snapshot supplies
merged-fix, post-fix-green, and covered history facts so a validated
`close-resolved` decision becomes possible.

Add a flaky incident fixture where round one performs an exact test-name search:

- zero complete results permits `canonical-search-complete`;
- one matching canonical issue permits `close-as-tracked`;
- more than 20 results or an API error leaves the search incomplete;
- a truncated search cannot support `open-dedicated-issue`.

- [ ] **Step 7: Run all prototype tests**

Run:

```bash
TMPDIR="$PWD/.ci-shepherd-build/tests/.tmp" \
  PYTHONPATH=.ci-shepherd-build/scripts \
  python3 -m unittest discover -s .ci-shepherd-build/tests -v
```

Expected: all tests PASS, including GET-only auditing and immutable baseline
artifacts.

### Task 7: Author the `ci-shepherd` reasoning skill

**Files:**
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/SKILL.md`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_corpus.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/input.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/expected-report.json`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/corpus/expected-decisions.json`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_corpus.py' -v
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

1. Save the target checkout as `CHECKOUT="$PWD"`, set
   `CI_SHEPHERD_ROOT` to the directory containing `SKILL.md`, and run
   `"$CI_SHEPHERD_ROOT/scripts/collect.py"` without resolving scripts relative
   to the target repository.
2. Read `input.json`, `collection-errors.json`, the previous `report.json`, and
   the corpus constraints.
3. Produce `decisions.draft.json` with exactly one decision per open issue.
4. Use only evidence IDs present in the current snapshot for factual claims.
5. Apply the approved lifecycle/action/confidence rules and never equate a
   merged PR with verified resolution.
6. Run `"$CI_SHEPHERD_ROOT/scripts/finalize.py"`.
7. If validation fails, correct only the invalid decisions and rerun.
8. Return a concise summary of changed/high-priority items and absolute report
   paths.

Include exact decision JSON shape, evidence citation syntax, bounded-next-
condition examples, owner evidence order, corpus-check instructions, and the
prohibition on all GitHub write commands.

- [ ] **Step 5: Run the corpus and full unit tests**

Run:

```bash
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -v
```

Expected: all tests PASS, including named-case violations that identify their
issue numbers.

### Task 8: Install and discover the personal skill safely

**Files:**
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/install.py`
- Create: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_install.py`

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
PYTHONPATH=/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts \
  python3 -m unittest discover -s /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests -p 'test_install.py' -v
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
/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/
```

Then install it with `install.py`; do not overwrite a pre-existing personal
skill without a timestamped backup.

- [ ] **Step 4: Verify skill discovery and frontmatter**

Run:

```bash
python3 /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/install.py \
  --source /Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build
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

### Task 10: Model `ci/main` incident dispositions explicitly

**Files:**
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/SKILL.md`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/collect.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/__init__.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/scripts/ci_shepherd/ownership.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_collector.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_enrichment.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_models.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_ownership.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/.ci-shepherd-build/tests/test_scripts.py`
- Modify: `/Users/ankj/worktrees/aspire/copilot-worktrees/aspire/radical-fuzzy-engine/docs/superpowers/specs/2026-08-17-ci-shepherd-design.md`

- [ ] **Step 1: Write failing disposition and evidence-role tests**

Keep the explicit Task 10 disposition tests and add these model tests for the
collector/report boundary:

- `test_valid_high_risk_decision_uses_roles_from_report_references` builds a
  collector-shaped merged pull request and workflow-run records with no
  `payload.role`, assigns `merged-fix`, `post-fix-green`, and
  `no-newer-matching-failure` on report references, and expects validation to
  pass.
- `test_report_role_must_not_conflict_with_deterministic_snapshot_role` assigns
  `newer-failure` in the snapshot and
  `no-newer-matching-failure` on the report reference, then expects a role
  conflict.
- `test_required_positive_role_associated_only_with_another_issue_is_rejected`
  assigns `post-fix-green` on a report reference to factual evidence scoped only
  to issue 2 and verifies it cannot close issue 1.
- `test_high_risk_decision_must_cite_scoped_roleless_snapshot_record` adds a
  factual issue-scoped comment without a role and expects omission to fail.
- `test_snapshot_rejects_unsupported_evidence_availability` uses `availble` and
  expects snapshot validation to fail.
- `test_optional_evidence_roles_must_be_supported` covers null, empty,
  non-string, and unknown roles in snapshots and report references.
- Plural-role tests require a nonempty, unique `roles` list drawn from
  `EVIDENCE_ROLES`, reject references that specify both `role` and `roles`, and
  reject any plural assignment other than `[payload.role]` when the snapshot
  has a deterministic singular role.
- A collector-shaped `close-resolved` end-to-end test assigns
  `roles: ["post-fix-green", "no-newer-matching-failure"]` to one run.
  Negative tests remove factual success or rigorous history coverage so each
  independent role gate demonstrably fails.
- Effective-cause tests assign optional `normalizedCause` on report references,
  prefer a deterministic snapshot cause, reject conflicting snapshot/reference
  values, and require all three `open-regression` role references to have the
  same nonempty effective cause.

Retain the full high-risk role/action matrix for `close`, `close-resolved`,
`close-stale`, `close-as-tracked`, `open-dedicated-issue`, `merge-duplicate`,
and `open-regression`. Add an end-to-end script test that passes exact
collector-shaped factual records without `payload.role` through
`build_snapshot`, assigns roles and matching normalized causes on report
references, and validates `open-regression` without a snapshot
`normalizedCause`.

Add collection-boundary tests proving:

- `scripts/collect.py` opts into supporting issues with explicit budgets of 20
  total supporting closed issues, five references per issue, three marker
  candidates, and three fact candidates while leaving timelines disabled.
- Every open issue records completed-zero-match and incomplete/truncated
  `supportingSearch` shapes.
- Every selected explicit or inferred supporting issue records deterministic
  `referencedBy` associations for each selecting open issue. Cover direct and
  transitive explicit references, marker/fact matches that use the stable open
  issue evidence ID, and one support issue selected by multiple open issues.
- A depth-chain fixture `#21 -> #401 -> #402 -> #403` proves only `#401` and
  `#402` are selected, `#403` is never fetched, and its unavailable stub retains
  root association and deterministic `depth-limit` selection metadata.
- A fanout fixture proves shared global-budget selection does not make extra
  issue-detail GETs, while each excluded stub retains every associated root.
- Minimal run evidence retains the current attempt, 10-failed-job, and
  three-log limits while an explicit option makes one bounded history request
  for each of at most 10 selected referenced runs.
- History proof covers missing lists, malformed responses, endpoint errors,
  truncated windows that omit the source, truncated windows containing the
  source, complete short windows, and missing source identity/timestamps.
  Collection failures leave `recentHistoryCollected: false`; only a bounded
  window that proves coverage sets `historyCoversSourceRun: true`.
- Issue comments, timeline events, local source paths, and CODEOWNERS evidence
  are associated with the source issue and included by high-risk completeness.
- Incomplete factual searches cannot satisfy search-complete report roles.

Add prompt-contract tests for
`{id, kind, role?, roles?, normalizedCause?}`, complete issue-scoped citation,
deterministic role/cause immutability, per-role factual proof, strong history
proof, supporting-issue associations and exclusions, endpoint-family/result
budgets, and the `CI_SHEPHERD_ROOT` / `CHECKOUT` invocation.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
TMPDIR="$PWD/.ci-shepherd-build/tests/.tmp" \
  PYTHONPATH=.ci-shepherd-build/scripts \
  python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_models.py' -v
TMPDIR="$PWD/.ci-shepherd-build/tests/.tmp" \
  PYTHONPATH=.ci-shepherd-build/scripts \
  python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_scripts.py' -v
(
  cd .ci-shepherd-build/tests
  TMPDIR="$PWD/.tmp" PYTHONPATH=../scripts python3 -m unittest -v \
    test_collector \
    test_enrichment \
    test_ownership
)
```

Expected: the new tests FAIL because report roles are ignored, role conflicts
and availability typos are accepted, roleless scoped evidence can be omitted,
the prompt resolves scripts relative to the target checkout, and bounded
supporting/history facts, normalized causes, and associations are absent.

- [ ] **Step 3: Implement bounded facts and semantic report roles without changing dispositions**

Set explicit runnable-profile budgets in `scripts/collect.py`:

```text
max supporting closed issues: 20
max references followed per issue: 5
max exact-marker candidates per issue: 3
max normalized-fact candidates per issue: 3
```

Enable supporting collection and explicit issue-reference enrichment while
keeping issue timelines off. Preserve deterministic truncation warnings and
prioritize explicit issue references within the global supporting budget.
Apply that global budget before out-of-lookback issue-detail probes and retain
budget-excluded references as explicit `not-enriched` evidence.
Record `supportingSearch: {complete, candidateIssueNumbers, truncated}` on
every open issue and its evidence record. A relevant collection error or
budget or depth truncation makes `complete` false. Candidate lists contain only
selected issues.

When traversal discovers an issue beyond the depth limit or global budget, add
`supportingSelection: {state: "excluded", reasons, rootIssueNumbers}` to the
inventory reference. Preserve the reference for provenance, but make generic
enrichment honor the exclusion rather than GETing the issue later. Emit a
`not-enriched` evidence stub with the issue identity and sorted `referencedBy`
associations for every affected root.

For every selected supporting issue, merge a sorted, deduplicated
`referencedBy` association into its issue evidence for each open issue whose
reference or match selected it. Preserve the direct source evidence association
for explicit references. For aggregate inferred marker/fact selection without
one source evidence item, use the stable open issue evidence ID together with
`sourceIssueNumber` and extraction method `marker-match` or `fact-match`.

Keep the runnable profile's minimal run expansion: at most 10 selected
referenced runs, only the current attempt, at most 10 failed jobs, and at most
three failed-job logs. Add an explicit history option that makes one
first-page `per_page=10` workflow/branch history GET per selected run and never
paginate it. Record `recentHistory`, `recentHistoryCollected`,
`recentHistoryTruncated`, `recentHistoryTotalCount`,
`historyCoversSourceRun`, and `recentHistoryGap`.

Set coverage true only when the bounded window proves every run newer than the
source is included: the source run appears in the returned window, or the whole
history fits because a reported total at most 10 agrees with the returned count
or fewer than 10 results are returned without a total. Exactly 10 results
without a total remains potentially truncated. Missing source run identity or
timestamps, malformed responses, endpoint errors, and a source older than a
truncated window leave coverage false; collection errors also leave
`recentHistoryCollected` false.
Default enrichment behavior remains unchanged for callers that do not supply
the new option.

Put `sourceIssueNumber` directly on issue comments and timeline events. Build
sorted, deduplicated path associations from local pull-request and commit
`referencedBy` data and copy them to source-path and CODEOWNERS payloads.
Never associate an external-repository path with local ownership evidence.

Preserve the approved states, actions, state/action pairs, action gates,
identity checks, incident-only restrictions, case-insensitive repository
matching, and relationship validation. Add finite schema constants:

```python
EVIDENCE_ROLES = frozenset({
    "canonical-issue",
    "canonical-search-complete",
    "current-failing-run",
    "deterministic-marker",
    "known-flaky-signature",
    "merged-fix",
    "newer-failure",
    "no-newer-matching-failure",
    "no-recent-matching-failure",
    "normalized-cause",
    "normalized-facts",
    "obsolete-surface",
    "post-fix-green",
    "prior-resolved-episode",
    "recurrence",
    "recovery",
})
EVIDENCE_AVAILABILITIES = frozenset({
    "available",
    "expired-or-unavailable",
    "not-enriched",
    "partial",
})
```

Evidence references have `{id, kind, role?, roles?, normalizedCause?}`.
Validate optional snapshot and reference roles against `EVIDENCE_ROLES`, and
require every supplied normalized cause to be a nonempty string. Reject using
`role` and `roles` together. Require `roles` to be nonempty and unique. Resolve
the effective roles as:

```python
report_roles = [reference_role] if reference_role is not None else roles
if payload_role is not None and report_roles not in ([], [payload_role]):
    raise ValidationError("report role conflicts with snapshot role")
effective_roles = [payload_role] if payload_role is not None else report_roles
```

Resolve the effective normalized cause similarly:

```python
if payload_cause is not None and reference_cause is not None and payload_cause != reference_cause:
    raise ValidationError("report normalizedCause conflicts with snapshot normalizedCause")
effective_cause = payload_cause if payload_cause is not None else reference_cause
```

This keeps deterministic fixture compatibility while leaving the collector
factual. Evaluate identity, current-source status, availability, issue scope,
and factual proof independently for every effective role. Only current,
`available` records in supporting `evidence` satisfy positive roles;
contradictory, missing, and previous-report references do not.
In addition, `canonical-search-complete` requires an available issue payload
with completed, non-truncated `supportingSearch`, while
`no-newer-matching-failure` and `no-recent-matching-failure` require available
workflow-run evidence with `recentHistoryCollected: true`, a list-valued
`recentHistory`, a boolean `recentHistoryTruncated`, and
`historyCoversSourceRun: true`. Empty candidate or history lists do not satisfy
these roles by themselves.

Require factual success for `post-fix-green`: a workflow-run record must either
be successful itself or include a successful run in rigorously covered
`recentHistory`; a workflow-job record must itself be successful. Chronology
relative to merged-fix or recovery evidence remains an agent judgment. A
multi-role reference has one effective `normalizedCause`, but only roles that
require cause equality consume it.

Required positive evidence must be associated with the decision issue through
`payload.sourceIssueNumber`, `payload.referencedBy[*].sourceIssueNumber`, or
the decision issue's own compact or repository-qualified issue evidence
record. Canonical-target and prior-episode evidence from another issue needs
that association even when a relationship points to the same target.

For every high-risk action, scan only current records deterministically scoped
to the decision issue. Require every scoped record to appear exactly once
across `evidence`, `contradictoryEvidence`, and `missingEvidence`, regardless
of availability and regardless of whether it has a role. Exclude
previous-report and unrelated records. Preserve bucket exclusivity.

Snapshot roles are authoritative, so a report cannot relabel a deterministic
blocker. The unchanged disposition gates remain:

- `close`: `merged-fix` or `recovery`, `post-fix-green`,
  `no-newer-matching-failure`, and no `newer-failure`.
- `close-resolved`: `merged-fix` or `recovery`, `post-fix-green`, and
  `no-newer-matching-failure`.
- `close-stale`: `obsolete-surface` and `no-recent-matching-failure`; issue age
  alone is never sufficient.
- `close-as-tracked`: `canonical-issue` plus a `canonical-tracker` or
  `exact-duplicate` relationship.
- `open-dedicated-issue`: `current-failing-run`, `recurrence` or
  `known-flaky-signature`, and `canonical-search-complete`.
- `merge-duplicate`: retain its existing identity, matching-fact, and
  relationship gates.
- `open-regression`: require `current-failing-run`,
  `prior-resolved-episode`, and `normalized-cause` supporting references to
  have equal nonempty effective normalized causes, in addition to its existing
  prior-identity and `regression-of` relationship gates.

Keep the generic `close` action valid only for compatibility with existing
prototype reports, with the same gates as `close-resolved`. The four explicit
Task 10 actions remain:

```python
{
    "close-resolved",
    "close-stale",
    "close-as-tracked",
    "open-dedicated-issue",
}
```

- [ ] **Step 4: Update the shepherd reasoning contract**

Document that `ci/main` issues are incident records. A recurring flaky test or
infrastructure defect belongs in a separate canonical problem issue. Require
the following order:

1. Determine whether the incident is active, resolved, flaky, duplicate, or
   obsolete.
2. For probable flakes, search for a canonical issue before recommending a new
   one.
3. If no canonical issue exists, recommend `open-dedicated-issue` and keep the
   incident open until that issue exists.
4. Once a canonical issue exists, recommend `close-as-tracked`.
5. Recommend `close-resolved` only after a verified fix/recovery, a later green
   run, and a completed search finding no newer matching failure.
6. Recommend `close-stale` only when the affected test/workflow/code path is
   removed or superseded and a bounded recent-history search finds no matching
   failure.

Show singular `role`, plural `roles`, and optional normalized causes in the
decision JSON example and state that these can be agent judgments over factual
collector records. Require complete citation of all current issue-scoped
records for high-risk recommendations, per-role factual proof, and no
overriding or supplementing deterministic snapshot roles or causes.

Provide a runnable invocation that preserves the target checkout and resolves
scripts from the skill directory:

```bash
CI_SHEPHERD_ROOT="/path/to/directory-containing-SKILL.md"
CHECKOUT="$PWD"
SCRATCH="$HOME/.copilot/ci-shepherd/manual-run"
umask 077
mkdir -p "$SCRATCH"
python3 "$CI_SHEPHERD_ROOT/scripts/collect.py" \
  --repository microsoft/aspire \
  --checkout "$CHECKOUT" \
  --output-dir "$SCRATCH"
```

Document deterministic endpoint-family/result budgets: at most 20 supporting
issue candidates receive enrichment, and at most 10 selected referenced runs
receive one first-page history request each. Do not translate these limits into
a total HTTP-request upper bound. Issue detail and comment endpoints can
paginate; the GitHub client stops after a page with fewer than 100 items but
has no fixed page-count cap, and it permits at most three attempts for each
page, detail, or log request. Cite
`.ci-shepherd-build/scripts/ci_shepherd/github.py` for these client behaviors.

Update the design document's input/output, lifecycle/action, resolution,
association, completeness, availability, and high-risk sections to match this
contract.

- [ ] **Step 5: Run the focused and complete prototype tests**

Run:

```bash
(
  cd .ci-shepherd-build/tests
  TMPDIR="$PWD/.tmp" PYTHONPATH=../scripts python3 -m unittest -v \
    test_collector \
    test_enrichment \
    test_ownership \
    test_models \
    test_scripts
)
TMPDIR="$PWD/.ci-shepherd-build/tests/.tmp" \
  PYTHONPATH=.ci-shepherd-build/scripts \
  python3 -m unittest discover -s .ci-shepherd-build/tests -v
```

Expected: all tests PASS.
