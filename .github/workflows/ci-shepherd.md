---
description: Proves a packet-bound, fresh CI Shepherd decision transport without external effects.
on:
  workflow_dispatch:

concurrency:
  group: ci-shepherd-transport-proof
  cancel-in-progress: false

permissions:
  contents: read
  actions: read
  copilot-requests: write

timeout-minutes: 10
max-ai-credits: 5
max-turns: 12

engine:
  id: copilot
  version: 1.0.92-3
  bare: true
  harness:
    max-retries: 0
  env:
    AWF_SESSION_STATE_DIR: ${{ github.workspace }}/artifacts/ci-shepherd/session-state
    # Empty expressions preserve string bindings in the pinned compiler;
    # literal empty strings are emitted as YAML null.
    GH_TOKEN: ${{ '' }}
    GITHUB_TOKEN: ${{ '' }}
    GH_AW_GITHUB_TOKEN: ${{ '' }}
    GH_AW_GITHUB_MCP_SERVER_TOKEN: ${{ '' }}
  args:
    - --no-auto-update
    - --disable-builtin-mcps
    - --no-remote
    - --no-remote-export
    - --no-ask-user
    - --available-tools
    - safeoutputs-submit_decision
    - --deny-tool
    - shell
    - write
    - task
    - --log-dir
    - artifacts/ci-shepherd/host-logs
    - --output-format
    - json
    - --silent

network:
  allowed:
    - defaults

tools:
  github: false
  bash: false
  edit: false
  cli-proxy: false

checkout:
  github-token: ${{ secrets.GITHUB_TOKEN }}
  ref: ${{ github.workflow_sha }}
  sparse-checkout: |
    .github/workflows/ci-shepherd

jobs:
  prepare:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    outputs:
      packet: ${{ steps.packet.outputs.packet }}
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          ref: ${{ github.workflow_sha }}
          persist-credentials: false
          sparse-checkout: .github/workflows/ci-shepherd
          sparse-checkout-cone-mode: false
      - name: Prepare host-owned envelope
        id: packet
        run: python3 .github/workflows/ci-shepherd/round.py prepare --workdir artifacts/ci-shepherd/prepared
      - uses: actions/upload-artifact@v7.0.1
        with:
          name: ci-shepherd-prepare-${{ github.run_id }}-${{ github.run_attempt }}
          path: |
            artifacts/ci-shepherd/prepared/trusted/packet.json
            artifacts/ci-shepherd/prepared/trusted/envelope.json
          if-no-files-found: error
          retention-days: 2

steps:
  - uses: actions/download-artifact@v8.0.1
    with:
      name: ci-shepherd-prepare-${{ github.run_id }}-${{ github.run_attempt }}
      path: artifacts/ci-shepherd/prepared/trusted
  - name: Create empty reasoning directory
    run: mkdir -p artifacts/ci-shepherd/agent artifacts/ci-shepherd/host-logs

post-steps:
  - name: Collect host process and session evidence
    if: always()
    env:
      ENGINE_OUTCOME: ${{ steps.agentic_execution.outcome }}
      AWF_SESSION_STATE_DIR: ${{ github.workspace }}/artifacts/ci-shepherd/session-state
    run: |
      python3 .github/workflows/ci-shepherd/round.py collect \
        --trusted artifacts/ci-shepherd/prepared/trusted \
        --session-root "$AWF_SESSION_STATE_DIR" \
        --logs artifacts/ci-shepherd/host-logs \
        --out artifacts/ci-shepherd/evidence.json \
        --outcome "$ENGINE_OUTCOME"
  - name: Upload only host evidence
    if: always()
    uses: actions/upload-artifact@v7.0.1
    with:
      name: ci-shepherd-evidence-${{ github.run_id }}-${{ github.run_attempt }}
      path: |
        artifacts/ci-shepherd/evidence.json
        artifacts/ci-shepherd/failure.json
      if-no-files-found: error
      retention-days: 2

safe-outputs:
  github-token: ${{ secrets.GITHUB_TOKEN }}
  threat-detection: false
  activation-comments: false
  report-failure-as-issue: false
  report-failed-jobs: false
  missing-tool: false
  missing-data: false
  report-incomplete: false
  noop: false
  jobs:
    submit-decision:
      description: Submit the one closed, packet-bound no-effect decision.
      if: needs.agent.result == 'success'
      runs-on: ubuntu-latest
      permissions:
        contents: read
        actions: read
      inputs:
        decision:
          description: The complete decision as a JSON string, with every packet field and outcome wait.
          required: true
          type: string
      steps:
        - uses: actions/checkout@v7.0.1
          with:
            ref: ${{ github.workflow_sha }}
            persist-credentials: false
            sparse-checkout: .github/workflows/ci-shepherd
            sparse-checkout-cone-mode: false
        - uses: actions/download-artifact@v8.0.1
          with:
            name: ci-shepherd-prepare-${{ github.run_id }}-${{ github.run_attempt }}
            path: artifacts/ci-shepherd/trusted
        - uses: actions/download-artifact@v8.0.1
          with:
            name: ci-shepherd-evidence-${{ github.run_id }}-${{ github.run_attempt }}
            path: artifacts/ci-shepherd/evidence
        - name: Guarded no-effect apply
          run: |
            python3 .github/workflows/ci-shepherd/round.py apply \
              --trusted artifacts/ci-shepherd/trusted \
              --evidence artifacts/ci-shepherd/evidence/evidence.json \
              --decision "$GH_AW_AGENT_OUTPUT" \
              --receipt artifacts/ci-shepherd/receipt.json
        - uses: actions/upload-artifact@v7.0.1
          if: always()
          with:
            name: ci-shepherd-receipt-${{ github.run_id }}-${{ github.run_attempt }}
            path: |
              artifacts/ci-shepherd/receipt.json
              artifacts/ci-shepherd/failure.json
            if-no-files-found: error
            retention-days: 2
---

# CI Shepherd transport proof

This packet is your only task input:

```json
${{ needs.prepare.outputs.packet }}
```

Copy every packet field exactly into one closed JSON object and add only
`"outcome": "wait"`. Call `submit_decision` exactly once with that object serialized
as the `decision` string. Return the same JSON object as your entire final answer.

Do not inspect GitHub, run shell commands, read other files, delegate, modify
files, or take external actions. There is no repair task or live mode.
