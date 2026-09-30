---
applyTo: "tests/Infrastructure.Tests/**"
---

# Infrastructure.Tests authoring and review guidance

## Test durable behavior

- Test observable behavior and durable contracts rather than source formatting or an implementation restatement. Each assertion should fail for the exact regression the test protects.
- When a brittle source-pinning test breaks, replace it with a semantic or behavior test, or delete it if it protects no durable contract. Do not mechanically update expected source text.
- Do not pin comments, whitespace, display names, full step inventories, incidental shell formatting, implementation prose, or the absence of renamed symbols.
- Exact source strings are appropriate only when the literal is itself an external or security contract, such as an immutable action SHA, public input or output, artifact or provider identifier, approved URL, or externally parsed token or command. Name the invariant in the test so the contract is clear.

## Workflows and YAML

- Parse workflow YAML and assert the semantic graph, data flow, and security boundaries: triggers, permissions, `needs`, reusable-workflow inputs and outputs, conditions, artifact flow, environments, gates, and ordering only when ordering changes execution.
- Use actionlint for workflow syntax, expression, and schema validation. It complements rather than replaces tests for repository-specific dependencies and behavior.

## Scripts and actions

- Execute the implementation with controlled fixtures when feasible. Reuse `NodeCommand`, `PowerShellCommand`, temporary workspaces or Git repositories, fake executables injected through `PATH`, and generated fixtures instead of reading source text.
- Assert inputs, outputs, exit codes, side effects, generated files, and external-tool invocations that callers observe.

## Review and validation

- Require coverage whose assertion would fail if the reported regression returned. Visual, stylistic, and implementation-restatement assertions are not useful coverage.
- Run focused tests with Microsoft.Testing.Platform filters after `--`, and exclude quarantined and outerloop tests with `--filter-not-trait "quarantined=true"` and `--filter-not-trait "outerloop=true"`.
