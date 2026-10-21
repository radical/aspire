<!-- analyze-ci-failure -->
⚠️ **CI Failure Analysis: Possible Flaky Test(s)**

The CI build failed due to test failure(s) that appear unrelated to the PR changes. These may be flaky tests.

**Failed jobs:**
- `` Build | [link](https://example.test) @reviewers `quoted` `` — ```` phase | @reviewers ``` Authorization: Bearer [REDACTED] ```` (flaky-test)

**Suspected flaky test(s):**
- `` Tests.First [`case`](https://example.test) @reviewers `` in job `` Build | [link](https://example.test) @reviewers `quoted` ``
  - **Error**:

        expected < actual
        ```
        @reviewers
        Authorization: Bearer [REDACTED]
  - **Stack Trace** (first frames):

        frame1
        frame2
        frame3
        frame4
        frame5
  - **Standard Output** (sensitive values redacted):

        safe stdout
        password=[REDACTED]
  - **Standard Error** (sensitive values redacted):

        safe stderr
        x-api-key: [REDACTED]
  - **Why likely flaky**: `` Intermittent | @reviewers `quoted` ``

**Suggested actions:**
- Re-run the failed CI jobs to confirm if the failure is intermittent
- If the test continues to fail, consider [quarantining it](https://github.com/microsoft/aspire/blob/main/docs/quarantined-tests.md) using `/quarantine-test <test name> <issue URL>`
- Search [existing issues](https://github.com/microsoft/aspire/issues?q=is%3Aissue+label%3Atest-failure) to see if this test is already known to be flaky

You can re-run the failed jobs from the [workflow run page](https://github.com/microsoft/aspire/actions/runs/123).