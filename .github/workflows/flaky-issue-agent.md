---
name: PR 21428 tool-visibility handoff probe
description: Verify a restricted apply_patch-only handoff without dispatching tests or changing a fix.
on:
  workflow_dispatch:
    inputs:
      issue_url:
        type: string
        required: true
      issue_number:
        type: string
        required: true
      state_revision:
        type: string
        required: true
if: github.repository == 'radical/aspire' && inputs.issue_url == 'https://github.com/microsoft/aspire/pull/21428' && inputs.issue_number == '21428' && inputs.state_revision == 'pr21428-tool-visibility-probe-20261030'
run-name: "PR 21428 apply_patch-only handoff probe"
concurrency:
  group: pr21428-visibility-${{ github.run_id }}
  cancel-in-progress: false
  job-discriminator: ${{ github.run_id }}
engine:
  id: copilot
  version: "1.0.85"
  model: gpt-6.1-sol
  bare: true
  args:
    - --reasoning-effort
    - high
    - --available-tools
    - apply_patch
    - --allow-tool
    - write(/tmp/gh-aw/pr21428-validation)
timeout-minutes: 5
env:
  GH_AW_HARNESS_MAX_RETRIES: "0"
  GH_AW_HARNESS_STARTUP_RETRIES: "0"
permissions:
  contents: read
  actions: read
  copilot-requests: write
network:
  allowed: [defaults, github]
tools:
  bash: false
  edit: false
  cli-proxy: false
pre-agent-steps:
  - name: Prepare the isolated handoff destination
    run: mkdir -p /tmp/gh-aw/pr21428-validation
post-steps:
  - name: Retain the restricted handoff
    if: always()
    uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
    with:
      name: pr21428-tool-visibility-handoff
      path: /tmp/gh-aw/pr21428-validation/visibility-probe.json
      if-no-files-found: error
---

# Restricted tool-visibility probe

This is a tool-handoff probe, not an assessment of PR 21428 or its test results.
You have only the apply_patch tool. Do not request or run any other tool.
Do not inspect or modify repository files. Do not create sessions, dispatch
tests, use network access, post comments, or propose a test fix.

Use apply_patch exactly once to create this absolute path:
`/tmp/gh-aw/pr21428-validation/visibility-probe.json`

Its complete content must be:

```json
{
  "version": 1,
  "state_revision": "pr21428-tool-visibility-probe-20261030",
  "status": "tool-handoff-only",
  "pr_validated": false,
  "test_dispatch_requested": false
}
```

Then end with the brief statement "Tool handoff created; no PR validation performed."
Trusted post-execution workflow code retains the file. Do not claim to
have uploaded it yourself or to have validated any test.
