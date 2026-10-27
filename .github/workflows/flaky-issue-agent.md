---
name: Fork Responses Tool Roundtrip
description: Exercise Bash and editing with the CLI-recommended Responses wire setting.
on:
  workflow_dispatch: {}
if: github.repository == 'radical/aspire' && github.ref == 'refs/heads/ankj/flaky-responses-probe-1'
run-name: "Fork Responses tool roundtrip [${{ github.run_id }}]"
concurrency:
  group: flaky-responses-tool-roundtrip
  cancel-in-progress: false
  job-discriminator: ${{ github.run_id }}
engine:
  id: copilot
  model: gpt-6.1-sol
  args: ["--reasoning-effort", "high"]
  env:
    COPILOT_PROVIDER_WIRE_API: responses
timeout-minutes: 5
permissions:
  contents: read
  actions: read
  issues: read
  copilot-requests: write
network:
  allowed: [defaults, github]
pre-agent-steps:
  - name: Prepare harmless tool-roundtrip fixture
    run: |
      python3 - <<'PY'
      from pathlib import Path
      root = Path("/tmp/gh-aw/tool-roundtrip")
      root.mkdir(parents=True, exist_ok=False)
      (root / "fixture.txt").write_bytes(b"wire_api=responses\nroundtrip=pending\n")
      print("Prepared fixture: wire_api=responses; roundtrip=pending")
      PY
post-steps:
  - name: Assert actual tool-roundtrip result
    if: always()
    env:
      ENGINE_OUTCOME: ${{ steps.agentic_execution.outcome }}
    run: |
      python3 - <<'PY'
      import json
      import os
      from pathlib import Path
      root = Path("/tmp/gh-aw/tool-roundtrip")
      root.mkdir(parents=True, exist_ok=True)
      fixture = root / "fixture.txt"
      actual = fixture.read_bytes() if fixture.is_file() else None
      expected = b"wire_api=responses\nroundtrip=verified\n"
      passed = actual == expected and os.environ["ENGINE_OUTCOME"] == "success"
      (root / "verification.json").write_text(json.dumps({
          "passed": passed,
          "engine_outcome": os.environ["ENGINE_OUTCOME"],
          "expected": expected.decode(),
          "actual": actual.decode(errors="replace") if actual is not None else None,
      }, indent=2) + "\n")
      print("TOOL_ROUNDTRIP_PASS" if passed else "TOOL_ROUNDTRIP_FAILED")
      raise SystemExit(0 if passed else 1)
      PY
  - name: Retain actual fixture and trusted assertion
    if: always()
    uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
    with:
      name: responses-tool-roundtrip
      path: |
        /tmp/gh-aw/tool-roundtrip/fixture.txt
        /tmp/gh-aw/tool-roundtrip/verification.json
      if-no-files-found: error
safe-outputs:
  report-failure-as-issue: false
  # This fixture-only probe must not launch a second model to inspect safe outputs.
  threat-detection: false
  # A non-issue output suppresses the pinned compiler's default create-issue.
  upload-artifact:
    max-uploads: 1
    allowed-paths: [/tmp/gh-aw/tool-roundtrip]
  missing-tool:
    create-issue: false
  missing-data:
    create-issue: false
  report-incomplete:
    create-issue: false
  noop:
    report-as-issue: false
tools:
  bash: true
  edit:
---

# Perform one real tool roundtrip

This is a harmless tool compatibility smoke check, not an issue investigation.
Do not read repository files, load skills, run .NET, install anything, access
credentials, dispatch workflows, publish, or modify any investigation checkpoint.
Do not select a different model or provider.

Perform these three actions using actual tools, not a narrated description:

1. Use Bash to read `/tmp/gh-aw/tool-roundtrip/fixture.txt` and show its contents.
2. Use the edit/apply_patch tool to change only `roundtrip=pending` to
   `roundtrip=verified` in that file. Do not use Bash to make the edit.
3. Use Bash to verify that the file's complete bytes are exactly
   `b"wire_api=responses\nroundtrip=verified\n"`. Fail the command if they differ.

Leave that file in place for the trusted post-agent assertion. Do not edit
`verification.json`, write other files, or invoke safe outputs. Finish with one
short sentence stating the observed result. A sentence alone is not success:
the host checks the real fixture and the engine step outcome after execution.
