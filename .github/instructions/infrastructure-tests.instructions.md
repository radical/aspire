---
applyTo: "tests/Infrastructure.Tests/**"
---

# Infrastructure.Tests authoring and review guidance

## Core rule

Do not assert YAML source text. Assert durable workflow semantics from parsed YAML. Extract non-trivial imperative logic into executable files and test its behavior, while retaining focused YAML tests for the wiring contract.

Each assertion should fail for the exact regression it protects. When a brittle source-pinning test breaks, convert it to a semantic or behavior test, or delete it if it protects no durable contract.

## YAML contracts

- Test declarative workflow behavior from parsed YAML: triggers, permissions, `needs`, reusable-workflow inputs and outputs, conditions, artifact flow, environments, gates, and ordering only when order affects execution.
- Test immutable action pins and security-sensitive literals from their parsed YAML nodes when the literal is the contract. Name the invariant so the reason for exactness is clear.
- Do not assert formatting, comments, display names, full step inventories, incidental command strings or quoting, implementation prose, or negative strings for renamed symbols.

## Imperative logic

- Extract inline JavaScript, PowerShell, or shell when it contains meaningful branches, loops, parsing or transformation, retry or error handling, security-sensitive decisions, or reusable logic.
- Keep trivial orchestration commands inline when locality is clearer. Extra files and quoting or path boundaries can make simple wiring harder to maintain.
- After extraction, test both layers:
  - Execute the script with controlled fixtures and assert exit codes, outputs, side effects, generated files, and external-tool invocations. Reuse `NodeCommand`, `PowerShellCommand`, temporary workspaces or Git repositories, fake executables through `PATH`, and generated fixtures.
  - Retain a focused parsed-YAML test proving that the workflow invokes the correct file with its required inputs, environment, permissions, dependencies, and condition.

## Verification hierarchy

1. Use actionlint for workflow syntax, expression, and schema validation.
2. Use parsed YAML tests for orchestration and security contracts.
3. Use executable behavior tests for imperative logic.
4. Use live or dry-run validation only for integration behavior owned by the platform.
