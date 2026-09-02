from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
import json
import os
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence

from .quarantine import (
    _issue_labels,
    quarantine_tool_tree_digest,
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from .quarantine_mutation import (
    validate_quarantine_mutation_result,
    validate_quarantine_post_inspection,
)
from .quarantine_result import (
    validate_quarantine_pull_request_target,
    validate_required_quarantine_approvals,
)
from .repository_policy import load_embedded_repository_policy


_PULL_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MergedQuarantineSourceVerification:
    verified: bool
    code: str
    reason: str

    def __bool__(self) -> bool:
        return self.verified


QUARANTINE_LABEL = "quarantined-test"
_LABEL_HUMAN_ACTION = (
    "Confirm whether this test should be quarantined. Either quarantine it "
    f"against this issue or remove the `{QUARANTINE_LABEL}` label."
)


def reconcile_quarantine_source(
    prepared: Mapping[str, Any],
    source_state: Mapping[str, Any] | None,
    session_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, object]:
    """Reconcile ``quarantined-test`` issues against the inspected checkout.

    The label is a routing hint. Only the pinned source inspection decides
    whether a current ``[QuarantinedTest]`` attribute exists, so a labelled
    issue with no matching attribute becomes a human question rather than a
    silent "already quarantined" skip.
    """
    repository = prepared.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Prepared repository must be a nonempty string.")

    labeled = _labeled_issues(prepared)
    pinned = _validated_source_state(source_state)
    if pinned is None:
        return {
            "schemaVersion": 1,
            "repository": repository,
            "sourceRevision": None,
            "sourceTreeDigest": None,
            "findings": [],
            "unverifiableIssueNumbers": [issue["issueNumber"] for issue in labeled],
        }

    findings: list[dict[str, object]] = []
    for issue in labeled:
        finding = _reconcile_labeled_issue(issue, pinned, session_events)
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda item: int(item["issueNumber"]))
    return {
        "schemaVersion": 1,
        "repository": repository,
        "sourceRevision": pinned["sourceRevision"],
        "sourceTreeDigest": pinned["sourceTreeDigest"],
        "findings": findings,
        "unverifiableIssueNumbers": [],
    }


def render_quarantine_source_reconciliation_section(
    document: Mapping[str, Any],
) -> str:
    lines = ["## Quarantine source reconciliation", ""]
    unverifiable = document.get("unverifiableIssueNumbers")
    findings = document.get("findings")
    if isinstance(unverifiable, list) and unverifiable:
        lines.append(
            "The `quarantined-test` label on these issues could not be verified "
            "against source: "
            + ", ".join(f"#{number}" for number in unverifiable)
            + "."
        )
        return "\n".join(lines) + "\n"
    if not isinstance(findings, list) or not findings:
        lines.append(
            "Every `quarantined-test` issue matches a current "
            "`[QuarantinedTest]` attribute."
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            (
                "These issues disagree with the inspected source at revision "
                f"`{document.get('sourceRevision')}`. Each one is a human "
                "decision; the shepherd changed no source, label, or issue "
                "metadata."
            ),
            "",
            "| Issue | Finding | Claimed test | Current source |",
            "|---|---|---|---|",
        ]
    )
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        current = finding.get("currentSource")
        rendered_source = (
            ", ".join(
                f"`{entry.get('testName')}` (`{entry.get('file')}:{entry.get('line')}`)"
                for entry in current
                if isinstance(entry, Mapping)
            )
            if isinstance(current, list) and current
            else "absent"
        )
        lines.append(
            f"| [#{finding.get('issueNumber')}]({finding.get('issueUrl')}) "
            f"| {finding.get('kind')} "
            f"| `{finding.get('claimedTestName')}` "
            f"| {rendered_source} |"
        )
    return "\n".join(lines) + "\n"


def quarantine_labeled_test_names(
    prepared: Mapping[str, Any],
) -> list[str] | None:
    """Return the test names ``quarantined-test`` issues claim, or ``None``.

    ``None`` means no issue carries the label, so the caller can skip the
    repository-wide source scan entirely. An empty list still requires the
    scan: an attribute may cite a labelled issue whose own metadata names no
    test at all.
    """
    labeled = _labeled_issues(prepared)
    if not labeled:
        return None
    return sorted(
        {
            issue["testName"]
            for issue in labeled
            if isinstance(issue["testName"], str)
        }
    )


