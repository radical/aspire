# CI Shepherd Watch Comment POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the first live CI shepherd action by posting one approved watch-status comment from a fresh report and showing that an unchanged subsequent run proposes no duplicate comment or edit.

**Architecture:** Extend the read-only collector so a marked status comment authored by the configured shepherd identity remains available for idempotency but contributes no markers, facts, or references. Add a small deterministic watch-proposal renderer that consumes the raw snapshot, prepared assessment, and finalized judgments. Run the existing two-stage assessment live, show one exact comment to the user, post only after approval, then recollect and prove the renderer reports it unchanged.

**Tech Stack:** Python 3 standard library, existing `ci_shepherd` modules, `unittest`, GitHub CLI, JSON artifacts.

---

## Scope and Persistence

This plan implements only Slice 1 from
`docs/superpowers/specs/2026-08-21-ci-shepherd-full-cycle-poc-design.md`.
Investigation assignment, duplicate closure, and quarantine or fix pull requests
remain separate plans.

`.ci-shepherd-build/` is intentionally excluded by the checkout's
`.git/info/exclude`. Do not force-add the prototype during this slice. Use the
test suite and session artifacts as checkpoints. The design and plan documents
remain reviewable repository files; commit them only after the user reviews the
draft commit message.

## File Map

- Modify: `.ci-shepherd-build/scripts/ci_shepherd/collector.py`
    - Recognize owned shepherd status comments.
    - Preserve identity metadata while excluding their content from evidence
    extraction in the full-collection path used by this POC.
- Modify: `.ci-shepherd-build/scripts/collect.py`
    - Accept and forward the authenticated shepherd login.
- Create: `.ci-shepherd-build/scripts/ci_shepherd/actions.py`
    - Render deterministic watch comments.
    - Compare them with an existing owned canonical comment.
    - Produce create, edit, or unchanged results without performing GitHub writes.
- Create: `.ci-shepherd-build/scripts/propose_actions.py`
    - Validate CLI inputs and write owner-only `action-proposals.json`.
- Modify: `.ci-shepherd-build/SKILL.md`
    - Document the status-comment, attribution, approval, preflight, and
    no-self-evidence contracts.
- Modify: `.ci-shepherd-build/tests/test_collector.py`
    - Cover full and incremental self-comment filtering.
- Create: `.ci-shepherd-build/tests/test_actions.py`
    - Cover watch comment rendering and idempotency.
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
    - Cover CLI forwarding, owner-only proposal output, and prompt contract.
- Create in session artifacts:
    - `ci-shepherd-live-cycle-1/`
    - Store the live collection, expanded assessment, judgments, report,
    proposals, posted body, result, and second-run artifacts.

### Task 1: Exclude owned shepherd comments from evidence extraction

**Files:**
- Modify: `.ci-shepherd-build/tests/test_collector.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/collector.py`

- [ ] **Step 1: Write the full-collection regression test**

Add a test beside
`test_direct_comment_reference_preserves_comment_provenance_for_root_association`:

```python
def test_owned_shepherd_status_comment_is_retained_without_becoming_evidence(self) -> None:
    comment_url = f"https://github.com/{REPOSITORY}/issues/21#issuecomment-900"
    pages = {
        f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
            make_issue(21, labels=["ci-failure-cause"])
        ],
        f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
        f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
            make_issue(
                401,
                state="closed",
                closed_at="2026-08-01T00:00:00Z",
                labels=["ci-failure-cause"],
            )
        ],
        f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
        f"/repos/{REPOSITORY}/issues/21/comments": [
            {
                "id": 900,
                "html_url": comment_url,
                "created_at": "2026-08-21T12:00:00Z",
                "updated_at": "2026-08-21T12:00:00Z",
                "user": {"login": "ankj"},
                "body": (
                    "[automated] Watching #401 and "
                    "https://github.com/owner/repo/actions/runs/777.\n\n"
                    "<!-- ci-shepherd:role=status -->\n"
                    "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                ),
            }
        ],
        f"/repos/{REPOSITORY}/issues/401/comments": [],
    }

    result = Collector(
        ScriptedClient(pages=pages),
        REPOSITORY,
        NOW,
        shepherd_author="ankj",
    ).collect(include_timeline=False)

    payload = result.evidence["issue:21:comment:900"]["payload"]
    self.assertEqual(
        {
            "role": "status",
            "idempotencyKey": "issue:21:watch",
            "owned": True,
        },
        payload["shepherdStatus"],
    )
    self.assertEqual([], payload["markers"])
    self.assertEqual([], payload["facts"])
    self.assertEqual([], payload["references"])
    self.assertNotIn("issue:401", result.evidence)
    self.assertNotIn(21, result.references)
```

