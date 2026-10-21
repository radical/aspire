<!-- analyze-ci-failure -->
⚠️ **CI Failure Analysis: Mixed Failures**

The CI build contains both transient and non-transient failures.

**Failed jobs:**
- `` Build | [link](https://example.test) @reviewers `quoted` `` — ```` phase | @reviewers ``` Authorization: Bearer [REDACTED] ```` (code-issue)

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

The CI will not be automatically rerun. Please review the failures above.