def _reconcile_labeled_issue(
    issue: Mapping[str, Any],
    pinned: Mapping[str, Any],
    session_events: Sequence[Mapping[str, Any]],
) -> dict[str, object] | None:
    issue_number = issue["issueNumber"]
    issue_url = issue["issueUrl"]
    claimed = issue["testName"]
    linked = pinned["quarantinesByIssueUrl"].get(_normalized_issue_url(issue_url), [])
    if linked:
        if claimed is None or any(
            (
                entry["testName"] == claimed
                if issue["hasRawTestName"]
                else entry["testName"].casefold() == claimed.casefold()
            )
            for entry in linked
        ):
            return None
        current = [
            {
                "testName": entry["testName"],
                "file": entry["file"],
                "line": entry["line"],
                "quarantineIssueUrls": [entry["issueUrl"]],
            }
            for entry in linked
        ]
        locations = ", ".join(
            f"`{entry['testName']}` at `{entry['file']}:{entry['line']}`"
            for entry in linked
        )
        return {
            "issueNumber": issue_number,
            "issueUrl": issue_url,
            "kind": "attribute-name-drift",
            "claimedTestName": claimed,
            "currentSource": current,
            "summary": (
                "This issue is still linked from a `[QuarantinedTest]` "
                f"attribute, but the quarantined method is now {locations} "
                f"rather than the `{claimed}` this issue names."
            ),
            "humanAction": (
                "Update the issue title and metadata to the current method "
                "name. The shepherd does not edit issue metadata."
            ),
        }

    result = pinned["testsByName"].get(claimed)
    if result is not None and result["status"] == "ambiguous":
        current = [
            {
                "testName": claimed,
                "file": match["file"],
                "line": match["line"],
                "quarantineIssueUrls": list(match["quarantineIssueUrls"]),
            }
            for match in result["matches"]
        ]
        return {
            "issueNumber": issue_number,
            "issueUrl": issue_url,
            "kind": "ambiguous-inspection",
            "claimedTestName": claimed,
            "currentSource": current,
            "summary": (
                f"`{claimed}` resolves to multiple source matches, so the "
                "shepherd cannot determine which method should carry the "
                "`[QuarantinedTest]` attribute."
            ),
            "humanAction": (
                "Identify the current canonical test method and update the "
                "issue metadata or source attribute. The shepherd will not "
                "infer identity from ambiguous matches."
            ),
        }
    if result is None or result["status"] != "resolved":
        if claimed is None:
            # No parseable test name: the repository-wide inventory is still
            # exact enough to say nothing links this issue.
            return {
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "kind": "label-without-attribute",
                "claimedTestName": None,
                "currentSource": [],
                "summary": (
                    f"The `{QUARANTINE_LABEL}` label is on this issue, but no "
                    "`[QuarantinedTest]` attribute in the inspected source "
                    "links it."
                ),
                "humanAction": _LABEL_HUMAN_ACTION,
            }
        if result is None or result["status"] != "not-found":
            return None
        prior = _completed_quarantine(session_events, claimed, issue_url)
        if prior is None:
            return {
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "kind": "ambiguous-absence",
                "reason": "no-recorded-quarantine",
                "claimedTestName": claimed,
                "currentSource": [],
                "summary": (
                    f"`{claimed}` is absent from the inspected source and no "
                    "`[QuarantinedTest]` attribute links this issue, but the "
                    "shepherd has no record of quarantining it, so removal and "
                    "rename are indistinguishable."
                ),
                "humanAction": (
                    "Decide whether the test was renamed or removed. The "
                    "shepherd will not close this issue on absence alone."
                ),
            }
        rename_candidates = [
            entry
            for entry in pinned["quarantines"]
            if entry["testName"].rsplit(".", 1)[-1] == claimed.rsplit(".", 1)[-1]
        ]
        if rename_candidates:
            return {
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "kind": "ambiguous-absence",
                "reason": "possible-move-candidate",
                "claimedTestName": claimed,
                "currentSource": [
                    {
                        "testName": entry["testName"],
                        "file": entry["file"],
                        "line": entry["line"],
                        "quarantineIssueUrls": (
                            [entry["issueUrl"]]
                            if entry["issueUrl"] is not None
                            else []
                        ),
                    }
                    for entry in rename_candidates
                ],
                "summary": (
                    f"`{claimed}` is absent from the inspected source, but a "
                    "quarantined method with the same leaf name still exists, "
                    "so removal and a move cannot be distinguished."
                ),
                "humanAction": (
                    "Decide whether the test moved or was removed. The shepherd "
                    "will not close this issue on absence alone."
                ),
            }
        return {
            "issueNumber": issue_number,
            "issueUrl": issue_url,
            "kind": "removed-test-closure-review",
            "claimedTestName": claimed,
            "currentSource": [],
            "priorQuarantine": prior,
            "summary": (
                f"`{claimed}` was quarantined for this issue by "
                f"{prior['pullRequestUrl']}, and the inspected source now "
                "contains neither that method nor any `[QuarantinedTest]` "
                "attribute linking this issue."
            ),
            "humanAction": (
                "Confirm the test was deleted rather than renamed, then close "
                "this issue. The shepherd does not close issues on absence."
            ),
        }

    match = result["matches"][0]
    matching_issue_url = _normalized_issue_url(issue_url)
    if any(
        _normalized_issue_url(url) == matching_issue_url
        for url in match["quarantineIssueUrls"]
    ):
        return None
    location = f"{match['file']}:{match['line']}"
    quarantine_issue_urls = list(match["quarantineIssueUrls"])
    attribute_summary = (
        "carries no `[QuarantinedTest]` attribute"
        if not quarantine_issue_urls
        else "has `[QuarantinedTest]` attributes that link other issues"
    )
    return {
        "issueNumber": issue_number,
        "issueUrl": issue_url,
        "kind": "label-without-attribute",
        "claimedTestName": claimed,
        "currentSource": [
            {
                "testName": claimed,
                "file": match["file"],
                "line": match["line"],
                "quarantineIssueUrls": quarantine_issue_urls,
            }
        ],
        "summary": (
            f"The `{QUARANTINE_LABEL}` label is on this issue, but "
            f"`{claimed}` at `{location}` {attribute_summary} in the "
            "inspected source."
        ),
        "humanAction": _LABEL_HUMAN_ACTION,
    }