- [ ] **Step 2: Add the spoofing regression**

In the same test, or as a second focused test, change the comment author to
`someone-else` and assert the existing reference behavior remains:

```python
self.assertNotIn("shepherdStatus", payload)
self.assertIn("issue:401", result.evidence)
self.assertEqual(
    "issue:21:comment:900",
    result.evidence["issue:401"]["payload"]["referencedBy"][0]["sourceEvidenceId"],
)
```

- [ ] **Step 3: Run the new tests and verify they fail**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -v \
  test_collector.CollectorTests.test_owned_shepherd_status_comment_is_retained_without_becoming_evidence \
  test_collector.CollectorTests.test_unowned_shepherd_marker_does_not_hide_comment_evidence
```

Expected: both tests fail because `Collector` does not accept
`shepherd_author`.

- [ ] **Step 4: Add constructor state and marker helpers**

Add `shepherd_author: str | None = None` to the existing
`Collector.__init__` signature without replacing the rest of its body:

```python
        shepherd_author: str | None = None,
```

After the existing `self._bot_authors = bot_authors` assignment, add only:

```python
self._shepherd_author = (
    shepherd_author.casefold()
    if isinstance(shepherd_author, str) and shepherd_author.strip()
    else None
)
```

Add focused helpers near `_extract_markers`:

```python
def _owned_shepherd_status(
    self,
    comment: dict[str, Any],
    markers: list[dict[str, Any]],
) -> dict[str, object] | None:
    if (
        self._shepherd_author is None
        or str(comment.get("author", "")).casefold() != self._shepherd_author
        or not str(comment.get("body", "")).startswith("[automated] ")
    ):
        return None

    marker_values = {
        str(marker.get("key")): str(marker.get("normalized"))
        for marker in markers
    }
    if marker_values.get("role") != "status":
        return None
    idempotency_key = marker_values.get("idempotency-key")
    if not idempotency_key:
        return None
    return {
        "role": "status",
        "idempotencyKey": idempotency_key,
        "owned": True,
    }
