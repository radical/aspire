from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
import re
from typing import Mapping


_MAX_GITHUB_ID_DIGITS = 20
_GITHUB_ID_PATTERN = rf"[1-9][0-9]{{0,{_MAX_GITHUB_ID_DIGITS - 1}}}"
_MARKER_KEYS = frozenset(
    {
        "automation-broken",
        "autoclose",
        "ci-failure",
        "ci-failure-cause",
        "gh-aw-agentic-workflow",
        "gh-aw-expires",
        "gh-aw-failure-issue",
        "run",
    }
)
_HTML_MARKER_RE = re.compile(
    rf"<!--\s*(?P<key>{'|'.join(re.escape(key) for key in sorted(_MARKER_KEYS))})"
    r"\s*:\s*(?P<value>.*?)\s*-->",
    re.IGNORECASE,
)
_FULL_ISSUE_OR_PULL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s<>()]+)/(?P<repo>[^/\s<>()]+)/"
    rf"(?P<kind>issues|pull)/(?P<number>{_GITHUB_ID_PATTERN})\b",
    re.IGNORECASE,
)
_RUN_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s<>()]+)/(?P<repo>[^/\s<>()]+)/"
    rf"actions/runs/(?P<run_id>{_GITHUB_ID_PATTERN})\b",
    re.IGNORECASE,
)
_COMMIT_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s<>()]+)/(?P<repo>[^/\s<>()]+)/"
    r"commit/(?P<sha>[0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
