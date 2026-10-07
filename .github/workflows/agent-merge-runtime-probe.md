---
description: One-shot read-only probe for Agent Merge controller availability in hosted gh-aw.
on:
  workflow_dispatch:

engine:
  id: copilot
  args:
    - --deny-tool
    - safeoutputs

permissions:
  contents: read
  pull-requests: read
  copilot-requests: write

network:
  allowed:
    - defaults
    - github

tools:
  bash: false
  cli-proxy: false
  edit: false
  github:
    toolsets: [pull_requests]
    allowed: [pull_request_read]
    lockdown: false
---

# Hosted Agent Merge capability probe

Perform one read-only capability probe in this hosted GitHub Agentic Workflow
run. Use only tools that are actually registered and callable in this runtime.

1. Use the standard GitHub pull-request read tool to inspect
   `radical/aspire#129`. Report whether it is open and draft, its head SHA, the
   `NO-MERGE` label, and whether an auto-merge request is present.
2. Determine whether this runtime exposes a registered tool named exactly
   `manage_agent_merge`. Do not infer availability or absence from this prompt,
   repository files, skill text, or prior chat. If the runtime provides a tool
   inventory, report the exact registered tool name and server. If the named
   controller tool is available, inspect only a read-only status surface and
   report its schema; do not call any action that changes controller or PR
   state. If there is no inventory or no such tool, state that limitation
   plainly and do not invent tool names or schemas.
3. Report the exact tool names you actually invoked and the read-only evidence
   returned. Distinguish a controller tool being absent from a failed or
   incomplete run.

This is a capability probe only. Do not change files, run shell commands, call
write tools, create safe outputs, push commits, add comments, alter Agent Merge
settings, change PR state, enable auto-merge, enqueue, or merge anything. Do not
use undocumented endpoints, private APIs, bridges, or custom MCP servers.