```

Add one comment payload helper so full and incremental collection cannot drift:

```python
def _extract_comment_payload(
    self,
    issue_number: int,
    comment: dict[str, Any],
    evidence_id: str,
) -> tuple[
    dict[str, object] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    markers = self._extract_markers(comment["body"], evidence_id)
    shepherd_status = self._owned_shepherd_status(comment, markers)
    if shepherd_status is not None:
        return shepherd_status, [], [], []
    return (
        None,
        markers,
        self._extract_facts(comment["body"], evidence_id),
        self._extract_references(
            issue_number,
            comment["body"],
            evidence_id,
            comment["url"],
        ),
    )
```

- [ ] **Step 5: Use the helper in the full-collection path**

In `_load_issue_detail`, replace the separate marker, fact, and reference calls
with `_extract_comment_payload`. Extend its four-item comment tuple with
`shepherd_status`, and include this payload fragment only when present:

```python
{
    **comment,
    "sourceIssueNumber": issue_number,
    "markers": markers,
    "facts": facts,
    "references": references_by_source.get(evidence_id, []),
    **(
        {"shepherdStatus": shepherd_status}
        if shepherd_status is not None
        else {}
    ),
}
```

Update the `comment_evidence_payloads` annotation, append call, and evidence
loop for the five-item tuple. `_issue_lifecycle_metadata` reads only the first
three items with `_, comment, markers, *_`, so it continues to receive the
expected `comment` and `markers` positions.

Update the marker and fact aggregation comprehensions so they do not retain the
old four-item tuple assumption:

```python
all_markers = _sorted_unique_records(
    markers + [marker for entry in comment_evidence_payloads for marker in entry[2]],
    ("key", "normalized", "method", "sourceEvidenceId"),
)
all_facts = _sorted_unique_records(
    facts + [fact for entry in comment_evidence_payloads for fact in entry[3]],
    ("field", "normalized", "method", "sourceEvidenceId"),
)
```

- [ ] **Step 6: Run the focused collector tests**

Run the Step 3 command again.

Expected: both tests pass.

- [ ] **Step 7: Run the complete collector suite**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_collector
```

Expected: all collector tests pass.

### Task 2: Render deterministic watch-comment proposals

**Files:**
- Create: `.ci-shepherd-build/tests/test_actions.py`
- Create: `.ci-shepherd-build/scripts/ci_shepherd/actions.py`

- [ ] **Step 1: Write the new-comment test**

Create `test_actions.py` with a prepared watch judgment and raw snapshot:

```python
from __future__ import annotations

import unittest

from ci_shepherd.actions import build_watch_proposals


class WatchActionTests(unittest.TestCase):
    def test_build_watch_proposals_renders_new_status_comment(self) -> None:
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-21T16:00:00Z",
            "openIssues": [21],
            "issues": [{"number": 21, "state": "open"}],
            "supportingIssues": [],
            "evidence": {
                "issue:21": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/21",
                    "availability": "available",
                    "payload": {"number": 21, "state": "open"},
                },
                "run:777": {
                    "kind": "workflow-run",
                    "url": "https://github.com/owner/repo/actions/runs/777",
                    "availability": "available",
                    "payload": {"id": 777},
                },
            },
            "collectionErrors": [],
            "warnings": [],
            "references": {},
        }
        prepared = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "sourceCollectedAt": "2026-08-21T16:00:00Z",
            "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
            "issues": [
                {
                    "issueNumber": 21,
                    "issueUrl": "https://github.com/owner/repo/issues/21",
                    "title": "One transient failure",
                    "evidenceBundle": [
                        {"id": "issue:21", "kind": "issue-event"},
                        {"id": "run:777", "kind": "workflow-run"},
                    ],
                }
            ],
        }
        judgments = {
            "schemaVersion": 1,
            "snapshotId": prepared["snapshotId"],
            "issues": [
                {
                    "issueNumber": 21,
                    "category": "transient-infrastructure",
                    "recommendations": [
                        {
                            "disposition": "watch",
                            "target": {"kind": "workflow-run", "value": "777"},
                            "confidence": "medium",
                            "summary": "One matching failure has been observed.",
                            "evidenceIds": ["issue:21", "run:777"],
                            "missingEvidence": ["another independent occurrence"],
                            "reassessWhen": (
                                "After another independent matching failure or "
                                "a covered successful execution."
                            ),
                        }
                    ],
                }
            ],
        }

        result = build_watch_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual([], result["unchangedIssueNumbers"])
        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("create-comment", proposal["operation"])
        self.assertEqual("issue:21:watch", proposal["idempotencyKey"])
        self.assertTrue(proposal["body"].startswith("[automated] "))
        self.assertIn("One matching failure has been observed.", proposal["body"])
        self.assertIn(
            "After another independent matching failure or a covered successful execution.",
            proposal["body"],
        )
        self.assertIn(
            "https://github.com/owner/repo/actions/runs/777",
            proposal["body"],
        )
        self.assertIn("<!-- ci-shepherd:role=status -->", proposal["body"])
        self.assertIn(
            "<!-- ci-shepherd:idempotency-key=issue:21:watch -->",
            proposal["body"],
        )
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -v \
  test_actions.WatchActionTests.test_build_watch_proposals_renders_new_status_comment
```

Expected: fail because `ci_shepherd.actions` does not exist.

- [ ] **Step 3: Implement the pure proposal renderer**

Create `actions.py` with:

```python
from __future__ import annotations

from typing import Any

from ci_shepherd.poc import validate_poc_judgments


def _status_markers(issue_number: int, disposition: str) -> str:
    return (
        "<!-- ci-shepherd:role=status -->\n"
        f"<!-- ci-shepherd:idempotency-key=issue:{issue_number}:{disposition} -->"
    )


def _evidence_lines(
    snapshot: dict[str, object],
    evidence_ids: list[str],
) -> list[str]:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    lines: list[str] = []
    for evidence_id in evidence_ids:
        record = evidence.get(evidence_id)
        url = record.get("url") if isinstance(record, dict) else None
        lines.append(
            f"- [{evidence_id}]({url})"
            if isinstance(url, str) and url
            else f"- `{evidence_id}`"
        )
    return lines


def _render_watch_body(
    issue_number: int,
    recommendation: dict[str, Any],
    snapshot: dict[str, object],
) -> str:
    missing = recommendation.get("missingEvidence", [])
    reassess_when = str(recommendation.get("reassessWhen", "")).strip()
    if not reassess_when:
        raise ValueError(
            f"Watch recommendation for issue {issue_number} must name reassessWhen."
        )
    missing_lines = (
        [f"- {value}" for value in missing]
        if missing
        else ["- No additional evidence is currently fetchable."]
    )
    return "\n".join(
        [
            "[automated] The CI shepherd is watching this failure.",
            "",
            f"**Current assessment:** {recommendation['summary']}",
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, recommendation["evidenceIds"]),
            "",
            "**Evidence still needed:**",
            *missing_lines,
            "",
            f"**Reassess when:** {reassess_when}",
            "",
            "No quarantine, retry, closure, or investigation has been started.",
            "",
            _status_markers(issue_number, "watch"),
        ]
    )


def _owned_status_comments(
    snapshot: dict[str, object],
    issue_number: int,
    idempotency_key: str,
) -> list[dict[str, object]]:
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise TypeError("Validated snapshot evidence must be an object.")
    matches: list[dict[str, object]] = []
    for record in evidence.values():
        if not isinstance(record, dict) or record.get("kind") != "issue-comment":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("sourceIssueNumber") != issue_number:
            continue
        status = payload.get("shepherdStatus")
        if (
            isinstance(status, dict)
            and status.get("owned") is True
            and status.get("idempotencyKey") == idempotency_key
        ):
            matches.append(payload)
    return matches


def build_watch_proposals(
    snapshot: object,
    prepared: object,
    judgments: object,
    shepherd_author: str,
) -> dict[str, object]:
    validate_poc_judgments(prepared, judgments)
    if not isinstance(snapshot, dict):
        raise TypeError("Snapshot must be an object.")
    if not isinstance(prepared, dict) or not isinstance(judgments, dict):
        raise TypeError("Prepared input and judgments must be objects.")
    if not shepherd_author.strip():
        raise ValueError("Shepherd author must be nonempty.")

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared["issues"]
        if isinstance(issue, dict)
    }
    proposals: list[dict[str, object]] = []
    unchanged: list[int] = []
    for issue in judgments["issues"]:
        issue_number = issue["issueNumber"]
        watch_recommendations = [
            recommendation
            for recommendation in issue["recommendations"]
            if recommendation["disposition"] == "watch"
        ]
        if len(watch_recommendations) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple watch recommendations."
            )
        if not watch_recommendations:
            continue
        recommendation = watch_recommendations[0]
        key = f"issue:{issue_number}:watch"
        body = _render_watch_body(issue_number, recommendation, snapshot)
        existing = _owned_status_comments(snapshot, issue_number, key)
        if len(existing) > 1:
            raise ValueError(
                f"Issue {issue_number} has multiple owned watch status comments."
            )
        existing_body = str(existing[0].get("body") or "").strip() if existing else ""
        if existing and existing_body == body.strip():
            unchanged.append(issue_number)
            continue
        proposal = {
            "actionId": f"{prepared['snapshotId']}:issue:{issue_number}:watch-comment",
            "issueNumber": issue_number,
            "issueUrl": prepared_issues[issue_number]["issueUrl"],
            "operation": "edit-comment" if existing else "create-comment",
            "idempotencyKey": key,
            "body": body,
            "evidenceIds": list(recommendation["evidenceIds"]),
            "expectedIssueState": "open",
        }
        if existing:
            proposal["commentId"] = existing[0]["id"]
        proposals.append(proposal)
    proposals.sort(key=lambda item: item["issueNumber"])
    unchanged.sort()
    return {
        "schemaVersion": 1,
        "repository": prepared["repository"],
        "snapshotId": prepared["snapshotId"],
        "shepherdAuthor": shepherd_author,
        "proposals": proposals,
        "unchangedIssueNumbers": unchanged,
    }
```

- [ ] **Step 4: Run the new-comment test**

Run the Step 2 command again.

Expected: pass.

- [ ] **Step 5: Add edit and unchanged tests**

Add helpers that place an owned `issue-comment` evidence record into the
snapshot. Cover:

```python
def test_build_watch_proposals_edits_changed_owned_comment(self) -> None:
    # Existing body differs.
    self.assertEqual("edit-comment", proposal["operation"])
    self.assertEqual(900, proposal["commentId"])


def test_build_watch_proposals_omits_identical_owned_comment(self) -> None:
    self.assertEqual([], result["proposals"])
    self.assertEqual([21], result["unchangedIssueNumbers"])


def test_build_watch_proposals_rejects_multiple_owned_comments(self) -> None:
    with self.assertRaisesRegex(
        ValueError,
        "multiple owned watch status comments",
    ):
        build_watch_proposals(snapshot, prepared, judgments, "ankj")


def test_build_watch_proposals_rejects_multiple_watch_recommendations(self) -> None:
    with self.assertRaisesRegex(ValueError, "multiple watch recommendations"):
        build_watch_proposals(snapshot, prepared, judgments, "ankj")
```

- [ ] **Step 6: Run all action tests**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -q test_actions
```

Expected: all action tests pass.

### Task 3: Add the proposal CLI and prompt contract

**Files:**
- Create: `.ci-shepherd-build/scripts/propose_actions.py`
- Modify: `.ci-shepherd-build/scripts/collect.py`
- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write CLI and prompt tests**

Add tests to `test_scripts.py` that assert:

```python
def test_collect_forwards_shepherd_author(self) -> None:
    # Patch Collector and call collect(..., shepherd_author="ankj").
    self.assertEqual("ankj", calls["shepherd_author"])


def test_propose_actions_cli_writes_owner_only_output(self) -> None:
    # Write snapshot/prepared/judgments fixtures and invoke main().
    self.assertEqual(0o600, output.stat().st_mode & 0o777)
    self.assertEqual("create-comment", document["proposals"][0]["operation"])


def test_skill_documents_watch_action_contract(self) -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    for required in (
        "canonical CI shepherd status comment",
        "Every issue- or pull-request-visible effect requires individual user approval.",
        "All automatically posted GitHub text starts with `[automated] `.",
        "Shepherd-authored status comments must not contribute markers, facts, or references.",
        "An unchanged watch state must not create or edit a comment.",
        "The assessment agent never executes actions.",
    ):
        self.assertIn(required, content)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest -v \
  test_scripts.PrototypeScriptTests.test_collect_forwards_shepherd_author \
  test_scripts.PrototypeScriptTests.test_propose_actions_cli_writes_owner_only_output \
  test_scripts.PrototypeScriptTests.test_skill_documents_watch_action_contract
```

Expected: fail because the CLI and prompt contract do not exist.

- [ ] **Step 3: Add `--shepherd-author` to collection**

Extend `collect()`:

```python
def collect(
    repository: str,
    output_dir: Path,
    checkout: Path | None,
    *,
    max_run_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_run_refs_per_issue"],
    max_issue_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_issue_refs_per_issue"],
    max_commit_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_commit_refs_per_issue"],
    state_dir: Path | None = None,
    full_refresh: bool = False,
    shepherd_author: str | None = None,
) -> Path:
```

Pass `shepherd_author=shepherd_author` to `Collector`. Add:

```python
parser.add_argument("--shepherd-author")
```

and forward `args.shepherd_author`.

- [ ] **Step 4: Implement `propose_actions.py`**

Create:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.actions import build_watch_proposals
from ci_shepherd.models import stable_json, validate_snapshot


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render review-only CI shepherd action proposals."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--shepherd-author", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        snapshot = _load(args.snapshot)
        validate_snapshot(snapshot)
        proposals = build_watch_proposals(
            snapshot,
            _load(args.prepared),
            _load(args.judgments),
            args.shepherd_author,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.write_text(stable_json(proposals), encoding="utf-8")
        args.output.chmod(0o600)
    finally:
        os.umask(old_umask)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Update the prompt**

Add an `Issue communication and action boundary` section to
`.ci-shepherd-build/SKILL.md` containing the exact contract asserted in Step 1.
Keep the existing top-level statement that the assessment agent is read-only.
State that only the coordinator renders proposals and that a proposal is not
authorization to post.

- [ ] **Step 6: Run the focused tests**

Run the Step 2 command again.

Expected: all three tests pass.

- [ ] **Step 7: Run the complete local POC suite**

Run:

```bash
PYTHONPATH="$PWD/.ci-shepherd-build/scripts:$PWD/.ci-shepherd-build/tests" \
  python3 -m unittest discover -s .ci-shepherd-build/tests -q
```

Expected: all tests pass.

### Task 4: Produce the first fresh live report

**Files:**
- Create: session artifact directory `ci-shepherd-live-cycle-1/`

- [ ] **Step 1: Resolve the authenticated GitHub identity**

Run:

```bash
gh api user --jq .login
```

Expected: one nonempty GitHub login. Record it as `SHEPHERD_AUTHOR`.

- [ ] **Step 2: Create a private fresh run directory**

Use:

```bash
SCRATCH="/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-live-cycle-1"
mkdir -p "$SCRATCH"
chmod 700 "$SCRATCH"
```

Do not reuse Trial 7 artifacts.

- [ ] **Step 3: Run a full live collection**

Run:

```bash
python3 "$PWD/.ci-shepherd-build/scripts/collect.py" \
  --repository microsoft/aspire \
  --checkout "$PWD" \
  --output-dir "$SCRATCH" \
  --shepherd-author "$SHEPHERD_AUTHOR" \
  --max-run-refs-per-issue 12 \
  --max-issue-refs-per-issue 5 \
  --max-commit-refs-per-issue 3
```

Expected: `input.json`, `collection-errors.json`, `progress.json`, and
`api-calls.jsonl` exist; no GitHub write occurs.

- [ ] **Step 4: Prepare baseline input and fingerprint history**

Run:

```bash
python3 "$PWD/.ci-shepherd-build/scripts/prepare.py" \
  --input "$SCRATCH/input.json" \
  --output "$SCRATCH/assessment-input.json" \
  --max-bundle-records 25

python3 "$PWD/.ci-shepherd-build/scripts/fingerprints.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --output "$SCRATCH/fingerprints.jsonl"

python3 "$PWD/.ci-shepherd-build/scripts/compact.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --fingerprints "$SCRATCH/fingerprints.jsonl" \
  --output "$SCRATCH/agent-input.json"
```

Expected: all three commands exit successfully.

- [ ] **Step 5: Run one bounded evidence-planning agent**

Give a fresh agent only:

- `.ci-shepherd-build/SKILL.md`;
- `$SCRATCH/agent-input.json`; and
- permission to write only
  `$SCRATCH/evidence-requests.round-1.json`.

The agent must use the exact request schema, emit at most 25 GET-only requests,
and make no judgments.

- [ ] **Step 6: Validate and expand**

Run:

```bash
python3 "$PWD/.ci-shepherd-build/scripts/validate_requests.py" \
  --input "$SCRATCH/input.json" \
  --requests "$SCRATCH/evidence-requests.round-1.json"

REQUEST_COUNT="$(python3 -c \
  'import json,sys; print(len(json.load(open(sys.argv[1]))["requests"]))' \
  "$SCRATCH/evidence-requests.round-1.json")"

if [ "$REQUEST_COUNT" -eq 0 ]; then
  cp "$SCRATCH/input.json" "$SCRATCH/input.round-1.json"
  cp "$SCRATCH/assessment-input.json" "$SCRATCH/assessment-input.round-1.json"
  cp "$SCRATCH/agent-input.json" "$SCRATCH/agent-input.round-1.json"
  printf '[]\n' > "$SCRATCH/expansion-errors.round-1.json"
else
  python3 "$PWD/.ci-shepherd-build/scripts/expand.py" \
    --input "$SCRATCH/input.json" \
    --requests "$SCRATCH/evidence-requests.round-1.json" \
    --output "$SCRATCH/input.round-1.json" \
    --errors "$SCRATCH/expansion-errors.round-1.json" \
    --audit "$SCRATCH/api-calls.jsonl"

  python3 "$PWD/.ci-shepherd-build/scripts/prepare.py" \
    --input "$SCRATCH/input.round-1.json" \
    --output "$SCRATCH/assessment-input.round-1.json" \
    --max-bundle-records 25

  python3 "$PWD/.ci-shepherd-build/scripts/compact.py" \
    --prepared "$SCRATCH/assessment-input.round-1.json" \
    --fingerprints "$SCRATCH/fingerprints.jsonl" \
    --output "$SCRATCH/agent-input.round-1.json"
fi
```

Expected: round-1 paths exist in both branches. When requests exist, inputs are
expanded and regenerated with GET-only access. When no request exists, the
validated baseline inputs are copied unchanged.

- [ ] **Step 7: Run one fresh assessment agent**

Give a fresh agent only:

- `.ci-shepherd-build/SKILL.md`;
- `$SCRATCH/agent-input.round-1.json`; and
- the source issue numbers from the validated request document.

The agent writes only `$SCRATCH/agent-judgments.round-1.json`. It receives no
preliminary judgments and performs no GitHub access.

- [ ] **Step 8: Finalize, validate, and render**

Run:

```bash
python3 "$PWD/.ci-shepherd-build/scripts/finalize.py" \
  --agent-input "$SCRATCH/agent-input.round-1.json" \
  --agent-judgments "$SCRATCH/agent-judgments.round-1.json" \
  --output "$SCRATCH/judgments.json"

python3 "$PWD/.ci-shepherd-build/scripts/validate.py" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --judgments "$SCRATCH/judgments.json"

python3 "$PWD/.ci-shepherd-build/scripts/render.py" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --judgments "$SCRATCH/judgments.json" \
  --output "$SCRATCH/report.md"
```

Expected: the report is valid and contains a Watch queue.

- [ ] **Step 9: Render local action proposals**

Run:

```bash
python3 "$PWD/.ci-shepherd-build/scripts/propose_actions.py" \
  --snapshot "$SCRATCH/input.round-1.json" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --agent-input "$SCRATCH/agent-input.round-1.json" \
  --judgments "$SCRATCH/judgments.json" \
  --shepherd-author "$SHEPHERD_AUTHOR" \
  --output "$SCRATCH/action-proposals.json"
```

Expected: the output contains only local review proposals and performs no
GitHub writes.

### Task 5: Review and post exactly one watch comment

**Files:**
- Read: session artifact `ci-shepherd-live-cycle-1/action-proposals.json`
- Create: session artifact `ci-shepherd-live-cycle-1/approved-comment.md`
- Create: session artifact `ci-shepherd-live-cycle-1/action-results.json`

- [ ] **Step 1: Select the safest live watch candidate**

Choose one proposal with:

- medium confidence when available;
- a specific failure identity;
- at least one cited primary evidence record;
- a concrete `reassessWhen` event;
- `reviewRequired: false` in the corresponding final compact input, so the
  deterministic watch explanation is stable across fresh assessors;
- no existing shepherd status comment; and
- no ambiguity with a duplicate or canonical issue.

Do not substitute a frozen Trial 7 issue merely to continue.

After choosing, set `SELECTED_ISSUE_NUMBER` to the exact reviewed issue number
and extract only that proposal:

```bash
python3 - \
  "$SCRATCH/action-proposals.json" \
  "$SELECTED_ISSUE_NUMBER" \
  "$SCRATCH/selected-action.json" <<'PY'
import json
from pathlib import Path
import sys

source = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
issue_number = int(sys.argv[2])
matches = [
    proposal
    for proposal in source["proposals"]
    if proposal["issueNumber"] == issue_number
]
if len(matches) != 1:
    raise SystemExit(
        f"Expected one proposal for issue {issue_number}, found {len(matches)}."
    )
Path(sys.argv[3]).write_text(
    json.dumps(matches[0], indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
Path(sys.argv[3]).chmod(0o600)
PY
```

- [ ] **Step 2: Run read-only preflight**

Run:

```bash
ISSUE_NUMBER="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["issueNumber"])' \
  "$SCRATCH/selected-action.json")"

gh issue view "$ISSUE_NUMBER" \
  --repo microsoft/aspire \
  --json number,title,state,stateReason,updatedAt,labels,assignees,comments
```

Expected: issue remains open, no matching owned shepherd status comment exists,
and the material state still matches the proposal.

- [ ] **Step 3: Show the exact effect to the user**

Present:

- issue number, title, and URL;
- complete generated comment;
- cited evidence and why this is watch rather than investigate or close;
- expected visible result; and
- the fact that no other issue or pull request will be touched.

Stop for explicit user approval. Do not post while the user is unavailable.

- [ ] **Step 4: Write the approved body to the session artifact**

Use the exact approved body from the proposal. Write it to
`approved-comment.md` without shell interpolation:

```bash
python3 - \
  "$SCRATCH/selected-action.json" \
  "$SCRATCH/approved-comment.md" <<'PY'
import json
from pathlib import Path
import sys

proposal = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).write_text(proposal["body"], encoding="utf-8")
Path(sys.argv[2]).chmod(0o600)
PY
```

Preserve the `[automated] ` prefix and both identity-only HTML markers.

- [ ] **Step 5: Re-run preflight immediately before posting**

Repeat Step 2. Abort as stale if material issue state changed.

- [ ] **Step 6: Post the approved comment**

Run:

```bash
COMMENT_URL="$(gh issue comment "$ISSUE_NUMBER" \
  --repo microsoft/aspire \
  --body-file "$SCRATCH/approved-comment.md")"
```

Expected: GitHub returns the new issue-comment URL.

- [ ] **Step 7: Reconcile the posted comment**

Fetch the exact comment and verify its body and author. Then write
`action-results.json` from the observed values:

```bash
python3 - \
  "$ISSUE_NUMBER" \
  "$COMMENT_URL" \
  "$SCRATCH/action-results.json" <<'PY'
import json
from pathlib import Path
import sys

issue_number = int(sys.argv[1])
document = {
    "schemaVersion": 1,
    "results": [
        {
            "issueNumber": issue_number,
            "idempotencyKey": f"issue:{issue_number}:watch",
            "outcome": "executed",
            "commentUrl": sys.argv[2],
        }
    ],
}
Path(sys.argv[3]).write_text(
    json.dumps(document, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
Path(sys.argv[3]).chmod(0o600)
PY
```

### Task 6: Recollect and prove unchanged idempotency

**Files:**
- Create: session artifact directory `ci-shepherd-live-cycle-1/recollection/`

- [ ] **Step 1: Run a second full live collection**

Run `collect.py` again with the same `--shepherd-author` into the recollection
directory. Do not reuse the first snapshot.

Expected: the posted comment appears as owned `shepherdStatus` evidence but
contributes no markers, facts, or references.

- [ ] **Step 2: Regenerate the two-stage assessment**

Repeat Task 4 Steps 4-8 against the recollection snapshot with fresh planner and
assessor agents.

Expected: a valid second `judgments.json` and `report.md`.

- [ ] **Step 3: Render second-run proposals**

Run `propose_actions.py` against the recollection artifacts.

Expected when the selected issue's watch state is materially unchanged:
`proposals` contains no action for `SELECTED_ISSUE_NUMBER`, and
`unchangedIssueNumbers` contains that integer. The list may contain other
unchanged watch issues only if they already had an owned canonical status
comment.

- [ ] **Step 4: Record the slice result**

Write `cycle-summary.md` with:

- first and second snapshot IDs;
- selected issue and posted comment URL;
- whether the second assessment preserved or changed the disposition;
- whether a duplicate comment or edit was proposed;
- any manual correction requested by the user; and
- any collector or proposal problem discovered.

- [ ] **Step 5: Stop before the next external effect**

Show the result to the user. Do not begin Copilot assignment, duplicate closure,
or quarantine work until the user explicitly continues.