def _completed_quarantine(
    session_events: Sequence[Mapping[str, Any]],
    test_name: str,
    issue_url: str,
) -> dict[str, str] | None:
    """Find proof that the shepherd itself merged this exact quarantine.

    Absence alone never proves removal: a rename also removes the old method.
    Only a recorded ``completed`` session shows the attribute did exist for
    this issue, which is what makes "gone entirely" a defensible reading.
    """
    normalized_issue_url = _normalized_issue_url(issue_url)
    for event in reversed(list(session_events)):
        if not isinstance(event, Mapping) or event.get("status") != "completed":
            continue
        pull_request_url = event.get("pullRequestUrl")
        recorded_at = event.get("recordedAt")
        if not isinstance(pull_request_url, str) or not isinstance(recorded_at, str):
            continue
        for test in event.get("tests", []):
            if (
                isinstance(test, Mapping)
                and test.get("testName") == test_name
                and isinstance(test.get("issueUrl"), str)
                and _normalized_issue_url(test["issueUrl"]) == normalized_issue_url
            ):
                return {
                    "pullRequestUrl": pull_request_url,
                    "recordedAt": recorded_at,
                }
    return None


def _labeled_issues(prepared: Mapping[str, Any]) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for issue in prepared.get("issues", []):
        if not isinstance(issue, Mapping):
            continue
        issue_number = issue.get("issueNumber")
        issue_url = issue.get("issueUrl")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or not isinstance(issue_url, str)
            or not issue_url
        ):
            continue
        labels = _issue_labels(issue)
        if not isinstance(labels, frozenset) or QUARANTINE_LABEL not in labels:
            continue
        identity = issue.get("identity")
        test_name = identity.get("tier2TestName") if isinstance(identity, Mapping) else None
        raw_test_name = (
            identity.get("tier2TestNameRaw")
            if isinstance(identity, Mapping)
            else None
        )
        has_raw_test_name = (
            isinstance(raw_test_name, str) and bool(raw_test_name.strip())
        )
        claimed_test_name = raw_test_name if has_raw_test_name else test_name
        labeled.append(
            {
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "testName": (
                    claimed_test_name.strip()
                    if (
                        isinstance(claimed_test_name, str)
                        and claimed_test_name.strip()
                    )
                    else None
                ),
                "hasRawTestName": has_raw_test_name,
            }
        )
    labeled.sort(key=lambda item: int(item["issueNumber"]))
    return labeled


