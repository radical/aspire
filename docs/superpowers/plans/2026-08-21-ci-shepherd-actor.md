# CI Shepherd Actor Implementation Plan

> **Status:** The exact-action actor and its lifecycle preflight are implemented
> through commit `443e989126`. Current validation and residual constraints are
> recorded in
> [CI Shepherd Implementation Status](../status/2026-08-28-ci-shepherd-implementation.md).
> Live action trials remain intentionally pending in
> [CI Shepherd Continuation Plan](2026-08-28-ci-shepherd-continuation.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dry-run-by-default actor that prints validated CI shepherd proposals and executes exactly one explicit action only with `--execute --action-id`.

**Architecture:** Keep proposal generation and action execution separate. A pure actor module validates and selects proposals, renders stable dry-run output, enforces dependencies and preflight, and records results; a focused GitHub adapter exposes only the issue/comment operations the actor supports; a small CLI wires files and mode flags together.

**Tech Stack:** Python 3 standard library, GitHub CLI REST API, existing `ci_shepherd` JSON helpers, `unittest`.

---

## File Map

- Create: `.ci-shepherd-build/scripts/ci_shepherd/actor.py`
  - Validate proposal/result documents.
  - Select one or all proposals for dry-run.
  - Execute one action through a typed client protocol.
  - Enforce dependencies, preflight, idempotency, and result recording.
- Create: `.ci-shepherd-build/scripts/ci_shepherd/github_actor.py`
  - Run only the fixed GitHub GET/POST/PATCH operations needed by the actor.
  - Reject arbitrary repositories, methods, and endpoints from proposal data.
- Create: `.ci-shepherd-build/scripts/execute_actions.py`
  - Parse `--proposals`, `--results`, optional `--action-id`, and `--execute`.
  - Default to network-free stable JSON output.
- Create: `.ci-shepherd-build/tests/test_actor.py`
  - Unit-test dry-run, proposal validation, selection, dependencies, preflight,
    supported mutations, reconciliation, and duplicate attempts.
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
  - Test the CLI mode boundary and owner-only result files.
- Modify: `.ci-shepherd-build/SKILL.md`
  - Document the actor command and artifact authority boundary.

### Task 1: Validate and render proposals without side effects

**Files:**
- Create: `.ci-shepherd-build/tests/test_actor.py`
- Create: `.ci-shepherd-build/scripts/ci_shepherd/actor.py`

- [ ] **Step 1: Write failing dry-run tests**

Create fixtures for schema-version-1 proposals and add:

```python
def test_dry_run_renders_all_actions_without_client_calls(self) -> None:
    client = ScriptedActorClient()

    rendered = build_dry_run(PROPOSALS, action_id=None)

    self.assertEqual("dry-run", rendered["mode"])
    self.assertEqual(
        ["create-comment", "close-issue"],
        [action["operation"] for action in rendered["actions"]],
    )
    self.assertEqual([], client.calls)


def test_dry_run_can_select_one_action(self) -> None:
    rendered = build_dry_run(PROPOSALS, action_id=COMMENT_ACTION_ID)

    self.assertEqual([COMMENT_ACTION_ID], [
        action["actionId"] for action in rendered["actions"]
    ])
```

Add validation tests for duplicate action IDs, unknown operations, malformed
comment bodies, missing edit comment IDs, invalid close reasons, and missing
action IDs.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor.ActorTests.test_dry_run_renders_all_actions_without_client_calls
```

Expected: import failure because `ci_shepherd.actor` does not exist.

- [ ] **Step 3: Implement proposal validation and stable dry-run output**

Define:

```python
KNOWN_OPERATIONS = frozenset({"create-comment", "edit-comment", "close-issue"})
KNOWN_CLOSE_REASONS = frozenset({"completed", "not_planned", "duplicate"})

def validate_action_proposals(document: object) -> dict[str, object]: ...
def select_action(document: dict[str, object], action_id: str) -> dict[str, object]: ...
def build_dry_run(
    document: object,
    *,
    action_id: str | None,
) -> dict[str, object]: ...
```

The rendered action copies only:

```python
{
    "actionId": proposal["actionId"],
    "issueNumber": proposal["issueNumber"],
    "issueUrl": proposal["issueUrl"],
    "operation": proposal["operation"],
    "body": proposal.get("body"),
    "closeReason": proposal.get("closeReason"),
    "evidenceIds": proposal["evidenceIds"],
    "dependsOn": proposal.get("dependsOn"),
    "expectedIssueState": proposal["expectedIssueState"],
    "wouldExecute": True,
}
```

- [ ] **Step 4: Run the actor tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor
```

Expected: all validation and dry-run tests pass.

### Task 2: Add the constrained GitHub action adapter

**Files:**
- Create: `.ci-shepherd-build/tests/test_actor.py`
- Create: `.ci-shepherd-build/scripts/ci_shepherd/github_actor.py`

- [ ] **Step 1: Write failing adapter command tests**

Use a recording subprocess runner and assert exact commands:

```python
client.get_issue("owner/repo", 21)
# gh api --method GET ... repos/owner/repo/issues/21

client.create_comment("owner/repo", 21, "[automated] body")
# gh api --method POST ... repos/owner/repo/issues/21/comments --input <private-file>

client.edit_comment("owner/repo", 900, "[automated] body")
# gh api --method PATCH ... repos/owner/repo/issues/comments/900 --input <private-file>

client.close_issue("owner/repo", 21, "completed")
# gh api --method PATCH ... repos/owner/repo/issues/21 --input <private-file>
```

Assert payload files are mode `0600`, contain the expected JSON, and are
removed after the call.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor.GitHubActorClientTests
```

Expected: import failure because `ci_shepherd.github_actor` does not exist.

- [ ] **Step 3: Implement fixed API operations**

Define:

```python
class GitHubActorClient:
    def get_issue(self, repository: str, issue_number: int) -> dict[str, object]: ...
    def get_comment(self, repository: str, comment_id: int) -> dict[str, object]: ...
    def list_comments(self, repository: str, issue_number: int) -> list[dict[str, object]]: ...
    def create_comment(self, repository: str, issue_number: int, body: str) -> dict[str, object]: ...
    def edit_comment(self, repository: str, comment_id: int, body: str) -> dict[str, object]: ...
    def close_issue(self, repository: str, issue_number: int, reason: str) -> dict[str, object]: ...
```

Build endpoints exclusively from validated repository, issue number, and
comment ID. Use private temporary JSON files and `gh api --input`; never pass
comment text through shell interpolation.

- [ ] **Step 4: Run adapter tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor.GitHubActorClientTests
```

Expected: all adapter tests pass.

### Task 3: Execute one action with preflight and reconciliation

**Files:**
- Modify: `.ci-shepherd-build/tests/test_actor.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/actor.py`

- [ ] **Step 1: Write failing execution tests**

Add a scripted client with queued issue/comment responses and tests proving:

```python
def test_execute_requires_completed_dependency(self) -> None: ...
def test_create_comment_aborts_when_marker_exists(self) -> None: ...
def test_edit_comment_requires_owned_marker(self) -> None: ...
def test_close_issue_aborts_when_issue_is_not_open(self) -> None: ...
def test_create_comment_executes_and_verifies_body(self) -> None: ...
def test_edit_comment_executes_and_verifies_body(self) -> None: ...
def test_close_issue_executes_and_verifies_reason(self) -> None: ...
def test_completed_action_id_is_not_attempted_twice(self) -> None: ...
```

For a successful close, assert:

```python
self.assertEqual("executed", result["outcome"])
self.assertEqual("closed", result["result"]["issueState"])
self.assertEqual("completed", result["result"]["stateReason"])
self.assertEqual(
    [("get_issue", 21), ("close_issue", 21, "completed"), ("get_issue", 21)],
    client.calls,
)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor.ActorTests.test_close_issue_executes_and_verifies_reason
```

Expected: failure because `execute_action` does not exist.

- [ ] **Step 3: Implement the actor protocol**

Define:

```python
class ActorClient(Protocol):
    def get_issue(...): ...
    def get_comment(...): ...
    def list_comments(...): ...
    def create_comment(...): ...
    def edit_comment(...): ...
    def close_issue(...): ...

def execute_action(
    document: object,
    *,
    action_id: str,
    prior_results: object,
    client: ActorClient,
    now: Callable[[], datetime],
) -> dict[str, object]: ...
```

Return one terminal result with:

```python
{
    "actionId": action_id,
    "attemptedAt": now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
    "outcome": "executed" | "stale" | "failed",
    "preflight": {...},
    "result": {...},
}
```

Use proposal fields verbatim. Do not re-render bodies or reinterpret evidence.

- [ ] **Step 4: Run actor tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v test_actor
```

Expected: all actor and adapter tests pass.

### Task 4: Add the dry-run-first CLI and durable results

**Files:**
- Create: `.ci-shepherd-build/scripts/execute_actions.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing CLI tests**

Add:

```python
def test_execute_actions_defaults_to_dry_run_without_github_access(self) -> None: ...
def test_execute_actions_rejects_execute_without_action_id(self) -> None: ...
def test_execute_actions_appends_owner_only_execution_result(self) -> None: ...
```

The first test patches `GitHubActorClient` and asserts it is never constructed.
The second expects `SystemExit(2)`. The third verifies mode `0600` and one
terminal result.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v \
  test_scripts.PrototypeScriptTests.test_execute_actions_defaults_to_dry_run_without_github_access
```

Expected: failure because `execute_actions.py` does not exist.

- [ ] **Step 3: Implement CLI mode handling**

Parse:

```python
parser.add_argument("--proposals", type=Path, required=True)
parser.add_argument("--results", type=Path, required=True)
parser.add_argument("--action-id")
parser.add_argument("--execute", action="store_true")
```

If `--execute` is false, print `stable_json(build_dry_run(...))` and return
without constructing a client. If true, require `--action-id`, load or create
the result document, execute one action, atomically write mode `0600`, and print
the terminal result.

- [ ] **Step 4: Run CLI and full prototype tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_*.py'
```

Expected: all tests pass.

### Task 5: Document and prove the live artifact contract

**Files:**
- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write a failing prompt-contract test**

Assert the skill contains:

```python
self.assertIn("The actor is dry-run by default.", normalized_skill)
self.assertIn("`--execute` requires one exact `--action-id`.", normalized_skill)
self.assertIn("Dry-run performs no GitHub access", normalized_skill)
self.assertIn("The actor never reinterprets `judgments.json`", normalized_skill)
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest -v \
  test_scripts.PrototypeScriptTests.test_skill_documents_dry_run_actor_contract
```

Expected: FAIL because the actor contract is not documented.

- [ ] **Step 3: Document actor usage**

Add the exact dry-run and execute commands from the actor design. State that
`judgments.json` feeds only proposal generation; the actor reads
`action-proposals.json`, performs no semantic assessment, and records terminal
effects in `action-results.json`.

- [ ] **Step 4: Run the full suite and a real dry-run**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
python3 -m unittest discover -s .ci-shepherd-build/tests -p 'test_*.py'

PYTHONPATH=.ci-shepherd-build/scripts \
python3 .ci-shepherd-build/scripts/execute_actions.py \
  --proposals /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-regression-19149-rerun/action-proposals.json \
  --results /Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-regression-19149-rerun/action-results.json
```

Expected: tests pass; the command prints both proposals with
`mode: "dry-run"` and does not create or modify `action-results.json`.
