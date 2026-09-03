---
name: ci-shepherd
description: Run the complete evidence-bounded CI shepherd workflow for microsoft/aspire from policy-level inputs. Use for action-free or explicitly authorized live shepherd operations, not implementation review.
---

# CI Shepherd entrypoint

Read `.ci-shepherd-build/SKILL.md` completely, then execute that workflow as
one operator. This registered entrypoint is the public skill boundary; the
referenced file is the canonical operating contract and implementation guide.

Accept policy-level inputs such as repository, action-free or live mode,
allowed mutation classes, and budgets. Do not ask the caller to orchestrate
individual scripts or copy a command-by-command recipe into the kickoff
prompt. The skill owns collection, fresh assessment, deterministic selection,
authorization, execution, verification, retrospective review, and final
reporting.

For an action-free run, do not mint a grant and do not execute any mutation.
For a live run, execute only effects selected and authorized through the
canonical workflow. If any required evidence, boundary, or validation is
unavailable, fail closed and report the blocker.