def _validated_source_state(
    source_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the pinned source state, or ``None`` when it cannot be trusted.

    Every downstream claim quotes exact file and line evidence, so an
    unpinned or malformed document must produce no claims at all.
    """
    if not isinstance(source_state, Mapping):
        return None
    revision = source_state.get("sourceRevision")
    tree_digest = source_state.get("sourceTreeDigest")
    if (
        source_state.get("schemaVersion") != 1
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or not isinstance(tree_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", tree_digest) is None
    ):
        return None

    tests = source_state.get("tests")
    if not isinstance(tests, list):
        return None
    tests_by_name: dict[str, dict[str, Any]] = {}
    for result in tests:
        if not isinstance(result, Mapping):
            return None
        test_name = result.get("testName")
        status = result.get("status")
        matches = result.get("matches")
        if (
            not isinstance(test_name, str)
            or not test_name
            or test_name in tests_by_name
            or status not in {"resolved", "not-found", "ambiguous"}
            or not isinstance(matches, list)
        ):
            return None
        normalized_matches: list[dict[str, Any]] = []
        for match in matches:
            normalized = _normalized_match(match)
            if normalized is None:
                return None
            normalized_matches.append(normalized)
        if status == "resolved" and len(normalized_matches) != 1:
            return None
        tests_by_name[test_name] = {
            "status": status,
            "matches": normalized_matches,
        }

    raw_quarantines = source_state.get("quarantines")
    if not isinstance(raw_quarantines, list):
        return None
    inventory: list[dict[str, Any]] = []
    quarantines_by_issue_url: dict[str, list[dict[str, Any]]] = {}
    for entry in raw_quarantines:
        if not isinstance(entry, Mapping):
            return None
        entry_test_name = entry.get("testName")
        entry_issue_url = entry.get("issueUrl")
        entry_file = entry.get("file")
        entry_line = entry.get("line")
        if (
            not isinstance(entry_test_name, str)
            or not entry_test_name
            or not isinstance(entry_file, str)
            or not entry_file
            or not isinstance(entry_line, int)
            or isinstance(entry_line, bool)
            or entry_line < 1
            or (
                entry_issue_url is not None
                and (not isinstance(entry_issue_url, str) or not entry_issue_url)
            )
        ):
            return None
        normalized_entry = {
            "testName": entry_test_name,
            "issueUrl": entry_issue_url,
            "file": entry_file,
            "line": entry_line,
        }
        inventory.append(normalized_entry)
        if isinstance(entry_issue_url, str):
            quarantines_by_issue_url.setdefault(
                _normalized_issue_url(entry_issue_url),
                [],
            ).append(normalized_entry)
    return {
        "sourceRevision": revision,
        "sourceTreeDigest": tree_digest,
        "testsByName": tests_by_name,
        "quarantinesByIssueUrl": quarantines_by_issue_url,
        "quarantines": inventory,
    }


def _normalized_issue_url(issue_url: str) -> str:
    return issue_url.strip().rstrip("/").casefold()


def _normalized_match(match: object) -> dict[str, Any] | None:
    if not isinstance(match, Mapping):
        return None
    file = match.get("file")
    line = match.get("line")
    attributes = match.get("quarantineAttributes")
    if (
        not isinstance(file, str)
        or not file
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line < 1
        or not isinstance(attributes, list)
    ):
        return None
    issue_urls: list[str] = []
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            return None
        issue_url = attribute.get("issueUrl")
        if isinstance(issue_url, str) and issue_url:
            issue_urls.append(issue_url)
    return {"file": file, "line": line, "quarantineIssueUrls": issue_urls}


def reconcile_quarantine_pull_requests(
    *,
    state_directory: Path,
    repository: str,
    recorded_at: str,
    get_pull: Callable[[str, int], Mapping[str, Any]],
    find_pull: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
    get_reviews: Callable[[str, int], list[Mapping[str, Any]]] | None = None,
    verify_merged_source: Callable[
        [Mapping[str, Any], Mapping[str, Any]],
        bool | MergedQuarantineSourceVerification,
    ]
    | None = None,
) -> dict[str, object]:
    events = read_quarantine_session_events(state_directory)
    latest_by_batch: dict[str, dict[str, Any]] = {}
    for event in events:
        batch_id = event.get("batchId")
        if (
            isinstance(batch_id, str)
            and str(event.get("repository", "")).casefold()
            == repository.casefold()
        ):
            latest_by_batch[batch_id] = event

    outcomes: list[dict[str, object]] = []
    for batch_id, event in sorted(latest_by_batch.items()):
        if event.get("status") == "publication-pending":
            if find_pull is None:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": (
                            "Publication recovery requires an exact pull request lookup."
                        ),
                    }
                )
                continue
            try:
                policy = load_embedded_repository_policy(
                    event.get("repositoryPolicy"),
                    repository,
                )
                allowed_heads = (
                    policy.quarantine_pull_request.allowed_head_repositories
                )
                if len(allowed_heads) != 1:
                    raise ValueError(
                        "Publication recovery requires exactly one allowed head "
                        "repository."
                    )
                pull = find_pull(repository, batch_id, next(iter(allowed_heads)))
            except (OSError, RuntimeError, ValueError) as error:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": f"Pull request lookup failed: {error}",
                    }
                )
                continue
            head_sha = event.get("pullRequestHeadSha")
            if pull is None:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "publication-pending",
                        "reason": "No pull request exists for the publication intent.",
                    }
                )
                continue
            try:
                validate_quarantine_pull_request_target(event, pull)
            except ValueError as error:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": str(error),
                    }
                )
                continue
            head = pull.get("head")
            actual_head_sha = (
                head.get("sha") if isinstance(head, Mapping) else None
            )
            url = pull.get("html_url")
            mutation_validation = event.get("mutationValidation")
            if (
                pull.get("state") not in {"open", "closed"}
                or pull.get("draft") is not True
                or not isinstance(url, str)
                or _PULL_URL_RE.fullmatch(url) is None
                or not isinstance(head_sha, str)
                or not isinstance(actual_head_sha, str)
                or actual_head_sha.casefold() != head_sha.casefold()
                or not isinstance(mutation_validation, Mapping)
            ):
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": (
                            "The publication intent does not match the live pull request."
                        ),
                    }
                )
                continue
            test_names = [
                str(test["testName"])
                for test in event.get("tests", [])
                if isinstance(test, Mapping)
                and isinstance(test.get("testName"), str)
            ]
            if pull.get("state") == "closed":
                record_quarantine_session_event(
                    state_directory,
                    event,
                    status="failed",
                    recorded_at=recorded_at,
                    session_id=str(event["sessionId"]),
                    failure_reason=(
                        "The quarantine pull request closed without merging."
                    ),
                    blocked_targets=[
                        {
                            "testName": test_name,
                            "reason": (
                                "The quarantine pull request closed without merging."
                            ),
                        }
                        for test_name in test_names
                    ],
                )
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "recovered-closed",
                        "pullRequestUrl": url,
                    }
                )
                continue
            record_quarantine_session_event(
                state_directory,
                event,
                status="pull-request-open",
                recorded_at=recorded_at,
                session_id=str(event["sessionId"]),
                pull_request_url=url,
                pull_request_head_sha=head_sha,
                completed_test_names=test_names,
                mutation_validation=mutation_validation,
            )
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "recovered-open",
                    "pullRequestUrl": url,
                }
            )
            continue
        if event.get("status") != "pull-request-open":
            continue
        url = event.get("pullRequestUrl")
        head_sha = event.get("pullRequestHeadSha")
        match = _PULL_URL_RE.fullmatch(str(url))
        if (
            match is None
            or match.group("repository").casefold() != repository.casefold()
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None
        ):
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "The ledger lacks an exact pull request URL and head SHA.",
                }
            )
            continue

        try:
            pull = get_pull(repository, int(match.group("number")))
        except (OSError, RuntimeError, ValueError) as error:
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": f"Pull request lookup failed: {error}",
                }
            )
            continue
        try:
            validate_quarantine_pull_request_target(event, pull)
        except ValueError as error:
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": str(error),
                }
            )
            continue
        actual_head = pull.get("head")
        actual_head_sha = (
            actual_head.get("sha") if isinstance(actual_head, Mapping) else None
        )
        if (
            pull.get("html_url") != url
            or not isinstance(actual_head_sha, str)
            or actual_head_sha.casefold() != head_sha.casefold()
        ):
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "The live pull request identity or head has changed.",
                }
            )
            continue

        test_names = [
            str(test["testName"])
            for test in event.get("tests", [])
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
        ]
        blocked_targets = [
            {
                "testName": str(target["test"]["testName"]),
                "reason": str(target["reason"]),
            }
            for target in event.get("blockedTargets", [])
            if isinstance(target, Mapping)
            and isinstance(target.get("test"), Mapping)
            and isinstance(target["test"].get("testName"), str)
            and isinstance(target.get("reason"), str)
        ]
        full_request = {
            **event,
            "tests": [
                *event.get("tests", []),
                *[
                    target["test"]
                    for target in event.get("blockedTargets", [])
                    if isinstance(target, Mapping)
                    and isinstance(target.get("test"), Mapping)
                ],
            ],
        }
        common = {
            "state_directory": state_directory,
            "request": full_request,
            "recorded_at": recorded_at,
            "session_id": str(event["sessionId"]),
        }
        if pull.get("state") == "closed" and pull.get("merged_at") is not None:
            try:
                validate_required_quarantine_approvals(
                    event,
                    pull,
                    (
                        get_reviews(repository, int(match.group("number")))
                        if get_reviews is not None
                        else None
                    ),
                )
            except ValueError as error:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": str(error),
                    }
                )
                continue
            mutation_validation = event.get("mutationValidation")
            verification = (
                verify_merged_source(event, pull)
                if (
                    isinstance(mutation_validation, Mapping)
                    and verify_merged_source is not None
                )
                else False
            )
            verification_succeeded = (
                verification is True
                or (
                    isinstance(
                        verification,
                        MergedQuarantineSourceVerification,
                    )
                    and verification.verified
                    and verification.code == "verified"
                )
            )
            if not verification_succeeded:
                reason = (
                    verification.reason
                    if isinstance(
                        verification,
                        MergedQuarantineSourceVerification,
                    )
                    else (
                        "The exact quarantine attributes were not verified "
                        "at the merged commit."
                    )
                )
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": reason,
                    }
                )
                continue
            record_quarantine_session_event(
                **common,
                status="completed",
                pull_request_url=str(url),
                pull_request_head_sha=head_sha,
                completed_test_names=test_names,
                blocked_targets=blocked_targets,
                mutation_validation=mutation_validation,
            )
            status = "completed"
        elif pull.get("state") == "closed" and pull.get("merged_at") is None:
            closed_blocked_targets = {
                target["testName"]: target
                for target in blocked_targets
            }
            for test_name in test_names:
                closed_blocked_targets[test_name] = {
                    "testName": test_name,
                    "reason": (
                        "The quarantine pull request closed without merging."
                    ),
                }
            record_quarantine_session_event(
                **common,
                status="failed",
                failure_reason="The quarantine pull request closed without merging.",
                blocked_targets=list(closed_blocked_targets.values()),
            )
            status = "closed-unmerged"
        elif pull.get("state") == "open":
            status = "pending"
        else:
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "GitHub returned an unsupported pull request state.",
                }
            )
            continue
        outcomes.append(
            {
                "batchId": batch_id,
                "status": status,
                "pullRequestUrl": url,
            }
        )
    return {
        "schemaVersion": 1,
        "repository": repository,
        "outcomes": outcomes,
    }


def verify_merged_quarantine_source(
    request: Mapping[str, Any],
    mutation_result: Mapping[str, Any],
    *,
    merge_commit_sha: str,
    tool_project: Path,
    get_file: Callable[[str, str], bytes],
    timeout_seconds: int = 300,
) -> MergedQuarantineSourceVerification:
    if re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit_sha) is None:
        return _merged_verification(
            "invalid-input",
            "The merge commit SHA is invalid.",
        )
    try:
        validated_mutation = validate_quarantine_mutation_result(
            request,
            mutation_result,
        )
    except ValueError as error:
        return _merged_verification(
            "invalid-input",
            f"The merged-source input is invalid: {error}",
        )

    expected_inspector_digest = request.get("inspectorTreeDigest")
    if not isinstance(expected_inspector_digest, str):
        return _merged_verification(
            "invalid-input",
            "The request lacks an inspector tree digest.",
        )
    try:
        actual_inspector_digest = quarantine_tool_tree_digest(tool_project)
    except OSError as error:
        return _merged_verification(
            "inspector-runtime-failed",
            f"The merged-source inspector could not be read: {error}",
        )
    if actual_inspector_digest != expected_inspector_digest:
        return _merged_verification(
            "inspector-digest-drift",
            "The merged-source inspector differs from the inspected version.",
        )

    try:
        with TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            for changed_file in validated_mutation["changedFiles"]:
                if (
                    not isinstance(changed_file, str)
                    or not changed_file.startswith("tests/")
                ):
                    return _merged_verification(
                        "invalid-input",
                        "A changed file is outside the tests directory.",
                    )
                relative_path = Path(changed_file).relative_to("tests")
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    return _merged_verification(
                        "invalid-input",
                        "A changed test path is unsafe.",
                    )
                destination = tests_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    content = get_file(changed_file, merge_commit_sha)
                except (OSError, ValueError) as error:
                    return _merged_verification(
                        "source-fetch-failed",
                        f"The merged source could not be fetched: {error}",
                    )
                if not isinstance(content, bytes):
                    return _merged_verification(
                        "source-fetch-failed",
                        "The merged source response was not bytes.",
                    )
                destination.write_bytes(content)
            test_names = list(validated_mutation["completedTests"])
            try:
                completed = subprocess.run(
                    [
                        "dotnet",
                        "run",
                        "--project",
                        str(tool_project),
                        "--no-restore",
                        "--verbosity",
                        "quiet",
                        "--",
                        "--inspect",
                        "--root",
                        str(tests_root),
                        *test_names,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env={
                        **os.environ,
                        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                        "DOTNET_CLI_UI_LANGUAGE": "en-US",
                        "DOTNET_NOLOGO": "1",
                        "DOTNET_ROLL_FORWARD": "Major",
                        "MSBUILDTERMINALLOGGER": "false",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return _merged_verification(
                    "inspector-runtime-failed",
                    f"The merged-source inspector could not run: {error}",
                )
            if completed.returncode != 0:
                return _merged_verification(
                    "inspector-runtime-failed",
                    (
                        "The merged-source inspector exited with code "
                        f"{completed.returncode}."
                    ),
                )
            try:
                inspection = json.loads(completed.stdout)
            except json.JSONDecodeError:
                return _merged_verification(
                    "inspector-output-malformed",
                    "The merged-source inspector returned malformed JSON.",
                )
            if not _is_inspection_document(inspection):
                return _merged_verification(
                    "inspector-output-malformed",
                    "The merged-source inspector returned an invalid document.",
                )
            try:
                validated_source = validate_quarantine_post_inspection(
                    request,
                    inspection,
                )
            except ValueError as error:
                return _merged_verification(
                    "merged-source-mismatch",
                    f"The merged quarantine source does not match: {error}",
                )
            if (
                validated_source["completedTests"]
                != validated_mutation["completedTests"]
            ):
                return _merged_verification(
                    "merged-source-mismatch",
                    "The merged quarantine test set does not match the mutation.",
                )
            return MergedQuarantineSourceVerification(
                verified=True,
                code="verified",
                reason="The exact quarantine attributes exist at the merge commit.",
            )
    except OSError as error:
        return _merged_verification(
            "source-fetch-failed",
            f"The merged source could not be materialized: {error}",
        )


def _is_inspection_document(value: object) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schemaVersion", "tests"}
        or value.get("schemaVersion") != 1
        or not isinstance(value.get("tests"), list)
    ):
        return False
    return all(
        isinstance(test, Mapping)
        and set(test) == {"testName", "status", "matches"}
        and isinstance(test.get("testName"), str)
        and bool(test["testName"])
        and isinstance(test.get("status"), str)
        and isinstance(test.get("matches"), list)
        for test in value["tests"]
    )


def _merged_verification(
    code: str,
    reason: str,
) -> MergedQuarantineSourceVerification:
    return MergedQuarantineSourceVerification(
        verified=False,
        code=code,
        reason=reason,
    )