_LOCAL_ISSUE_RE = re.compile(rf"(?<![\w/])#(?P<number>{_GITHUB_ID_PATTERN})\b")
_LABELLED_COMMIT_RE = re.compile(r"(?i)\b(?:commit|sha)\s*[:#]?\s*`?(?P<sha>[0-9a-f]{7,40})\b")
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_FENCED_CODE_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|\Z)"
    r".*?(?:^[ \t]*(?P=fence)[ \t]*(?:\r?$)|\Z)"
)
_INLINE_CODE_RE = re.compile(
    r"(?P<delimiter>`+)(?!`)[^\r\n]*?(?P=delimiter)(?!`)"
)
_TRIGGERING_PULL_RE = re.compile(
    rf"(?im)^pull request\s*:\s*#(?P<number>{_GITHUB_ID_PATTERN})\s*$"
)
_BUILD_RE = re.compile(
    rf"(?im)^build\s*:\s*(?P<url>https?://github\.com/[^/\s]+/[^/\s]+/actions/runs/{_GITHUB_ID_PATTERN})\s*$"
)
_BUILD_ERROR_LEG_RE = re.compile(
    r"(?im)^build error leg(?: or test failing)?\s*:\s*(?P<value>.+?)\s*$"
)
_BUILD_ERROR_TEST_SUFFIX_RE = re.compile(r"^(?P<job>.+) / `(?P<test>[^`\r\n]+)`$")
_TYPE_RE = re.compile(
    r"(?im)^\*\*type(?:\*\*\s*:|:\*\*)\s*`?(?P<value>[^`\r\n]+?)`?\s*$"
)
_LEGACY_FACT_PATTERNS = {
    "testName": re.compile(r"(?im)^test name\s*:\s*(?P<value>.+?)\s*$"),
    "exceptionType": re.compile(r"(?im)^exception type\s*:\s*(?P<value>.+?)\s*$"),
    "errorCode": re.compile(r"(?im)^error code\s*:\s*(?P<value>.+?)\s*$"),
    "workflow": re.compile(r"(?im)^workflow\s*:\s*(?P<value>.+?)\s*$"),
    "job": re.compile(r"(?im)^job\s*:\s*(?P<value>.+?)\s*$"),
    "step": re.compile(r"(?im)^step\s*:\s*(?P<value>.+?)\s*$"),
}
_EXCEPTION_RE = re.compile(r"\b(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*Exception\b")
_COMPILER_CODE_RE = re.compile(r"\bCS[0-9]{4}\b", re.IGNORECASE)
_HTTP_STATUS_RE = re.compile(
    r"(?i)(?:\bHTTP(?:/[0-9](?:\.[0-9])?)?\s+|"
    r"\bstatus code[^\r\n:]{0,80}:\s*)(?P<code>[1-5][0-9]{2})\b"
)
_EXIT_CODE_RE = re.compile(r"(?i)\bexit code\s+(?P<code>-?[0-9]+)\b")
_HEX_EXIT_CODE_RE = re.compile(
    r"(?i)(?:\bHRESULT\b|\bexit code\b|\bcorresponds(?:\s+to)?\b)"
    r"[^\r\n]{0,100}?\b(?P<code>0x[0-9a-f]{8})\b"
)
_ERROR_MESSAGE_HEADING_RE = re.compile(r"(?im)^##\s+Error Message\s*$")
_NEXT_HEADING_RE = re.compile(r"(?m)^##\s+")
_OCCURRENCES_HEADING_RE = re.compile(r"(?im)^##\s+Occurrences\s*$")
_RESOLUTION_HEADING_RE = re.compile(r"(?im)^##\s+Resolution\s*$")
_SECOND_LEVEL_HEADING_RE = re.compile(r"(?m)^##\s+")
_FIXED_BY_RE = re.compile(r"(?i)\b(?:fixed|resolved)\s+by\b")
_OCCURRENCE_BUILD_RE = re.compile(
    r"^\[(?P<label>[^\]]+)\]\((?P<url>https?://github\.com/[^/\s]+/[^/\s]+/actions/runs/"
    rf"(?P<run_id>{_GITHUB_ID_PATTERN}))\)$",
    re.IGNORECASE,
)
_OCCURRENCE_PR_RE = re.compile(
    rf"^#(?P<number>0|{_GITHUB_ID_PATTERN})(?:\s+\([^)]*\))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Occurrence:
    date: str
    source_run: int
    run_url: str
    job: str
    pull_request: int | None

    def as_record(self) -> dict[str, object]:
        return {
            "date": self.date,
            "sourceRun": self.source_run,
            "runUrl": self.run_url,
            "job": self.job,
            "pullRequest": self.pull_request,
        }


@dataclass(frozen=True, slots=True)
class OccurrenceLedger:
    source: str
    schema: str | None
    schema_recognized: bool
    source_record_count: int
    parsed_row_count: int
    complete: bool

    def as_record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "schema": self.schema,
            "schemaRecognized": self.schema_recognized,
            "sourceRecordCount": self.source_record_count,
            "parsedRowCount": self.parsed_row_count,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class IssueSignals:
    markers: tuple[Mapping[str, object], ...]
    facts: tuple[Mapping[str, object], ...]
    occurrences: tuple[Occurrence, ...]
    occurrence_ledger: OccurrenceLedger
    references: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ReferenceSelection:
    selected: tuple[Mapping[str, object], ...]
    excluded: tuple[Mapping[str, object], ...]


def select_references(
    references: tuple[Mapping[str, object], ...],
    occurrences: tuple[Occurrence, ...],
    *,
    max_run_refs_per_issue: int,
    max_issue_refs_per_issue: int,
    max_commit_refs_per_issue: int,
) -> ReferenceSelection:
    occurrence_dates = {
        occurrence.source_run: occurrence.date
        for occurrence in occurrences
    }

    def decision_key(reference: Mapping[str, object]) -> tuple[object, ...]:
        target_type = str(reference.get("targetType", ""))
        extraction_method = str(reference.get("extractionMethod", ""))
        run_id = reference.get("runId")
        if target_type == "workflow-run" and reference.get("decisionValue") == "explicit-resolution":
            priority = 0
            value: object = int(reference.get("decisionOrder", 0))
            secondary_value: object = 0
        elif target_type == "workflow-run" and isinstance(run_id, int) and run_id in occurrence_dates:
            priority = 1
            value: object = -int(occurrence_dates[run_id].replace("-", ""))
            secondary_value: object = -run_id
        elif reference.get("decisionValue") == "explicit-resolution":
            priority = 2
            value = int(reference.get("decisionOrder", 0))
            secondary_value = 0
        elif target_type == "workflow-run":
            priority = 3
            value = 0
            secondary_value = 0
        elif target_type == "commit":
            priority = 4
            value = str(reference.get("sha", ""))
            secondary_value = 0
        elif target_type == "pull-request" and extraction_method in {
            "triggering-pull-request",
            "occurrence-pull-request",
        }:
            priority = 5
            value = int(reference.get("targetNumber", 0))
            secondary_value = 0
        else:
            priority = 6
            value = int(reference.get("targetNumber", 0))
            secondary_value = 0
        return (
            priority,
            value,
            secondary_value,
            str(reference.get("sourceEvidenceId", "")),
            str(reference.get("targetRepository", "")).lower(),
            str(reference.get("targetUrl", "")),
        )

    def target_key(reference: Mapping[str, object]) -> tuple[object, ...]:
        target_type = str(reference["targetType"])
        target_repository = str(reference.get("targetRepository", "")).lower()
        if target_type == "workflow-run":
            target_value: object = reference.get("runId")
        elif target_type == "commit":
            target_value = str(reference.get("sha", "")).lower()
        else:
            target_value = reference.get("targetNumber")
        budget_type = "issue-or-pull-request" if target_type in {"issue", "pull-request"} else target_type
        return (budget_type, target_repository, target_value)

    budgets = {
        "workflow-run": ("max_run_refs_per_issue", max_run_refs_per_issue),
        "commit": ("max_commit_refs_per_issue", max_commit_refs_per_issue),
        "issue": ("max_issue_refs_per_issue", max_issue_refs_per_issue),
        "pull-request": ("max_issue_refs_per_issue", max_issue_refs_per_issue),
    }
    counts = {name: 0 for name, _ in budgets.values()}
    selected: list[Mapping[str, object]] = []
    excluded: list[Mapping[str, object]] = []
    references_by_target: dict[tuple[object, ...], list[Mapping[str, object]]] = {}
    for reference in sorted(references, key=decision_key):
        references_by_target.setdefault(target_key(reference), []).append(reference)

    for target_references in references_by_target.values():
        budget_name, limit = budgets[str(target_references[0]["targetType"])]
        if counts[budget_name] < limit:
            counts[budget_name] += 1
            selected.extend(target_references)
        else:
            excluded.extend(
                MappingProxyType(
                    {**reference, "exclusionReason": budget_name}
                )
                for reference in target_references
            )
    return ReferenceSelection(tuple(selected), tuple(excluded))


def extract_issue_signals(
    issue_number: int,
    source_evidence_id: str,
    source_url: str,
    text: str,
    local_repository: str,
) -> IssueSignals:
    owner, repository = local_repository.split("/", 1)
    markers = _extract_markers(text, source_evidence_id)
    occurrences, occurrence_spans, occurrence_ledger = _extract_occurrences(text)
    facts = _extract_facts(text, source_evidence_id, markers)
    references = _extract_references(
        issue_number,
        source_evidence_id,
        source_url,
        text,
        local_repository,
        owner,
        repository,
        markers,
        occurrences,
        occurrence_spans,
    )
    return IssueSignals(
        markers=_freeze_records(
            markers,
            ("key", "normalized", "method", "sourceEvidenceId"),
        ),
        facts=_freeze_records(
            facts,
            ("field", "normalized", "method", "sourceEvidenceId"),
        ),
        occurrences=tuple(
            sorted(
                set(occurrences),
                key=lambda occurrence: (
                    occurrence.date,
                    occurrence.source_run,
                    occurrence.run_url,
                    occurrence.job,
                    occurrence.pull_request is not None,
                    occurrence.pull_request or 0,
                ),
            )
        ),
        occurrence_ledger=occurrence_ledger,
        references=_freeze_references(references),
    )


def _extract_markers(text: str, source_evidence_id: str) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    for match in _HTML_MARKER_RE.finditer(text):
        key = match.group("key").lower()
        if key not in _MARKER_KEYS:
            continue
        raw = match.group("value").strip()
        if not raw:
            continue
        markers.append(
            {
                "key": key,
                "raw": raw,
                "normalized": _normalize_generic(raw),
                "method": "html-comment",
                "sourceEvidenceId": source_evidence_id,
            }
        )
    return markers


def _extract_facts(
    text: str,
    source_evidence_id: str,
    markers: list[dict[str, object]],
) -> list[dict[str, object]]:
    facts: list[dict[str, object]] = []
    for marker in markers:
        if marker["key"] == "ci-failure-cause":
            _append_fact(
                facts,
                "causeId",
                str(marker["raw"]),
                "html-comment",
                source_evidence_id,
            )

    for match in _BUILD_RE.finditer(text):
        _append_fact(
            facts,
            "sourceRun",
            match.group("url"),
            "build-run",
            source_evidence_id,
        )

    for match in _BUILD_ERROR_LEG_RE.finditer(text):
        value = match.group("value")
        job = value
        test_suffix = _BUILD_ERROR_TEST_SUFFIX_RE.fullmatch(value)
        if (
            value.count("`") == 2
            and test_suffix is not None
        ):
            job = test_suffix.group("job")
            _append_fact(
                facts,
                "testName",
                test_suffix.group("test"),
                "build-error-leg",
                source_evidence_id,
            )
        _append_fact(
            facts,
            "job",
            job,
            "build-error-leg",
            source_evidence_id,
        )

    for match in _TRIGGERING_PULL_RE.finditer(text):
        _append_fact(
            facts,
            "triggeringPullRequest",
            f"#{match.group('number')}",
            "triggering-pull-request",
            source_evidence_id,
            normalized=match.group("number"),
        )

    for match in _TYPE_RE.finditer(text):
        _append_fact(
            facts,
            "failureType",
            match.group("value"),
            "labelled-type",
            source_evidence_id,
        )

    # Retain the collector's established labelled-line records for old snapshots
    # while the structured forms above handle Aspire's current issue template.
    for field, pattern in _LEGACY_FACT_PATTERNS.items():
        for match in pattern.finditer(text):
            _append_fact(
                facts,
                field,
                match.group("value"),
                "labelled-line",
                source_evidence_id,
            )

    error_message = _section_body(text, _ERROR_MESSAGE_HEADING_RE)
    for match in _EXCEPTION_RE.finditer(error_message):
        _append_fact(
            facts,
            "exceptionType",
            match.group(0),
            "error-message",
            source_evidence_id,
        )
    for match in _COMPILER_CODE_RE.finditer(error_message):
        _append_fact(
            facts,
            "errorCode",
            match.group(0).upper(),
            "compiler-error-code",
            source_evidence_id,
        )
    for match in _HTTP_STATUS_RE.finditer(error_message):
        _append_fact(
            facts,
            "errorCode",
            match.group("code"),
            "http-status",
            source_evidence_id,
        )
    for match in _EXIT_CODE_RE.finditer(error_message):
        _append_fact(
            facts,
            "errorCode",
            match.group("code"),
            "exit-code",
            source_evidence_id,
        )
    for match in _HEX_EXIT_CODE_RE.finditer(error_message):
        raw = match.group("code")
        _append_fact(
            facts,
            "errorCode",
            raw,
            "hex-exit-code",
            source_evidence_id,
            normalized=raw.upper(),
        )
    return facts


def _append_fact(
    facts: list[dict[str, object]],
    field: str,
    raw: str,
    method: str,
    source_evidence_id: str,
    *,
    normalized: str | None = None,
) -> None:
    stripped = raw.strip()
    if not stripped:
        return
    facts.append(
        {
            "field": field,
            "raw": stripped,
            "normalized": normalized if normalized is not None else _normalize_fact(field, stripped),
            "method": method,
            "sourceEvidenceId": source_evidence_id,
        }
    )


def _extract_occurrences(
    text: str,
) -> tuple[list[Occurrence], list[tuple[int, int]], OccurrenceLedger]:
    heading = _OCCURRENCES_HEADING_RE.search(text)
    if heading is None:
        return (
            [],
            [],
            OccurrenceLedger(
                source="none",
                schema=None,
                schema_recognized=False,
                source_record_count=0,
                parsed_row_count=0,
                complete=False,
            ),
        )
    following = text[heading.end() :]
    next_heading = _NEXT_HEADING_RE.search(following)
    end = heading.end() + (next_heading.start() if next_heading is not None else len(following))
    section_start = heading.end()
    section = text[section_start:end]

    occurrences: list[Occurrence] = []
    occurrence_row_spans: list[tuple[int, int]] = []
    column_indexes: dict[str, int] | None = None
    schema: str | None = None
    source_record_count = 0
    parsed_row_count = 0
    header_seen = False
    offset = 0
    for line in section.splitlines(keepends=True):
        line_without_ending = line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            offset += len(line)
            continue
        occurrence_row_spans.append(
            (section_start + offset, section_start + offset + len(line_without_ending))
        )
        offset += len(line)
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        normalized_cells = [cell.lower() for cell in cells]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if not header_seen:
            header_seen = True
            if normalized_cells == ["date", "build", "job", "pr"]:
                schema = "occurrences-v1"
                column_indexes = {
                    "date": 0,
                    "build": 1,
                    "job": 2,
                    "pullRequest": 3,
                }
            elif normalized_cells == [
                "date",
                "build",
                "branch",
                "job",
                "triggering merge",
            ]:
                schema = "occurrences-v2"
                column_indexes = {
                    "date": 0,
                    "build": 1,
                    "job": 3,
                    "pullRequest": 4,
                }
            continue
        source_record_count += 1
        if column_indexes is None or max(column_indexes.values()) >= len(cells):
            continue
        date_cell = cells[column_indexes["date"]]
        build_cell = cells[column_indexes["build"]]
        job_cell = cells[column_indexes["job"]]
        pr_cell = cells[column_indexes["pullRequest"]]
        try:
            date.fromisoformat(date_cell)
        except ValueError:
            continue
        build_match = _OCCURRENCE_BUILD_RE.fullmatch(build_cell)
        pr_match = _OCCURRENCE_PR_RE.fullmatch(pr_cell)
        if build_match is None or pr_match is None or not job_cell:
            continue
        run_id = _parse_github_id(build_match.group("run_id"))
        label_match = re.fullmatch(
            rf"(?:run\s+#?)?(?P<run_id>{_GITHUB_ID_PATTERN})",
            build_match.group("label").strip(),
            re.IGNORECASE,
        )
        label_run_id = (
            _parse_github_id(label_match.group("run_id"))
            if label_match is not None
            else None
        )
        pull_request = (
            None
            if pr_match.group("number") == "0"
            else _parse_github_id(pr_match.group("number"))
        )
        if (
            run_id is None
            or label_run_id != run_id
            or (pull_request is None and pr_match.group("number") != "0")
        ):
            continue
        parsed_row_count += 1
        occurrences.append(
            Occurrence(
                date=date_cell,
                source_run=run_id,
                run_url=build_match.group("url"),
                job=job_cell,
                pull_request=pull_request,
            )
        )
    schema_recognized = schema is not None
    return (
        occurrences,
        occurrence_row_spans,
        OccurrenceLedger(
            source="body-table",
            schema=schema,
            schema_recognized=schema_recognized,
            source_record_count=source_record_count,
            parsed_row_count=parsed_row_count,
            complete=(
                schema_recognized
                and source_record_count > 0
                and parsed_row_count == source_record_count
            ),
        ),
    )


def _extract_references(
    issue_number: int,
    source_evidence_id: str,
    source_url: str,
    text: str,
    local_repository: str,
    owner: str,
    repository: str,
    markers: list[dict[str, object]],
    occurrences: list[Occurrence],
    occurrence_spans: list[tuple[int, int]],
) -> list[dict[str, object]]:
    references: list[dict[str, object]] = []
    resolution_spans = _resolution_spans(text)

    for match in _FULL_ISSUE_OR_PULL_RE.finditer(text):
        number = _parse_github_id(match.group("number"))
        if number is None:
            continue
        kind = match.group("kind").lower()
        references.append(
            {
                **_numbered_reference(
                    issue_number,
                    source_evidence_id,
                    source_url,
                    "issue" if kind == "issues" else "pull-request",
                    f"{match.group('owner')}/{match.group('repo')}",
                    number,
                    match.group(0),
                    "full-issue-url" if kind == "issues" else "full-pull-url",
                ),
                **_decision_metadata(text, match.start(), resolution_spans),
            }
        )

    for match in _RUN_RE.finditer(text):
        run_id = _parse_github_id(match.group("run_id"))
        if run_id is None:
            continue
        references.append(
            {
                **_reference_source(issue_number, source_evidence_id, source_url),
                "targetType": "workflow-run",
                "targetRepository": f"{match.group('owner')}/{match.group('repo')}",
                "runId": run_id,
                "targetUrl": match.group(0),
                "extractionMethod": "actions-run-url",
                **_decision_metadata(text, match.start(), resolution_spans),
            }
        )

    for match in _COMMIT_URL_RE.finditer(text):
        sha = match.group("sha").lower()
        references.append(
            {
                **_reference_source(issue_number, source_evidence_id, source_url),
                "targetType": "commit",
                "targetRepository": f"{match.group('owner')}/{match.group('repo')}",
                "sha": sha,
                "targetUrl": match.group(0),
                "extractionMethod": "commit-url",
                **_decision_metadata(text, match.start(), resolution_spans),
            }
        )
    masked_spans: list[tuple[int, int]] = list(occurrence_spans)
    masked_spans.extend(_markdown_link_spans(text))
    masked_spans.extend(match.span() for match in _URL_RE.finditer(text))
    masked_spans.extend(match.span() for match in _FENCED_CODE_BLOCK_RE.finditer(text))
    masked_spans.extend(match.span() for match in _INLINE_CODE_RE.finditer(text))

    for match in _TRIGGERING_PULL_RE.finditer(text):
        number = _parse_github_id(match.group("number"))
        if number is None:
            continue
        references.append(
            _numbered_reference(
                issue_number,
                source_evidence_id,
                source_url,
                "pull-request",
                local_repository,
                number,
                f"https://github.com/{owner}/{repository}/pull/{number}",
                "triggering-pull-request",
            )
        )
        masked_spans.append(match.span())

    for occurrence in occurrences:
        if occurrence.pull_request is None:
            continue
        references.append(
            _numbered_reference(
                issue_number,
                source_evidence_id,
                source_url,
                "pull-request",
                local_repository,
                occurrence.pull_request,
                f"https://github.com/{owner}/{repository}/pull/{occurrence.pull_request}",
                "occurrence-pull-request",
            )
        )

    for marker in markers:
        if marker["key"] != "run":
            continue
        run_id = _parse_github_id(str(marker["raw"]))
        if run_id is None:
            continue
        references.append(
            {
                **_reference_source(issue_number, source_evidence_id, source_url),
                "targetType": "workflow-run",
                "targetRepository": local_repository,
                "runId": run_id,
                "targetUrl": f"https://github.com/{owner}/{repository}/actions/runs/{run_id}",
                "extractionMethod": "run-marker",
            }
        )

    masked_text = _mask_spans(text, masked_spans)
    for match in _LOCAL_ISSUE_RE.finditer(masked_text):
        number = _parse_github_id(match.group("number"))
        if number is None:
            continue
        references.append(
            {
                **_numbered_reference(
                    issue_number,
                    source_evidence_id,
                    source_url,
                    "issue",
                    local_repository,
                    number,
                    f"https://github.com/{owner}/{repository}/issues/{number}",
                    "local-issue",
                ),
                **_decision_metadata(text, match.start(), resolution_spans),
            }
        )

    for match in _LABELLED_COMMIT_RE.finditer(masked_text):
        sha = match.group("sha").lower()
        references.append(
            {
                **_reference_source(issue_number, source_evidence_id, source_url),
                "targetType": "commit",
                "targetRepository": local_repository,
                "sha": sha,
                "targetUrl": f"https://github.com/{owner}/{repository}/commit/{sha}",
                "extractionMethod": "labelled-commit",
                **_decision_metadata(text, match.start(), resolution_spans),
            }
        )
    return references


def _resolution_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for match in _RESOLUTION_HEADING_RE.finditer(text):
        next_heading = _SECOND_LEVEL_HEADING_RE.search(text, match.end())
        spans.append((match.end(), next_heading.start() if next_heading is not None else len(text)))
    return tuple(spans)


def _decision_metadata(
    text: str,
    position: int,
    resolution_spans: tuple[tuple[int, int], ...],
) -> dict[str, object]:
    in_resolution = any(start <= position < end for start, end in resolution_spans)
    line_start = text.rfind("\n", 0, position) + 1
    fixed_by = _FIXED_BY_RE.search(text, line_start, position) is not None
    if not in_resolution and not fixed_by:
        return {}
    return {
        "decisionValue": "explicit-resolution",
        "decisionOrder": position,
    }


def _parse_github_id(raw: str) -> int | None:
    if (
        not raw
        or len(raw) > _MAX_GITHUB_ID_DIGITS
        or not raw.isascii()
        or not raw.isdigit()
        or raw[0] == "0"
    ):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _markdown_link_spans(text: str) -> list[tuple[int, int]]:
    # Pair delimiters in one pass so malformed repeated link openers cannot
    # repeatedly rescan the remainder of a line.
    square_stack: list[int] = []
    parenthesis_stack: list[int] = []
    square_pairs: dict[int, int] = {}
    parenthesis_pairs: dict[int, int] = {}
    escaped = False

    for index, character in enumerate(text):
        if character == "\n":
            square_stack.clear()
            parenthesis_stack.clear()
            escaped = False
            continue
        if character == "\\":
            escaped = not escaped
            continue
        if escaped:
            escaped = False
            continue
        if character == "[":
            square_stack.append(index)
        elif character == "]" and square_stack:
            square_pairs[square_stack.pop()] = index
        elif character == "(":
            parenthesis_stack.append(index)
        elif character == ")" and parenthesis_stack:
            parenthesis_pairs[parenthesis_stack.pop()] = index

    spans: list[tuple[int, int]] = []
    for label_start, label_end in square_pairs.items():
        destination_start = label_end + 1
        if destination_start >= len(text):
            continue
        if text[destination_start] == "(":
            destination_end = parenthesis_pairs.get(destination_start)
        elif text[destination_start] == "[":
            destination_end = square_pairs.get(destination_start)
        else:
            continue
        if destination_end is not None:
            spans.append((label_start, destination_end + 1))
    return spans


def _numbered_reference(
    issue_number: int,
    source_evidence_id: str,
    source_url: str,
    target_type: str,
    target_repository: str,
    target_number: int,
    target_url: str,
    extraction_method: str,
) -> dict[str, object]:
    return {
        **_reference_source(issue_number, source_evidence_id, source_url),
        "targetType": target_type,
        "targetRepository": target_repository,
        "targetNumber": target_number,
        "targetUrl": target_url,
        "extractionMethod": extraction_method,
    }


def _reference_source(
    issue_number: int,
    source_evidence_id: str,
    source_url: str,
) -> dict[str, object]:
    return {
        "sourceIssueNumber": issue_number,
        "sourceEvidenceId": source_evidence_id,
        "sourceUrl": source_url,
    }


def _freeze_records(
    records: list[dict[str, object]],
    keys: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for record in records:
        identity = tuple(record.get(key) for key in keys)
        unique.setdefault(identity, record)
    return tuple(
        MappingProxyType(dict(record))
        for _, record in sorted(unique.items(), key=lambda item: tuple(str(part) for part in item[0]))
    )


def _freeze_references(
    references: list[dict[str, object]],
) -> tuple[Mapping[str, object], ...]:
    method_priority = {
        "triggering-pull-request": 0,
        "full-issue-url": 1,
        "full-pull-url": 1,
        "actions-run-url": 1,
        "commit-url": 1,
        "occurrence-pull-request": 2,
        "run-marker": 2,
        "local-issue": 3,
        "labelled-commit": 3,
    }
    ordered = sorted(
        references,
        key=lambda record: (
            str(record.get("sourceEvidenceId", "")),
            str(record.get("targetType", "")),
            str(record.get("targetRepository", "")).lower(),
            str(record.get("targetNumber", record.get("runId", record.get("sha", "")))),
            record.get("decisionValue") != "explicit-resolution",
            method_priority.get(str(record.get("extractionMethod", "")), 99),
            str(record.get("targetUrl", "")),
        ),
    )
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for record in ordered:
        identity = (
            record.get("sourceEvidenceId"),
            record.get("targetType"),
            str(record.get("targetRepository", "")).lower(),
            record.get("targetNumber", record.get("runId", record.get("sha"))),
        )
        unique.setdefault(identity, record)
    return tuple(MappingProxyType(dict(record)) for record in unique.values())


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _section_body(text: str, heading_pattern: re.Pattern[str]) -> str:
    heading = heading_pattern.search(text)
    if heading is None:
        return ""
    following = text[heading.end() :]
    next_heading = _NEXT_HEADING_RE.search(following)
    if next_heading is None:
        return following
    return following[: next_heading.start()]


def _normalize_generic(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def _normalize_fact(field: str, value: str) -> str:
    collapsed = " ".join(value.strip().split())
    if field == "errorCode":
        return collapsed.upper()
    return collapsed.lower()
