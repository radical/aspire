---
description: |
  Analyzes failed CI builds using Copilot to determine whether the failure is
  transient (flaky test, infrastructure issue), caused by pull request changes,
  or a repository break on main. Pull request failures are reported on the PR;
  main repository breaks create a dedicated issue.

on:
  workflow_run:
    workflows: ["CI"]
    types:
      - completed
    # Intentional for now: only analyze CI runs for builds against main while this workflow is being validated.
    branches:
      - main
  workflow_dispatch:
    inputs:
      run_id:
        description: "CI workflow run ID to analyze"
        required: true
        type: number

jobs:
  collect-data:
    runs-on: ubuntu-latest
    if: >-
      github.repository_owner == 'microsoft'
      && (
        github.event_name == 'workflow_dispatch'
        || (
          github.event.workflow_run.conclusion == 'failure'
          && github.event.workflow_run.run_attempt <= 3
        )
      )
    permissions:
      contents: read
      actions: read
      checks: read
      pull-requests: read
    outputs:
      has-work: ${{ steps.collect.outputs.has_work }}
      run_id: ${{ steps.collect.outputs.run_id }}
      run_attempt: ${{ steps.collect.outputs.run_attempt }}
      run_url: ${{ steps.collect.outputs.run_url }}
      run_scope: ${{ steps.collect.outputs.run_scope }}
      pr_numbers: ${{ steps.collect.outputs.pr_numbers }}
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Checkout data collection helpers
        uses: actions/checkout@v7.0.1
        with:
          sparse-checkout: |
            eng/test-retry-patterns.json
            .github/workflows/analyze-ci-failure-history.sh
            .github/workflows/analyze-ci-failure-candidates.sh
            .github/workflows/analyze-ci-failure-persistence.sh
            .github/workflows/analyze-ci-failure-collect.sh
            .github/workflows/analyze-ci-failure-summary.sh
          sparse-checkout-cone-mode: false
      - name: Collect CI failure data
        id: collect
        env:
          REPO: ${{ github.repository }}
          MANUAL_RUN_ID: ${{ inputs.run_id }}
          WORKFLOW_RUN_ID: ${{ github.event.workflow_run.id }}
          WORKFLOW_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}
          EVENT_NAME: ${{ github.event_name }}
        run: bash .github/workflows/analyze-ci-failure-collect.sh
      - name: Create analysis summary
        if: steps.collect.outputs.has_work == 'true'
        env:
          RUN_ID: ${{ steps.collect.outputs.run_id }}
          RUN_ATTEMPT: ${{ steps.collect.outputs.run_attempt }}
          RUN_URL: ${{ steps.collect.outputs.run_url }}
          RUN_SCOPE: ${{ steps.collect.outputs.run_scope }}
          PR_NUMBERS: ${{ steps.collect.outputs.pr_numbers }}
        run: bash .github/workflows/analyze-ci-failure-summary.sh
      - uses: actions/upload-artifact@v7.0.1
        if: steps.collect.outputs.has_work == 'true'
        with:
          name: ci-failure-data
          path: ci-failure-data/

if: needs.collect-data.outputs.has-work == 'true'

env:
  # Set to 'true' to actually rerun failed CI jobs on transient failures.
  # Set to 'false' for dry-run mode: the agent still analyzes and comments
  # on the PR, but the comment will note that it was a dry run and no rerun
  # was triggered. Comments are intentionally posted even in dry-run mode to
  # provide visibility into CI failure classifications for debugging and
  # validation of the analysis quality.
  ENABLE_RERUN: 'false'

# Publication performs two ordinary memory pushes around issue side effects, so serialize every
# analysis. The maximum queue preserves pending work that the default single queue would replace.
concurrency:
  group: analyze-ci-failure
  cancel-in-progress: false
  queue: max

permissions:
  contents: read
  actions: read
  checks: read
  pull-requests: read
  issues: read
  copilot-requests: write

network:
  allowed:
    - defaults
    - github

safe-outputs:
  jobs:
    publish-data:
      name: "Publish analysis data and comment on PR"
      description: |
        Publishes the CI failure analysis to the memory branch, then posts a PR
        comment or updates a main-breakage issue according to the trusted scope.
        The agent must write:
          - /tmp/gh-aw/agent/analysis-result.json (run summary)
          - /tmp/gh-aw/agent/causes/*.json (one file per failure cause)
        Emit exactly one `publish_data` item with run_id and pr_numbers.
      runs-on: ubuntu-latest
      needs: [safe_outputs]
      if: needs.detection.result == 'success' && needs.detection.outputs.detection_success == 'true' && needs.safe_outputs.result == 'success'
      permissions:
        actions: read
        contents: write
        issues: write
        pull-requests: write
      inputs:
        run_id:
          description: "The workflow run ID that was analyzed."
          required: true
          type: number
        pr_numbers:
          description: "The unambiguous subject PR number, or an empty string."
          required: true
          type: string
      env:
        GH_TOKEN: ${{ github.token }}
      steps:
        - name: Download CI analysis files
          id: download-analysis
          uses: actions/download-artifact@v8.0.1
          with:
            name: ci-analysis-output
            path: ${{ runner.temp }}/ci-analysis-output
        - name: Checkout publication helpers
          uses: actions/checkout@v7.0.1
          with:
            persist-credentials: false
            sparse-checkout: |
              .github/workflows/analyze-ci-failure-validation.sh
              .github/workflows/analyze-ci-failure-persistence.sh
              .github/workflows/analyze-ci-failure-comment.sh
              .github/workflows/analyze-ci-failure-issue.sh
              .github/workflows/analyze-ci-failure-publish.sh
              .github/workflows/analyze-ci-failure-publish-comment.sh
            sparse-checkout-cone-mode: false
        - uses: actions/download-artifact@v8.0.1
          with:
            name: ci-failure-data
            path: ci-failure-data/
        - name: Validate analysis scope
          env:
            ANALYSIS_DIR: ${{ steps.download-analysis.outputs.download-path }}
          run: bash .github/workflows/analyze-ci-failure-validation.sh
        - name: Publish analysis data and comment on PR
          env:
            ANALYSIS_DIR: ${{ steps.download-analysis.outputs.download-path }}
            REPO: ${{ github.repository }}
          run: bash .github/workflows/analyze-ci-failure-publish.sh
        - name: Comment on PR
          env:
            ANALYSIS_DIR: ${{ steps.download-analysis.outputs.download-path }}
            REPO: ${{ github.repository }}
          run: bash .github/workflows/analyze-ci-failure-publish-comment.sh
    rerun-failed-jobs:
      name: "Rerun failed CI jobs"
      description: |
        Reruns the failed CI jobs when the agent determines all failures are
        transient infrastructure issues. Emit exactly one `rerun_failed_jobs`
        item with the run_id and pr_numbers when a rerun is warranted.
      runs-on: ubuntu-latest
      needs: [safe_outputs]
      if: needs.detection.result == 'success' && needs.detection.outputs.detection_success == 'true' && needs.safe_outputs.result == 'success'
      permissions:
        actions: write
        contents: read
        pull-requests: write
      inputs:
        run_id:
          description: "The workflow run ID to rerun failed jobs for."
          required: true
          type: number
        pr_numbers:
          description: "The unambiguous subject PR number, or an empty string."
          required: true
          type: string
        reason:
          description: "Short summary of why the rerun was requested."
          required: true
          type: string
      steps:
        - name: Checkout rerun helper
          uses: actions/checkout@v7.0.1
          with:
            sparse-checkout: |
              .github/workflows/analyze-ci-failure-rerun.js
            sparse-checkout-cone-mode: false
        - name: Download CI analysis files for rerun
          id: download-analysis
          uses: actions/download-artifact@v8.0.1
          with:
            name: ci-analysis-output
            path: ${{ runner.temp }}/ci-analysis-output
        - uses: actions/download-artifact@v8.0.1
          with:
            name: ci-failure-data
            path: ci-failure-data/
        - name: Rerun failed jobs
          uses: actions/github-script@v9.0.0
          env:
            ENABLE_RERUN: ${{ env.ENABLE_RERUN }}
            ANALYSIS_DIR: ${{ steps.download-analysis.outputs.download-path }}
          with:
            script: |
              const rerun = require('./.github/workflows/analyze-ci-failure-rerun.js');
              await rerun.run({ github, context, core });
steps:
  - uses: actions/download-artifact@v8.0.1
    with:
      name: ci-failure-data
      path: ci-failure-data/

# Custom agent files are not included in gh-aw's diagnostic agent artifact.
post-steps:
  - name: Upload CI analysis files
    uses: actions/upload-artifact@v7.0.1
    with:
      name: ci-analysis-output
      path: |
        /tmp/gh-aw/agent/analysis-result.json
        /tmp/gh-aw/agent/causes/*.json
      if-no-files-found: error
---

# Analyze CI Failure

You are analyzing a failed CI build in the **microsoft/aspire** repository. Your job is to determine the root cause of the failure and take the appropriate action. The run scope in the summary was derived deterministically from the failed run's immutable `event` and `head_branch`; never infer or change it based on associated pull requests.

## Workflow

### Step 1: Read the summary file

Read `ci-failure-data/analysis-summary.md`. It contains the run information, PR metadata, failed jobs, error-focused logs, annotations, test failures, PR changed files, and known transient failure patterns.

### Step 2: Analyze

Classify each failed job from its current failed step and diagnostic before comparing it to prior causes (see **Classification Rules** below). A job's name describes what it was intended to run, not what actually failed.

Read the step conclusions in `ci-failure-data/failed-jobs.json`, including skipped steps. Distinguish setup, restore/build, test execution, and artifact-upload failures. For example, a prerequisite download that returns HTTP 504 with curl exit code 22 while the test step is skipped is an infrastructure/download failure, not a flaky test or a recurrence of an earlier test assertion failure.

Missing logs are a collection problem, not a reason to infer a known test failure. Use the saved fetch diagnostics, annotations, and test-result artifacts to establish what happened; do not substitute a prior cause's error for missing current evidence.

#### Matching against prior causes

When a failure is classified as `flaky-test`, `infra-failure`, or `main-repository-breakage` (NOT pull-request `code-issue`), check the **Prior Causes** section in the summary for a match. Prior causes are loaded from JSON files in the `ci-failure-data/prior-causes/` directory (one file per cause, e.g. `ci-failure-data/prior-causes/nuget-feed-timeout.json`). These files are fetched by the `collect-data` job from the `memory/ci-failure-analysis` branch's `causes/` directory and rendered into the summary under the "Prior Causes (from memory branch)" heading.

If any of this run's tracked failures match an existing cause, you MUST reuse that cause's `id` when writing the cause file in Step 3b. This allows the publish job to merge occurrences into the existing cause rather than creating duplicates. Do NOT attempt to match code-issue failures against prior causes — those are not tracked.

A failure matches an existing cause only when its failure category, failing phase, and current diagnostic agree:
- For flaky tests: the test actually ran and failed, its full test name matches `test_name`, and its observed error/stack trace substantially matches the prior cause's `error_pattern`.
- For infra failures: the current failed operation and diagnostic substantially match the prior infra-failure cause, including relevant HTTP status or exit codes.
- For main repository breakages: the deterministic failure substantially matches the `error_pattern` of a prior main-repository-breakage cause
- A job or shard name is not a test name. A failed prerequisite and a skipped test cannot match a test failure, even if the job name is identical.

When reusing an existing cause, keep the same `id` and `type`. Copy the existing `title`, `test_name`, and `error_pattern` when practical; the publisher treats the previously stored values as authoritative and will not let a later run rewrite them. Add the current run's `job_ids` as described below and add the cause ID to the `causes` array in the run summary.

### Step 3: Write the analysis JSON files

Write two types of files:

#### 3a. Run summary file

Write the run summary to `/tmp/gh-aw/agent/analysis-result.json`. The JSON must follow this schema:

```json
{
  "run_id": 12345,
  "run_attempt": 1,
  "run_url": "https://github.com/microsoft/aspire/actions/runs/12345",
  "run_scope": "main | pull-request",
  "analyzed_at": "2026-06-30T12:00:00Z",
  "verdict": "transient-infra | flaky-test | code-issue | main-repository-breakage | mixed",
  "pr": {
    "number": 1234,
    "title": "PR title",
    "author": "username",
    "state": "open",
    "head_branch": "feature-branch",
    "base_branch": "main",
    "url": "https://github.com/microsoft/aspire/pull/1234"
  },
  "triggering_merge_pr": null,
  "main_context": null,
  "failed_jobs": [
    {
      "name": "Build and Test (ubuntu-latest)",
      "id": 67890,
      "conclusion": "failure",
      "url": "https://github.com/microsoft/aspire/actions/runs/12345/job/67890",
      "classification": "transient-infra | flaky-test | code-issue | main-repository-breakage",
      "reason": "Brief explanation of why this job failed",
      "failed_steps": ["step1", "step2"]
    }
  ],
  "failed_tests": [
    {
      "name": "Fully.Qualified.TestName",
      "job": "job-name",
      "error": "the error message from the test failure",
      "stack_trace": "the stack trace from the test failure (first few frames)",
      "standard_output": "standard output captured by the test result",
      "standard_error": "standard error captured by the test result",
      "classification": "flaky | code-issue",
      "reason": "Why this test is classified this way"
    }
  ],
  "causes": ["cause-id-1", "cause-id-2"]
}
```

Field details:
- `run_scope`: Copy the immutable run scope from the summary exactly.
- `verdict`: The overall classification. Use `"transient-infra"` when every failed job is an infrastructure issue, `"flaky-test"` when at least one failed job is a flaky test and every failed job is transient, `"code-issue"` when every failed job is caused by pull request changes, `"main-repository-breakage"` when every failed job is a deterministic repository failure on main, or `"mixed"` when transient and non-transient failures occur together.
- `pr`: For pull-request scope, include the subject PR object when the summary provides one; otherwise use `null`. For main scope, this MUST be `null`.
- `triggering_merge_pr`: For main scope, include the triggering merge PR from the summary when available. It is non-causal context and MUST NOT be copied to `pr`. For pull-request scope, this is `null`.
- `main_context`: For main scope, include `last_successful_main_sha`, `failed_sha`, and `candidate_merges` from the summary. For pull-request scope, this is `null`.
- `failed_jobs[].classification`: Per-job classification — one of `"transient-infra"`, `"flaky-test"`, `"code-issue"`, or `"main-repository-breakage"`.
- `failed_jobs[].reason`: A single-line explanation, limited to 500 characters.
- `failed_jobs` MUST contain exactly one object for every failed job in the summary, using its exact numeric ID, with no additions, omissions, or duplicates.
- When trusted structured test evidence is complete, `failed_tests` MUST contain exactly one entry for every `{name, job}` pair in the summary, with no additions, omissions, or duplicates. When no supported structured test result is available, use an empty array. Do not infer failed tests from job logs.
- `failed_tests[].name`: The exact single-line test name from the structured artifact, limited to 500 characters.
- `failed_tests[].job`: The exact failed job name from the summary, limited to 500 characters.
- `failed_tests[].classification`: Per-test classification — `"flaky"` or `"code-issue"`.
- `failed_tests[].error`: Copy the error message from the matching structured test failure.
- `failed_tests[].stack_trace`: Copy the stack trace from the matching structured test failure, or use `null` when it is absent.
- `failed_tests[].standard_output`: Copy the standard output from the matching structured test failure, or use an empty string when it is absent.
- `failed_tests[].standard_error`: Copy the standard error from the matching structured test failure, or use an empty string when it is absent.
- The validator replaces `error`, `stack_trace`, `standard_output`, and `standard_error` with bounded trusted artifact values before publication.
- `failed_tests[].reason`: A single-line explanation, limited to 500 characters.
- `analyzed_at`: The current UTC timestamp in ISO 8601 format.
- `causes`: An array of at most 10 cause IDs (strings) that were identified for this run. These correspond to the cause files written in Step 3b. The publish job uses this to add an occurrence entry to each referenced cause. Empty array `[]` for code-issue verdicts. `causes` MUST cover every `transient-infra` failed job with an `infra-failure` cause, every `flaky-test` failed job with a `flaky-test` cause, every flaky `{name, job}` test identity with an exactly matching `flaky-test` cause, and every `main-repository-breakage` failed job with a `main-repository-breakage` cause. `code-issue` jobs are exempt. Group failures only when they have the same underlying root cause and, for flaky failures, the same test identity. The 10-cause publication budget is fail-closed: never combine or omit distinct flaky tests merely to fit within it.

#### 3b. Per-cause files

For each distinct underlying cause that is NOT a pull-request code issue, write a separate JSON file to `/tmp/gh-aw/agent/causes/<cause-id>.json`. The `<cause-id>` should be a filesystem-safe identifier derived from the cause (e.g., sanitized test name for flaky tests, or a short descriptive slug for infrastructure issues and main repository breakages). Do NOT create cause files for `code-issue` classifications — those are the PR author's responsibility and are not tracked as recurring CI problems.

Each cause file must follow this schema:

```json
{
  "id": "cause-id",
  "type": "flaky-test | infra-failure | main-repository-breakage",
  "title": "Human-readable short description of the cause",
  "test_name": "Fully.Qualified.TestName (required for flaky-test)",
  "error_pattern": "The key error message or pattern that identifies this cause",
  "job_ids": [123456789]
}
```

Field details:
- `id`: Must match the filename (without `.json`). Use lowercase with hyphens. For flaky tests, derive from the test name (e.g., `aspire-hosting-tests-mytest`). For infra failures, use a descriptive slug (e.g., `nuget-feed-timeout`, `docker-registry-rate-limit`).
- `type`: One of `"flaky-test"`, `"infra-failure"`, or `"main-repository-breakage"`. Do NOT create cause files for pull-request code-issue classifications.
- `title`: A brief, single-line human-readable description of at most 238 characters (e.g., "Flaky: MyNamespace.MyTest times out intermittently", "NuGet feed connection timeout").
- `test_name`: A `flaky-test` cause MUST include a `test_name` that exactly matches a `failed_tests` entry classified as `"flaky"`, limited to 500 characters. Omit this field for infrastructure failures; infrastructure causes MUST NOT include a non-empty `test_name`.
- `error_pattern`: The actual error message and relevant stack trace from the failure. For flaky tests, use the error message and first few stack trace frames from the structured test data. For infra failures, use the error text from the job logs. Include enough detail to identify and reproduce the issue, up to 500 characters. Use LF for multiline text and omit ANSI styling or other control characters.
- `job_ids`: A non-empty array of unique numeric IDs for the failed jobs where this cause occurred. Use only IDs from the trusted failed-job summary; do not write job names. An `infra-failure` cause may reference only `transient-infra` jobs, and a `main-repository-breakage` cause may reference only `main-repository-breakage` jobs. Every job referenced by a `flaky-test` cause must have a `"flaky"` `failed_tests` entry whose `name` exactly matches the cause's `test_name` and whose `job` exactly matches that trusted job name.

Do NOT include an `occurrences` field — the publish job builds occurrences automatically from the run summary JSON. The publisher derives display names from trusted job metadata and removes `job_ids` before storing the stable cause definition.

Create the `/tmp/gh-aw/agent/causes/` directory and write one `.json` file per distinct cause, with at most 10 cause files for the run. Multiple failed tests with the same root cause (e.g., same infrastructure error) can be grouped into a single cause file. When a failure matches an existing prior cause, use the same filename (`<cause-id>.json`) so the publish job merges correctly.

### Step 4: Take action

Determine the overall verdict and proceed to the **Actions** section.

## Input Data

The file `ci-failure-data/analysis-summary.md` contains the full failure data:
- The failed workflow run information
- PR metadata (number, title, author, state, branch)
- Failed jobs and all step conclusions, including failed and skipped steps
- Job logs (error-focused extracts)
- Job annotations
- Test failures extracted from structured test artifacts (test name and error message)
- PR changed files
- Known transient failure patterns from `eng/test-retry-patterns.json`
- **Prior causes** from the memory branch (previously identified recurring failures with their IDs and occurrence history)

## Classification Rules

Apply rules based on the immutable run scope:

- For `pull-request`, determine whether the PR changes caused the failure and report deterministic failures as `code-issue`.
- For `main`, consider the complete candidate merge range since the last successful main run. The triggering merge PR is context only and is not necessarily causal. Deterministic compilation, test, API compatibility, lint, or formatting failures are `main-repository-breakage`; they MUST NOT be classified as infrastructure merely because they are unrelated to the triggering merge PR.

Classify each failed job into one of these categories:

### 1. Transient Infrastructure Failure

The failure was caused by infrastructure issues outside the PR author's control. Indicators:
- Network errors: `ECONNRESET`, `ECONNREFUSED`, `ENOTFOUND`, `Could not resolve host`, `Connection reset by peer`
- Prerequisite/tool download failures: HTTP 5xx responses, such as `curl: (22) The requested URL returned error: 504`
- SSL/TLS failures: `The SSL connection could not be established`
- Timeout errors not caused by test code: `Operation timed out`, `A connection attempt failed`
- Container registry rate limiting: `403 Forbidden` from `mcr.microsoft.com`, `The request is blocked`
- GitHub runner issues: `The job was not acquired by Runner`, `The hosted runner lost communication`
- NuGet feed failures: errors from `pkgs.dev.azure.com/dnceng` or `dnceng.pkgs.visualstudio.com`
- Git operation failures: `expected 'packfile'`, `RPC failed`, `Recv failure`
- Windows process init: `0xC0000142`, exit code `-1073741502`
- Steps like "Set up job", "Checkout code", "Set up .NET Core" failing with transient errors

### 2. Transient Test Failure (Flaky Test)

A test actually ran and failed transiently rather than because repository code changed. Establish the failing test and its diagnostic from the current trusted structured test evidence first. Missing logs, an exit code, a matching job name, or unrelated PR files do not establish a test failure. In particular, HTTP/curl failures in prerequisite downloads are setup failures, not flaky tests.

PR-file relationships are indicators only for pull-request scope; main-scope `flaky-test` classification requires independent transient evidence. After establishing a real test failure, indicators of flakiness include:
- The test failure message matches a known transient pattern from `eng/test-retry-patterns.json`
- The failing test is in a code area NOT modified by the PR (check the PR changed files)
- The failure shows intermittent/timing-related errors (race conditions, port conflicts, timeout in integration tests)
- The test name or namespace does not correspond to any file changed in the PR
- The error message shows environmental issues (Docker connectivity, service availability, port already in use)

Classify a job as `flaky-test` only when the summary contains a specific structured test failure. Every `flaky-test` cause must identify that validated test.

### 3. Non-Transient Failure (PR Code Issue)

The failure was directly caused by changes in the PR. Indicators:
- **Build/compilation errors**: `error CS`, `error MSB`, `Build FAILED`, syntax errors in files changed by the PR
- **Test failures in PR-modified code**: test assertions fail in tests that test functionality changed by the PR
- **New test failures**: tests that previously passed now fail due to behavioral changes from the PR
- **API compatibility failures**: public API surface changes that break compatibility
- **Lint/format errors**: code style violations in PR-changed files

This classification is valid only for pull-request scope.

### 4. Main Repository Breakage

The failure is a deterministic code or repository failure on main. Indicators:
- Compilation or build errors caused by the combined repository state
- Deterministic test, API compatibility, lint, or formatting failures on main
- Semantic merge conflicts where independently valid changes are incompatible together

Use all candidate merges since the last successful main run when investigating. Name a specific PR as causal only when the logs and changed code provide direct evidence and candidate history comes from a complete `ahead` comparison. Identical, behind, diverged, malformed, or incomplete comparisons are non-attributable; report only repository-level evidence and do not name any PR as causal, including the triggering merge.

## Analysis Process

1. Read `ci-failure-data/analysis-summary.md`
2. For each failed job, examine:
   - The failed step names
   - The job log output for error messages
   - The job annotations
3. Cross-reference failures against:
   - The known transient failure patterns
   - For pull requests, the PR changed files list
   - For main, all candidate merges since the last successful main run
4. Classify each failed job
5. Determine the overall verdict and proceed to **Actions**

## Actions

After writing the JSON files (summary + per-cause), take action based on the verdict:

### If ALL failures are Transient Infrastructure Failures:

Set `verdict` to `"transient-infra"` in the JSON. Set `failed_tests` to an empty array for `transient-infra`; a run with any reported failed test must use `flaky-test`, `code-issue`, or `mixed` according to the evidence. Check the `ENABLE_RERUN` environment variable (set in the workflow `env:` block).

**If `ENABLE_RERUN` is `'true'`:** Emit the `rerun-failed-jobs` safe output to rerun the failed CI jobs.

**Regardless of `ENABLE_RERUN`:** Emit the `publish-data` safe output so the analysis is pushed to the memory branch and a PR comment is posted.

### If failures include Transient Test Failures and no deterministic failures:

Set `verdict` to `"flaky-test"` in the JSON. Ensure `failed_tests` entries have `classification: "flaky"` and include a `reason` explaining why the test is likely flaky.

Emit the `publish-data` safe output. Do NOT emit `rerun-failed-jobs`.

### If ALL failures are Non-Transient PR Code Issues:

Set `verdict` to `"code-issue"` in the JSON. Ensure `failed_jobs` entries have `classification: "code-issue"` with a clear `reason` linking the error to PR changes.

Emit the `publish-data` safe output. Do NOT emit `rerun-failed-jobs`.

### If ALL failures are Main Repository Breakages:

Set `verdict` to `"main-repository-breakage"` in the JSON. Set `pr` to `null`, populate `triggering_merge_pr` only as non-causal context when candidate history comes from a complete `ahead` comparison, and include the main candidate range in `main_context`. Otherwise, do not identify a causal PR or claim a candidate range. Write a `main-repository-breakage` cause file so the publish job creates or updates the dedicated main-CI-break issue. The publisher derives the public issue title and diagnostic text from trusted run context; agent-proposed main-breakage title and error-pattern fields are not published as attribution.

Emit the `publish-data` safe output. Do NOT emit `rerun-failed-jobs`.

### Mixed Failures

If there are both transient and non-transient failures, set `verdict` to `"mixed"`. Report all findings with per-job and per-test classifications.

A single failed job can contain both a deterministic failure and a flaky failed test. In that case, classify the job by the deterministic failure, include the flaky test and its cause, and use `mixed` so neither failure is omitted.

Emit the `publish-data` safe output. Do NOT emit `rerun-failed-jobs`.

## Important Rules

1. **Always write the run summary** — every analysis must produce `/tmp/gh-aw/agent/analysis-result.json`. Write cause files in `/tmp/gh-aw/agent/causes/` for `flaky-test`, `infra-failure`, and `main-repository-breakage` causes (NOT for pull-request `code-issue`).
2. **Always emit the `publish-data` safe output** — with `run_id` and `pr_numbers` so the publish-data job can push the data and post a comment.
3. **Never rerun when there are code issues** — only emit `rerun-failed-jobs` for pure infrastructure failures with `ENABLE_RERUN` set to `'true'`.
4. **Be specific** — include actual error messages and job/test names in the JSON fields.
5. **Use scope-appropriate history** — cross-reference PR files only for pull-request scope; for main scope, consider every candidate merge since the last successful main run.
6. **PR-directed effects require an open, unlocked PR** — for pull-request scope, use the "Pull Request" section as analysis context even when the PR is closed or locked. Still emit `publish-data` so run-scoped persistence can continue; the publication and rerun jobs recheck live PR state immediately before any PR-directed mutation.
7. **Do NOT use MCP to query GitHub** — all needed data (PR metadata, changed files, job logs, annotations) is already in the summary file. No GitHub API tools are available.
8. **Do NOT post PR comments directly** — the `publish-data` job handles commenting using the JSON file. Do not use `add-comment`.
