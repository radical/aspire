from __future__ import annotations

"""Task 2 observation building: occurrences, coverage, and fingerprint summaries.

Shared surface with :mod:`ci_shepherd.lifecycle`: ``is_scoped_to_issue`` is public
precisely because lifecycle scopes its evidence bundles with the same rule. Timestamp
parsing and formatting live in :mod:`ci_shepherd.timeutils`; lifecycle decision
helpers live in :mod:`ci_shepherd.lifecycle`. Everything else here is private to
Task 2.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Mapping, NamedTuple
from urllib.parse import quote

from ci_shepherd.naming import normalize_component
from ci_shepherd.policy import ManualPolicy
from ci_shepherd.timeutils import format_utc_z, parse_aware_iso8601


_FAILED_JOB_CONCLUSIONS = frozenset({"action_required", "failure", "startup_failure", "timed_out"})
# Matches an HTTP status inside raw log text, e.g.
#   "download-artifact failed with HTTP 502"
#   "##[error]Unable to download: status code returned was: 429"
#   "HTTP/1.1 503 Service Unavailable"
_HTTP_STATUS_RE = re.compile(
    r"(?i)(?:\bHTTP(?:/[0-9](?:\.[0-9])?)?\s+|\bstatus code[^\r\n:]{0,80}:\s*)(?P<code>[1-5][0-9]{2})\b"
)
_RETRY_RELEVANT_HTTP_STATUSES = frozenset({408, 425, 429, *range(500, 600)})
# MSBuild/Roslyn/NuGet/SDK diagnostic codes. Used with fullmatch against structured
# `errorCode` facts, where the collector already isolated the bare code.
_BUILD_BREAK_CODE_RE = re.compile(r"\b(?:CS[0-9]{4}|NU[0-9]{4}|NETSDK[0-9]{4})\b", re.IGNORECASE)
# Raw-text form of the same codes, anchored to the severity token so prose that merely
# mentions a code is not treated as a build break, e.g.
#   "src/Program.cs(10,20): error CS1002: ; expected"     -> CS1002
#   "##[error]error NU1101: Unable to find package Foo"   -> NU1101
#   "  Actual:   status code sequence CS1002"             -> no match
_BUILD_BREAK_DIAGNOSTIC_RE = re.compile(r"(?i)\b(?:error|warning)\s+(?P<code>(?:CS|NU|NETSDK)[0-9]{4})\b")
# A raw log line only counts as diagnostic evidence when it carries a failure marker.
# Without this, expected/actual text inside a test assertion would be mined for causes.
_DIAGNOSTIC_LINE_RE = re.compile(
    r"(?i)(?:##\[error\]|\berror\b|\bfailed\b|\bfailure\b|\bfatal\b|\btimed out\b|\btimeout\b"
    r"|\bexception\b|\bunable to\b|\brefused\b|\breset by peer\b)"
)
# Assertion/diff lines emitted by test frameworks, e.g.
#   "  Expected: HTTP 503"
#   "  Actual:   HTTP 200 OK"
#   "  Assert.Equal() Failure: Strings differ"
# These describe the test's own expectations, never the infrastructure that ran it.
_ASSERTION_LINE_RE = re.compile(r"(?i)^\s*(?:expected|actual|assert[a-z.]*)\b")
# `dotnet test` console output for a passing test, e.g. "  Passed Alpha.Tests.One [42 ms]".
_PASSED_TEST_RE = re.compile(r"(?m)^\s*Passed\s+(?P<test>.+?)\s+\[[^\]\r\n]+\]\s*$")
# Runner labels that name an operating system image, e.g. "ubuntu-latest",
# "windows-2022", "macos-14.1". Anything else in a matrix job name (a TFM, a
# configuration, a shard index) is lane detail, not an OS.
_RUNNER_OS_RE = re.compile(r"(?i)^(?:ubuntu|windows|macos)-(?:latest|[0-9]+(?:\.[0-9]+)?)$")
# Repository roots whose files are CI/build configuration rather than product code.
_REPO_CONFIG_PATH_PREFIXES = (".github/", "eng/")
# Local workflow evidence IDs, e.g.
#   "run:100"                          (workflow run)
#   "run:100:attempt:1:job:900"        (workflow job)
#   "run:100:attempt:none:job:900:log" (workflow log, attempt unknown)
_WORKFLOW_EVIDENCE_ID_RE = re.compile(
    r"^run:(?P<run_id>[1-9][0-9]*)"
    r"(?::attempt:(?P<attempt>[1-9][0-9]*|none):job:(?P<job_id>[1-9][0-9]*|none)(?P<log>:log)?)?$"
)
_ISSUE_EVIDENCE_ID_RE = re.compile(r"^issue:(?P<issue_number>[1-9][0-9]*)$")
# Check-run annotation evidence IDs emitted by the collector, e.g.
#   "run:7001:check:8001:annotation:9001"
# Annotations are attached to a job's check run and carry the failure text, but they
# are not workflow jobs: they have no lane, status, or conclusion of their own, so
# they must never be counted as runs, coverage, or occurrences.
_ANNOTATION_EVIDENCE_ID_RE = re.compile(
    r"^run:(?P<run_id>[1-9][0-9]*):check:(?P<check_run_id>[1-9][0-9]*)"
    r":annotation:(?P<annotation_id>[1-9][0-9]*)$"
)
# Ordinal occurrence IDs, e.g.
#   "occurrence:12:100:1:900:2"        (issue 12, run 100, attempt 1, job 900, second occurrence)
#   "occurrence:12:100:none:none:1"    (issue-ledger occurrence with no resolvable job/attempt)
_OCCURRENCE_ID_RE = re.compile(
    r"^occurrence:(?P<issue_number>[1-9][0-9]*):(?P<run_id>[1-9][0-9]*):"
    r"(?P<attempt>[1-9][0-9]*|none):(?P<job_id>[1-9][0-9]*|none):(?P<ordinal>[1-9][0-9]*)$"
)
_TEST_ALLOWED_CAUSES = (
    "test-flake",
    "test-contention",
    "product-regression-suspect",
    "unknown",
)
# Occurrence fields that must agree when the same occurrence ID appears in both the
# current snapshot and history. Divergence means the ordinal ID was reused for
# different facts, which would silently discard one of them.
_OCCURRENCE_IDENTITY_FIELDS = ("fingerprintId", "testName", "issueNumber", "runId", "attempt", "jobId")
# History keys observations will read. Everything else - notably causes and
# proposals - is a decision, not an observation.
_FACTUAL_HISTORY_FIELDS = frozenset({"occurrences", "coverage"})


def build_observations(
    snapshot: Mapping[str, Any],
    *,
    policy: ManualPolicy,
    history: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    evidence = _require_mapping(snapshot.get("evidence"), "Snapshot evidence")
    issue_numbers = _require_int_list(snapshot.get("openIssues"), "Snapshot openIssues")
    collected_at = parse_aware_iso8601(snapshot.get("collectedAt"), "Snapshot collectedAt")
    window_cutoff = collected_at - timedelta(days=policy.systemic_transient_window_days)
    _require_factual_history(history)

    records = _index_evidence(evidence)
    runs_by_id = _index_runs(records)
    logs_by_key = _index_logs(records)
    _require_parent_run_evidence(records, runs_by_id)
    for issue_number in issue_numbers:
        _require_issue_record(issue_number, records)
    issue_test_names = {
        issue_number: _collect_issue_test_names(issue_number, records)
        for issue_number in issue_numbers
    }
    issue_test_anchors = {
        issue_number: _resolve_issue_test_anchor(issue_number, records, runs_by_id)
        for issue_number in issue_numbers
    }

    occurrences: list[dict[str, Any]] = []
    for issue_number in issue_numbers:
        anchor = issue_test_anchors[issue_number]
        for record in records.values():
            if (
                record.availability != "available"
                or not record.is_job
                or not is_scoped_to_issue(record.evidence_id, record.payload, issue_number)
            ):
                continue
            if str(record.payload.get("conclusion", "")).lower() not in _FAILED_JOB_CONCLUSIONS:
                continue
            occurrences.extend(
                _build_job_occurrences(
                    issue_number=issue_number,
                    job=record,
                    run_record=runs_by_id[
                        _require_positive_int(record.payload.get("runId"), f"{record.evidence_id} payload.runId")
                    ],
                    logs_by_key=logs_by_key,
                    # Issue-level test facts reach a job only through a deterministic
                    # anchor; otherwise they would be replayed onto every failed job.
                    issue_test_names=(
                        issue_test_names[issue_number]
                        if anchor is not None and anchor[0] == "job" and anchor[1] == record.evidence_id
                        else []
                    ),
                    policy=policy,
                    records=records,
                )
            )
        if anchor is not None and anchor[0] == "run":
            occurrences.extend(
                _build_issue_ledger_occurrences(
                    issue_number=issue_number,
                    run_record=runs_by_id[int(anchor[1])],
                    issue_test_names=issue_test_names[issue_number],
                )
            )

    occurrences = _assign_occurrence_ids(occurrences, _history_occurrence_ordinals(history))
    coverage = _build_coverage(
        records,
        runs_by_id,
        logs_by_key,
        window_cutoff=window_cutoff,
        collected_at=collected_at,
    )
    fingerprints = _build_fingerprint_summaries(
        occurrences,
        history,
        records,
        runs_by_id,
        window_cutoff=window_cutoff,
        collected_at=collected_at,
    )
    return {"occurrences": occurrences, "coverage": coverage, "fingerprints": fingerprints}


def _independent_recovery_eligible(attempt: object) -> bool:
    return isinstance(attempt, int) and not isinstance(attempt, bool) and attempt == 1


def is_annotation_evidence_id(evidence_id: str) -> bool:
    """True for check-run annotation IDs the collector files under ``workflow-job``.

    The collector has no separate annotation kind, so the evidence ID is the only
    thing that tells an annotation apart from the job it hangs off.
    """
    return _ANNOTATION_EVIDENCE_ID_RE.fullmatch(evidence_id) is not None


def _is_successful_job_execution(payload: Mapping[str, Any]) -> bool:
    """True only when a job finished and reported success.

    A ``cancelled``, ``skipped``, ``neutral``, or ``stale`` job never produced a
    verdict for its lane, and a job that is still queued or in progress has not
    produced one yet - GitHub leaves ``conclusion`` null until it completes. None of
    those prove the lane executed without this failure, so none may stand in for the
    population a failure rate divides by. Coverage records and rate population share
    this predicate so the two can never disagree about what counts as a success.
    """
    return (
        str(payload.get("status", "")).lower() == "completed"
        and str(payload.get("conclusion", "")).lower() == "success"
    )


class _EvidenceRecord:
    def __init__(self, evidence_id: str, record: Mapping[str, Any]) -> None:
        self.evidence_id = evidence_id
        self.kind = _require_string(record.get("kind"), f"{evidence_id}.kind")
        self.availability = _require_string(record.get("availability"), f"{evidence_id}.availability")
        self.payload = _require_mapping(record.get("payload"), f"{evidence_id}.payload")
        self.is_annotation = self.kind == "workflow-job" and is_annotation_evidence_id(evidence_id)
        _validate_identity_payload(evidence_id, self.kind, self.payload)

    @property
    def is_job(self) -> bool:
        """True only for records that describe an actual workflow job execution.

        The collector files check-run annotations under the ``workflow-job`` kind, so
        kind alone cannot distinguish a job from an annotation attached to one.
        """
        return self.kind == "workflow-job" and not self.is_annotation


def _index_evidence(evidence: Mapping[str, Any]) -> dict[str, _EvidenceRecord]:
    records: dict[str, _EvidenceRecord] = {}
    for evidence_id, record in evidence.items():
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("Snapshot evidence keys must be nonempty strings.")
        if not isinstance(record, Mapping):
            raise ValueError(f"Evidence {evidence_id} must be an object.")
        records[evidence_id] = _EvidenceRecord(evidence_id, record)
    return records


def _index_runs(records: Mapping[str, _EvidenceRecord]) -> dict[int, _EvidenceRecord]:
    runs_by_id: dict[int, _EvidenceRecord] = {}
    for record in records.values():
        if record.availability != "available" or record.kind != "workflow-run":
            continue
        run_id = _require_positive_int(record.payload.get("runId"), f"{record.evidence_id} payload.runId")
        runs_by_id[run_id] = record
    return runs_by_id


def _index_logs(records: Mapping[str, _EvidenceRecord]) -> dict[tuple[int, object, object], list[_EvidenceRecord]]:
    logs_by_key: dict[tuple[int, object, object], list[_EvidenceRecord]] = defaultdict(list)
    for record in records.values():
        if record.availability != "available" or record.kind != "workflow-log":
            continue
        run_id = _require_positive_int(record.payload.get("runId"), f"{record.evidence_id} payload.runId")
        logs_by_key[(run_id, record.payload.get("attempt"), record.payload.get("jobId"))].append(record)
    for logs in logs_by_key.values():
        logs.sort(key=lambda item: item.evidence_id)
    return logs_by_key


def _require_parent_run_evidence(
    records: Mapping[str, _EvidenceRecord],
    runs_by_id: Mapping[int, _EvidenceRecord],
) -> None:
    """Reject job evidence whose parent workflow run was not collected.

    Occurrence and coverage records both derive workflow, headSha, and observedAt
    from the parent run, so a job without its run cannot produce a comparable
    record. This is enforced symmetrically for failed and successful jobs; letting
    successful jobs through would silently shrink coverage and failure-rate
    denominators instead of surfacing the collection gap.
    """
    for job in sorted(
        (
            record
            for record in records.values()
            if record.availability == "available" and record.is_job
        ),
        key=lambda record: record.evidence_id,
    ):
        run_id = _require_positive_int(job.payload.get("runId"), f"{job.evidence_id} payload.runId")
        if run_id not in runs_by_id:
            raise ValueError(
                f"{job.evidence_id} requires workflow-run evidence run:{run_id}."
            )


def _resolve_issue_test_anchor(
    issue_number: int,
    records: Mapping[str, _EvidenceRecord],
    runs_by_id: Mapping[int, _EvidenceRecord],
) -> tuple[str, object] | None:
    """Find the single lane an issue's test facts can be attributed to, if any.

    Issue bodies carry test names without lane, run, or attempt scope. Attributing
    them to every failed job would fabricate a Cartesian product of occurrences and
    invent cross-OS recurrence that the evidence never showed. Attribution therefore
    requires a deterministic anchor:

    1. exactly one available failed job scoped to the issue, or
    2. exactly one distinct ledger ``sourceRun`` with collected run evidence -
       narrowed to a job when the ledger names one that matches exactly one failed
       job in that run, otherwise left at run scope.

    Returns ``("job", evidence_id)``, ``("run", run_id)``, or ``None`` when the
    evidence is too ambiguous to attribute.
    """
    failed_jobs = sorted(
        (
            record
            for record in records.values()
            if record.availability == "available"
            and record.is_job
            and is_scoped_to_issue(record.evidence_id, record.payload, issue_number)
            and str(record.payload.get("conclusion", "")).lower() in _FAILED_JOB_CONCLUSIONS
        ),
        key=lambda record: record.evidence_id,
    )
    if len(failed_jobs) == 1:
        return ("job", failed_jobs[0].evidence_id)

    issue = records.get(f"issue:{issue_number}")
    if issue is None or issue.availability != "available":
        return None
    rows = _ledger_rows(issue)
    source_runs = {
        int(row["sourceRun"])
        for row in rows
        if isinstance(row.get("sourceRun"), int)
        and not isinstance(row.get("sourceRun"), bool)
        and int(row["sourceRun"]) > 0
    }
    if len(source_runs) != 1:
        return None
    run_id = next(iter(source_runs))
    if run_id not in runs_by_id:
        return None

    ledger_job_names = {
        row["job"].strip()
        for row in rows
        if isinstance(row.get("job"), str) and row["job"].strip()
    }
    if len(ledger_job_names) == 1:
        ledger_job_name = next(iter(ledger_job_names))
        matches = [
            job
            for job in failed_jobs
            if job.payload.get("runId") == run_id and job.payload.get("name") == ledger_job_name
        ]
        if len(matches) == 1:
            return ("job", matches[0].evidence_id)
    return ("run", run_id)


def _ledger_rows(issue: _EvidenceRecord) -> list[Mapping[str, Any]]:
    ledger = issue.payload.get("ledger")
    if not isinstance(ledger, Mapping):
        return []
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _require_issue_record(issue_number: int, records: Mapping[str, _EvidenceRecord]) -> None:
    evidence_id = f"issue:{issue_number}"
    record = records.get(evidence_id)
    if record is None:
        raise ValueError(f"Missing issue evidence for #{issue_number}.")
    if record.kind != "issue-event":
        raise ValueError(f"{evidence_id} must be issue-event evidence.")
    _require_identity_match(evidence_id, "issueNumber", issue_number, record.payload.get("number"))


def _validate_identity_payload(evidence_id: str, kind: str, payload: Mapping[str, Any]) -> None:
    if kind == "workflow-job":
        annotation = _ANNOTATION_EVIDENCE_ID_RE.fullmatch(evidence_id)
        if annotation is not None:
            _require_positive_int(payload.get("runId"), f"{evidence_id} payload.runId")
            _require_optional_positive_int(payload.get("attempt"), f"{evidence_id} payload.attempt")
            _require_optional_positive_int(payload.get("jobId"), f"{evidence_id} payload.jobId")
            for field_name, group in (
                ("runId", "run_id"),
                ("checkRunId", "check_run_id"),
                ("annotationId", "annotation_id"),
            ):
                if field_name in payload:
                    _require_positive_int(payload.get(field_name), f"{evidence_id} payload.{field_name}")
                    _require_identity_match(
                        evidence_id, field_name, int(annotation.group(group)), payload.get(field_name)
                    )
            return
    if kind in {"workflow-run", "workflow-job", "workflow-log"}:
        _require_positive_int(payload.get("runId"), f"{evidence_id} payload.runId")
        _require_optional_positive_int(payload.get("attempt"), f"{evidence_id} payload.attempt")
        parsed = _parse_workflow_evidence_id(evidence_id)
        if parsed is None:
            raise ValueError(f"{evidence_id} must use a supported local workflow evidence ID.")
        if kind == "workflow-run" and (parsed["attempt"] is not None or parsed["jobId"] is not None or parsed["isLog"]):
            raise ValueError(f"{evidence_id} must cite a top-level workflow run.")
        if kind == "workflow-job" and (parsed["jobId"] is None or parsed["isLog"]):
            raise ValueError(f"{evidence_id} must cite a workflow job.")
        if kind == "workflow-log" and (parsed["jobId"] is None or not parsed["isLog"]):
            raise ValueError(f"{evidence_id} must cite a workflow log.")
        _require_identity_match(evidence_id, "runId", parsed["runId"], payload.get("runId"))
        if kind in {"workflow-job", "workflow-log"} and (parsed["attempt"] is not None or "attempt" in payload):
            _require_identity_match(evidence_id, "attempt", parsed["attempt"], payload.get("attempt"))
    if kind in {"workflow-job", "workflow-log"}:
        _require_optional_positive_int(payload.get("jobId"), f"{evidence_id} payload.jobId")
        parsed = _parse_workflow_evidence_id(evidence_id)
        if parsed is not None and (parsed["jobId"] is not None or "jobId" in payload):
            _require_identity_match(evidence_id, "jobId", parsed["jobId"], payload.get("jobId"))
    if kind == "workflow-log":
        payload_evidence_id = payload.get("evidenceId")
        if payload_evidence_id is not None:
            payload_id = _require_string(payload_evidence_id, f"{evidence_id} payload.evidenceId")
            if payload_id != evidence_id:
                raise ValueError(f"{evidence_id} evidenceId mismatch: payload.evidenceId is {payload_id}.")
    if kind == "issue-event" and "number" in payload:
        issue_number = _require_positive_int(payload.get("number"), f"{evidence_id} payload.number")
        parsed = _parse_issue_evidence_id(evidence_id)
        if parsed is not None:
            _require_identity_match(evidence_id, "issueNumber", parsed["issueNumber"], issue_number)


def _build_job_occurrences(
    *,
    issue_number: int,
    job: _EvidenceRecord,
    run_record: _EvidenceRecord,
    logs_by_key: Mapping[tuple[int, object, object], list[_EvidenceRecord]],
    issue_test_names: list[tuple[str, str]],
    policy: ManualPolicy,
    records: Mapping[str, _EvidenceRecord],
) -> list[dict[str, Any]]:
    run_id = _require_positive_int(job.payload.get("runId"), f"{job.evidence_id} payload.runId")
    attempt = job.payload.get("attempt")
    job_id = job.payload.get("jobId")
    job_name = job.payload.get("name") if isinstance(job.payload.get("name"), str) else None
    log_records = logs_by_key.get((run_id, attempt, job_id), [])
    workflow = _workflow_name(run_record)
    lane, os_name = _lane_and_os(job.payload)
    observed_at = _observed_at(job.payload, run_record.payload)
    head_sha = run_record.payload.get("headSha")
    evidence_ids = _evidence_ids_for_occurrence(run_record, job, log_records)
    test_names = _collect_job_test_names(issue_test_names, job, log_records)

    if test_names:
        return [
            {
                "_sortTestName": test_name,
                "_sortEvidenceId": evidence_id,
                "issueNumber": issue_number,
                "runId": run_id,
                "attempt": attempt,
                "jobId": job_id,
                "workflow": workflow,
                "lane": lane,
                "os": os_name,
                "headSha": head_sha,
                "observedAt": observed_at,
                "testName": test_name,
                "fingerprintId": f"test:{normalize_component(test_name)}",
                "fingerprintComponents": _fingerprint_components(
                    runner_os=os_name,
                    job=job_name,
                    test_name=test_name,
                ),
                "allowedCauses": list(_TEST_ALLOWED_CAUSES),
                "retrySafe": False,
                "evidenceIds": sorted(set((*evidence_ids, evidence_id))),
            }
            for test_name, evidence_id in test_names
        ]

    build_break_code = _build_break_code(job, log_records)
    if build_break_code is not None:
        allowed_causes = ["toolchain-build-break"]
        if _has_repo_config_evidence(issue_number, records):
            allowed_causes.append("repo-config-break")
        allowed_causes.extend(["product-regression-suspect", "unknown"])
        return [
            _non_test_occurrence(
                issue_number=issue_number,
                run_id=run_id,
                attempt=attempt,
                job_id=job_id,
                workflow=workflow,
                lane=lane,
                os_name=os_name,
                head_sha=head_sha,
                observed_at=observed_at,
                fingerprint_id=f"build:{normalize_component(build_break_code)}:{normalize_component(str(job.payload.get('name') or 'none'))}",
                fingerprint_components=_fingerprint_components(
                    runner_os=os_name,
                    error_code=build_break_code,
                    job=job_name,
                ),
                allowed_causes=allowed_causes,
                retry_safe=False,
                evidence_ids=evidence_ids,
            )
        ]

    network = _network_pattern(job.payload, log_records)
    if network is not None:
        pattern_id, step = network
        return [
            _non_test_occurrence(
                issue_number=issue_number,
                run_id=run_id,
                attempt=attempt,
                job_id=job_id,
                workflow=workflow,
                lane=lane,
                os_name=os_name,
                head_sha=head_sha,
                observed_at=observed_at,
                fingerprint_id=f"infra:{normalize_component(pattern_id)}:{normalize_component(os_name)}:{normalize_component(step)}",
                fingerprint_components=_fingerprint_components(
                    pattern_id=pattern_id,
                    runner_os=os_name,
                    step=step,
                    job=job_name,
                ),
                allowed_causes=["infra-transient", "unknown"],
                retry_safe=normalize_component(pattern_id) in policy.retry_safe_pattern_ids,
                evidence_ids=evidence_ids,
            )
        ]

    return [
        _non_test_occurrence(
            issue_number=issue_number,
            run_id=run_id,
            attempt=attempt,
            job_id=job_id,
            workflow=workflow,
            lane=lane,
            os_name=os_name,
            head_sha=head_sha,
            observed_at=observed_at,
            fingerprint_id=(
                f"unknown:{normalize_component(str(issue_number))}:"
                f"{normalize_component(str(run_id))}:"
                f"{normalize_component(str(job_id) if job_id is not None else 'none')}"
            ),
            fingerprint_components=_fingerprint_components(
                runner_os=os_name,
                job=job_name,
            ),
            allowed_causes=["unknown"],
            retry_safe=False,
            evidence_ids=evidence_ids,
        )
    ]


def _build_issue_ledger_occurrences(
    *,
    issue_number: int,
    run_record: _EvidenceRecord,
    issue_test_names: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Emit run-scoped occurrences for issue test facts with no resolvable job.

    Attempt, lane, and OS stay ``None`` rather than being guessed from an
    arbitrary job in the run, so downstream lifecycle can block on the data-quality
    gap instead of acting on fabricated identity.
    """
    run_id = _require_positive_int(run_record.payload.get("runId"), f"{run_record.evidence_id} payload.runId")
    workflow = _workflow_name(run_record)
    observed_at = _observed_at({}, run_record.payload)
    return [
        {
            "_sortTestName": test_name,
            "_sortEvidenceId": evidence_id,
            "issueNumber": issue_number,
            "runId": run_id,
            "attempt": None,
            "jobId": None,
            "workflow": workflow,
            "lane": None,
            "os": None,
            "headSha": run_record.payload.get("headSha"),
            "observedAt": observed_at,
            "testName": test_name,
            "fingerprintId": f"test:{normalize_component(test_name)}",
            "fingerprintComponents": _fingerprint_components(test_name=test_name),
            "allowedCauses": list(_TEST_ALLOWED_CAUSES),
            "retrySafe": False,
            "evidenceIds": sorted({evidence_id, run_record.evidence_id}),
        }
        for test_name, evidence_id in issue_test_names
    ]


def _non_test_occurrence(
    *,
    issue_number: int,
    run_id: int,
    attempt: object,
    job_id: object,
    workflow: str | None,
    lane: str | None,
    os_name: str | None,
    head_sha: object,
    observed_at: str | None,
    fingerprint_id: str,
    fingerprint_components: dict[str, object],
    allowed_causes: list[str],
    retry_safe: bool,
    evidence_ids: list[str],
) -> dict[str, Any]:
    return {
        "_sortTestName": "",
        "_sortEvidenceId": evidence_ids[-1] if evidence_ids else "",
        "issueNumber": issue_number,
        "runId": run_id,
        "attempt": attempt,
        "jobId": job_id,
        "workflow": workflow,
        "lane": lane,
        "os": os_name,
        "headSha": head_sha,
        "observedAt": observed_at,
        "testName": None,
        "fingerprintId": fingerprint_id,
        "fingerprintComponents": fingerprint_components,
        "allowedCauses": allowed_causes,
        "retrySafe": retry_safe,
        "evidenceIds": sorted(set(evidence_ids)),
    }


def _assign_occurrence_ids(
    occurrences: list[dict[str, Any]],
    prior_ordinals: Mapping[tuple[int, int, object, object], _PriorOrdinals],
) -> list[dict[str, Any]]:
    """Number occurrences within their physical (issue, run, attempt, job) group.

    Ordinals must survive incremental fact growth and shrink: an occurrence ID is
    quoted in issue comments and history, so renumbering it would silently retarget
    an existing citation. When history supplies prior ordinals for this group, each
    stable occurrence identity reclaims the ordinal it already had, and new
    identities take the lowest ordinal the group has never used. Cold runs (no
    history) number the sorted group from 1.
    """
    groups: dict[tuple[int, int, object, object], list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        groups[
            (
                int(occurrence["issueNumber"]),
                int(occurrence["runId"]),
                occurrence.get("attempt"),
                occurrence.get("jobId"),
            )
        ].append(occurrence)

    assigned: list[dict[str, Any]] = []
    for key in sorted(groups, key=lambda item: (item[0], item[1], _attempt_sort(item[2]), _none_sort(item[3]))):
        group = groups[key]
        group.sort(
            key=lambda item: (
                str(item.get("_sortTestName") or ""),
                str(item.get("_sortEvidenceId") or ""),
                str(item.get("fingerprintId") or ""),
            )
        )
        issue_number, run_id, attempt, job_id = key
        prior = prior_ordinals.get(key)
        # Every ordinal history published stays reserved even when its identity is
        # absent from this snapshot, so a re-added fact reclaims its own ID instead of
        # colliding with a neighbour's.
        used = set(prior.reserved) if prior is not None else set()
        by_identity = prior.by_identity if prior is not None else {}
        ordinals: list[int | None] = [by_identity.get(_occurrence_identity(item)) for item in group]
        next_ordinal = 1
        for index, ordinal in enumerate(ordinals):
            if ordinal is not None:
                continue
            while next_ordinal in used:
                next_ordinal += 1
            ordinals[index] = next_ordinal
            used.add(next_ordinal)
        for ordinal, occurrence in zip(ordinals, group):
            clean = {
                field: value
                for field, value in occurrence.items()
                if not field.startswith("_")
            }
            clean["occurrenceId"] = (
                f"occurrence:{issue_number}:{run_id}:"
                f"{attempt if attempt is not None else 'none'}:"
                f"{job_id if job_id is not None else 'none'}:{ordinal}"
            )
            assigned.append(clean)
    return assigned


def _occurrence_identity(occurrence: Mapping[str, Any]) -> tuple[str, str]:
    """Identify an occurrence within its physical group, independent of ordinal.

    An exact test name is the strongest identity; without one the fingerprint is
    all the evidence supports.
    """
    test_name = occurrence.get("testName")
    if isinstance(test_name, str) and test_name:
        return ("test", test_name)
    return ("fingerprint", str(occurrence.get("fingerprintId") or ""))


class _PriorOrdinals(NamedTuple):
    """Ordinals history already published for one physical occurrence group.

    ``by_identity`` is what each stable identity reclaims; ``reserved`` is every
    ordinal the group has ever handed out. The two differ whenever one identity
    holds more than one published ordinal, and only ``reserved`` is safe to check
    for collisions.
    """

    by_identity: Mapping[tuple[str, str], int]
    reserved: frozenset[int]


def _history_occurrence_ordinals(
    history: Mapping[str, Any] | None,
) -> dict[tuple[int, int, object, object], _PriorOrdinals]:
    if history is None:
        return {}
    history_mapping = _require_mapping(history, "history")
    history_occurrences = history_mapping.get("occurrences", [])
    if not isinstance(history_occurrences, list):
        raise ValueError("history.occurrences must be an array.")
    by_identity: dict[tuple[int, int, object, object], dict[tuple[str, str], int]] = defaultdict(dict)
    reserved: dict[tuple[int, int, object, object], set[int]] = defaultdict(set)
    for index, occurrence in enumerate(history_occurrences):
        if not isinstance(occurrence, Mapping):
            raise ValueError(f"history.occurrences[{index}] must be an object.")
        occurrence_id = _validate_history_occurrence(occurrence, index)
        parsed = _parse_occurrence_id(occurrence_id, f"history.occurrences[{index}].occurrenceId")
        key = (
            int(parsed["issueNumber"]),
            int(parsed["runId"]),
            parsed["attempt"],
            parsed["jobId"],
        )
        identity = _occurrence_identity(occurrence)
        ordinal = int(parsed["ordinal"])
        # Out-of-window entries count too: their ordinals are already published, so
        # handing them to a different identity would retarget an existing citation.
        # Every ordinal stays reserved, not just the lowest one per identity - an
        # earlier cycle can have numbered the same identity differently, and both IDs
        # are quoted in comments.
        reserved[key].add(ordinal)
        existing = by_identity[key].get(identity)
        by_identity[key][identity] = ordinal if existing is None else min(existing, ordinal)
    return {
        key: _PriorOrdinals(by_identity=by_identity[key], reserved=frozenset(ordinals))
        for key, ordinals in reserved.items()
    }


def _build_coverage(
    records: Mapping[str, _EvidenceRecord],
    runs_by_id: Mapping[int, _EvidenceRecord],
    logs_by_key: Mapping[tuple[int, object, object], list[_EvidenceRecord]],
    *,
    window_cutoff: datetime,
    collected_at: datetime,
) -> list[dict[str, Any]]:
    coverage: list[dict[str, Any]] = []
    for job in sorted(
        (record for record in records.values() if record.availability == "available" and record.is_job),
        key=lambda record: record.evidence_id,
    ):
        if not _is_successful_job_execution(job.payload):
            continue
        run_id = _require_positive_int(job.payload.get("runId"), f"{job.evidence_id} payload.runId")
        run_record = runs_by_id[run_id]
        attempt = job.payload.get("attempt")
        job_id = job.payload.get("jobId")
        workflow = _workflow_name(run_record)
        lane, os_name = _lane_and_os(job.payload)
        subject_base = _coverage_subject_base(workflow, lane, os_name)
        observed_at = _observed_at(job.payload, run_record.payload)
        if not _is_timestamp_in_window(observed_at, f"{job.evidence_id} observedAt", window_cutoff, collected_at):
            continue
        evidence_ids = [run_record.evidence_id, job.evidence_id]
        coverage.append(
            {
                "coverageId": _coverage_id(run_id, attempt, job_id),
                "subjectKind": "lane",
                "subjectId": subject_base,
                "runId": run_id,
                "attempt": attempt,
                "headSha": run_record.payload.get("headSha"),
                "observedAt": observed_at,
                "status": "succeeded",
                "independentRecoveryEligible": _independent_recovery_eligible(attempt),
                "evidenceIds": evidence_ids,
            }
        )
        passed_tests = _passed_tests(logs_by_key.get((run_id, attempt, job_id), []))
        for ordinal, (test_name, evidence_id) in enumerate(passed_tests, start=1):
            coverage.append(
                {
                    "coverageId": f"{_coverage_id(run_id, attempt, job_id)}:test:{_encode_exact_component(test_name)}",
                    "subjectKind": "test",
                    # The subject is the test's identity, normalized exactly like a
                    # `test:<normalized>` fingerprint so coverage and failure records
                    # for the same test agree; the coverage ID keeps the raw name,
                    # percent-encoded, so two raw spellings never collide.
                    "subjectId": f"{subject_base}:test:{normalize_component(test_name)}",
                    "runId": run_id,
                    "attempt": attempt,
                    "headSha": run_record.payload.get("headSha"),
                    "observedAt": observed_at,
                    "status": "succeeded",
                    "independentRecoveryEligible": _independent_recovery_eligible(attempt),
                    "evidenceIds": [*evidence_ids, evidence_id],
                }
            )
    return coverage


def _build_fingerprint_summaries(
    current_occurrences: list[dict[str, Any]],
    history: Mapping[str, Any] | None,
    records: Mapping[str, _EvidenceRecord],
    runs_by_id: Mapping[int, _EvidenceRecord],
    *,
    window_cutoff: datetime,
    collected_at: datetime,
) -> list[dict[str, Any]]:
    merged_by_id: dict[str, Mapping[str, Any]] = {}
    for occurrence in current_occurrences:
        merged_by_id[str(occurrence["occurrenceId"])] = occurrence

    if history is not None:
        history_mapping = _require_mapping(history, "history")
        history_occurrences = history_mapping.get("occurrences", [])
        if not isinstance(history_occurrences, list):
            raise ValueError("history.occurrences must be an array.")
        for index, occurrence in enumerate(history_occurrences):
            if not isinstance(occurrence, Mapping):
                raise ValueError(f"history.occurrences[{index}] must be an object.")
            occurrence_id = _validate_history_occurrence(occurrence, index)
            existing = merged_by_id.get(occurrence_id)
            if existing is None:
                merged_by_id[occurrence_id] = occurrence
                continue
            _require_compatible_occurrence(occurrence_id, existing, occurrence, index)

    by_fingerprint: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for occurrence in merged_by_id.values():
        if not _is_occurrence_in_window(occurrence, window_cutoff, collected_at):
            continue
        by_fingerprint[str(occurrence["fingerprintId"])].append(occurrence)

    observed_lane_runs = _lane_population(
        records,
        runs_by_id,
        history,
        window_cutoff=window_cutoff,
        collected_at=collected_at,
    )
    summaries: list[dict[str, Any]] = []
    for fingerprint_id in sorted(by_fingerprint):
        occurrences = by_fingerprint[fingerprint_id]
        occurrence_ids = sorted((str(item["occurrenceId"]) for item in occurrences), key=_occurrence_id_sort_key)
        issue_numbers = sorted(
            {
                int(item["issueNumber"])
                for item in occurrences
                if isinstance(item.get("issueNumber"), int) and not isinstance(item.get("issueNumber"), bool)
            }
        )
        run_ids = sorted(
            {
                int(item["runId"])
                for item in occurrences
                if isinstance(item.get("runId"), int) and not isinstance(item.get("runId"), bool)
            }
        )
        observed_times = sorted(
            parse_aware_iso8601(item["observedAt"], "occurrence observedAt")
            for item in occurrences
            if isinstance(item.get("observedAt"), str)
        )
        matching_run_ids: set[int] = set(run_ids)
        # A rate is only a probability when the population it divides by is known.
        # Every occurrence must resolve to a concrete lane, and every one of those
        # lanes must show at least one non-failing run inside the window; otherwise
        # all we have is a pile of failures and no evidence of how often the lane ran.
        rate_evidence_complete = True
        for item in occurrences:
            lane_identity = _occurrence_lane_identity(item)
            if lane_identity is None:
                rate_evidence_complete = False
                continue
            population = observed_lane_runs.get(lane_identity)
            if population is None or not population.positive_run_ids:
                rate_evidence_complete = False
            if population is not None:
                matching_run_ids.update(population.run_ids)
        summary: dict[str, Any] = {
            "fingerprintId": fingerprint_id,
            "occurrenceIds": occurrence_ids,
            "issueNumbers": issue_numbers,
            "distinctRunIds": run_ids,
            "firstSeenAt": format_utc_z(observed_times[0]) if observed_times else None,
            "lastSeenAt": format_utc_z(observed_times[-1]) if observed_times else None,
            **_failure_rate_fields(
                fingerprint_id,
                run_ids,
                matching_run_ids,
                rate_evidence_complete=rate_evidence_complete,
            ),
        }
        summaries.append(summary)
    return summaries


def _failure_rate_fields(
    fingerprint_id: str,
    failure_run_ids: list[int],
    matching_run_ids: set[int],
    *,
    rate_evidence_complete: bool,
) -> dict[str, Any]:
    """Express failure rate in run units on both sides of the ratio.

    The numerator counts distinct runs that failed with this fingerprint, not
    occurrences: one run failing the same test in two jobs is one failing run. The
    denominator counts distinct runs observed on the same lanes within the window,
    failed and successful alike.

    The counts are always reported because they are known facts. ``failureRate`` is
    only reported when the denominator is a known-complete population for the
    window; a summary built from failures alone would otherwise claim a 100%
    probability that the evidence never supported.
    """
    distinct_failure_run_count = len(failure_run_ids)
    denominator = len(matching_run_ids)
    if denominator < distinct_failure_run_count:
        raise ValueError(
            f"{fingerprint_id} observed {distinct_failure_run_count} distinct failure runs but only "
            f"{denominator} observed matching-lane runs."
        )
    failure_rate: float | None = None
    if rate_evidence_complete:
        # Completeness requires every contributing occurrence's lane to have observed
        # a positive run, so the population is never empty here.
        failure_rate = distinct_failure_run_count / denominator
        if failure_rate > 1:
            raise ValueError(f"{fingerprint_id} failure rate {failure_rate} exceeds 1.")
    return {
        "distinctFailureRunCount": distinct_failure_run_count,
        "observedMatchingLaneRunDenominator": denominator,
        "rateEvidenceComplete": rate_evidence_complete,
        "failureRate": failure_rate,
    }


def _require_compatible_occurrence(
    occurrence_id: str,
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
    index: int,
) -> None:
    for field in _OCCURRENCE_IDENTITY_FIELDS:
        if field not in existing or field not in candidate:
            continue
        if existing[field] != candidate[field]:
            raise ValueError(
                f"history.occurrences[{index}] reuses occurrenceId {occurrence_id} with a different "
                f"{field}: {existing[field]!r} versus {candidate[field]!r}."
            )


def _validate_history_occurrence(occurrence: Mapping[str, Any], index: int) -> str:
    occurrence_id = _require_string(
        occurrence.get("occurrenceId"),
        f"history.occurrences[{index}].occurrenceId",
    )
    _require_string(occurrence.get("fingerprintId"), f"history.occurrences[{index}].fingerprintId")
    # testName drives _occurrence_identity, so a non-string here would silently push
    # an occurrence into the fingerprint-keyed identity bucket and let it claim an
    # ordinal that a differently named occurrence already published.
    test_name = occurrence.get("testName")
    if test_name is not None:
        _require_string(test_name, f"history.occurrences[{index}].testName")
    issue_number = _require_positive_int(occurrence.get("issueNumber"), f"history.occurrences[{index}].issueNumber")
    run_id = _require_positive_int(occurrence.get("runId"), f"history.occurrences[{index}].runId")
    attempt = _require_optional_positive_int(occurrence.get("attempt"), f"history.occurrences[{index}].attempt")
    job_id = _require_optional_positive_int(occurrence.get("jobId"), f"history.occurrences[{index}].jobId")
    parse_aware_iso8601(occurrence.get("observedAt"), f"history.occurrences[{index}].observedAt")
    parsed = _parse_occurrence_id(occurrence_id, f"history.occurrences[{index}].occurrenceId")
    _require_identity_match(f"history.occurrences[{index}].occurrenceId", "issueNumber", parsed["issueNumber"], issue_number)
    _require_identity_match(f"history.occurrences[{index}].occurrenceId", "runId", parsed["runId"], run_id)
    _require_identity_match(f"history.occurrences[{index}].occurrenceId", "attempt", parsed["attempt"], attempt)
    _require_identity_match(f"history.occurrences[{index}].occurrenceId", "jobId", parsed["jobId"], job_id)
    return occurrence_id


class _LanePopulation:
    """Runs observed on one normalized lane inside the window.

    ``run_ids`` is the population the failure rate divides by; ``positive_run_ids``
    is the subset that did not fail, which is the only proof that the lane ran at
    all when it was not failing.
    """

    def __init__(self) -> None:
        self.run_ids: set[int] = set()
        self.positive_run_ids: set[int] = set()


def _occurrence_lane_identity(occurrence: Mapping[str, Any]) -> str | None:
    """Normalized ``workflow:lane:os`` key, or None when any dimension is unknown."""
    values = []
    for field in ("workflow", "lane", "os"):
        value = occurrence.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value)
    return _coverage_subject_base(*values)


def _lane_population(
    records: Mapping[str, _EvidenceRecord],
    runs_by_id: Mapping[int, _EvidenceRecord],
    history: Mapping[str, Any] | None,
    *,
    window_cutoff: datetime,
    collected_at: datetime,
) -> dict[str, _LanePopulation]:
    observed: dict[str, _LanePopulation] = defaultdict(_LanePopulation)
    for job in records.values():
        if job.availability != "available" or not job.is_job:
            continue
        run_id = _require_positive_int(job.payload.get("runId"), f"{job.evidence_id} payload.runId")
        run_record = runs_by_id[run_id]
        observed_at = _observed_at(job.payload, run_record.payload)
        if not _is_timestamp_in_window(observed_at, f"{job.evidence_id} observedAt", window_cutoff, collected_at):
            continue
        lane, os_name = _lane_and_os(job.payload)
        population = observed[_coverage_subject_base(_workflow_name(run_record), lane, os_name)]
        population.run_ids.add(run_id)
        if _is_successful_job_execution(job.payload):
            population.positive_run_ids.add(run_id)

    for run_record in runs_by_id.values():
        for run_id, subject_id, succeeded in _lane_dimensioned_recent_runs(
            run_record,
            window_cutoff=window_cutoff,
            collected_at=collected_at,
        ):
            population = observed[subject_id]
            population.run_ids.add(run_id)
            if succeeded:
                population.positive_run_ids.add(run_id)

    for subject_id, run_id in _history_lane_coverage(history, window_cutoff=window_cutoff, collected_at=collected_at):
        population = observed[subject_id]
        population.run_ids.add(run_id)
        population.positive_run_ids.add(run_id)
    return dict(observed)


def _lane_dimensioned_recent_runs(
    run_record: _EvidenceRecord,
    *,
    window_cutoff: datetime,
    collected_at: datetime,
) -> list[tuple[int, str, bool]]:
    """Read ``recentHistory`` entries that carry their own execution dimensions.

    The collector's ``recentHistory`` is run-level only - ``runId``, ``attempt``,
    ``event``, ``branch``, ``headSha``, ``conclusion``, ``createdAt``, ``url`` - and
    ``recentHistoryTotalCount`` is a workflow-wide count. Neither says anything about
    which lane or OS ran, so counting them as lane coverage would inflate the
    denominator with runs that never executed the failing lane. Only entries that
    explicitly name workflow, lane, and OS are usable.

    The returned flag is ``True`` only for an explicit ``success`` conclusion, for the
    same reason ``_is_successful_job_execution`` demands one: a cancelled, skipped, or
    still-running historical run is not proof the lane completed without this failure.
    """
    entries = run_record.payload.get("recentHistory")
    if not isinstance(entries, list):
        return []
    usable: list[tuple[int, str, bool]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        dimensions = [entry.get(field) for field in ("workflow", "lane", "os")]
        if not all(isinstance(value, str) and value.strip() for value in dimensions):
            continue
        run_id = entry.get("runId")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            continue
        observed_at = entry.get("createdAt") if isinstance(entry.get("createdAt"), str) else entry.get("observedAt")
        if not _is_timestamp_in_window(
            observed_at,
            f"{run_record.evidence_id} recentHistory observedAt",
            window_cutoff,
            collected_at,
        ):
            continue
        succeeded = str(entry.get("conclusion", "")).lower() == "success"
        usable.append((run_id, _coverage_subject_base(*dimensions), succeeded))
    return usable


def _history_lane_coverage(
    history: Mapping[str, Any] | None,
    *,
    window_cutoff: datetime,
    collected_at: datetime,
) -> list[tuple[str, int]]:
    """Lane coverage carried forward from a previous cycle's observation output.

    Only succeeded lane coverage counts: it is the record that a lane ran without
    this failure, which is exactly the population a failure rate needs.
    """
    if history is None:
        return []
    coverage = _require_mapping(history, "history").get("coverage", [])
    if not isinstance(coverage, list):
        raise ValueError("history.coverage must be an array.")
    resolved: list[tuple[str, int]] = []
    for index, record in enumerate(coverage):
        if not isinstance(record, Mapping):
            raise ValueError(f"history.coverage[{index}] must be an object.")
        subject_id = _require_string(record.get("subjectId"), f"history.coverage[{index}].subjectId")
        subject_kind = _require_string(record.get("subjectKind"), f"history.coverage[{index}].subjectKind")
        status = _require_string(record.get("status"), f"history.coverage[{index}].status")
        run_id = _require_positive_int(record.get("runId"), f"history.coverage[{index}].runId")
        parse_aware_iso8601(record.get("observedAt"), f"history.coverage[{index}].observedAt")
        if subject_kind != "lane" or status != "succeeded":
            continue
        if not _is_timestamp_in_window(
            record.get("observedAt"),
            f"history.coverage[{index}].observedAt",
            window_cutoff,
            collected_at,
        ):
            continue
        resolved.append((subject_id, run_id))
    return resolved


def _require_factual_history(history: Mapping[str, Any] | None) -> None:
    """Reject decision records offered as factual input.

    Observations are built from what was observed. Causes and proposals are
    conclusions drawn from observations, so feeding them back in would let a prior
    interpretation masquerade as evidence.
    """
    if history is None:
        return
    for field in sorted(_require_mapping(history, "history")):
        if field not in _FACTUAL_HISTORY_FIELDS:
            raise ValueError(
                f"history.{field} is not factual input; observations accept only "
                f"{', '.join(sorted(_FACTUAL_HISTORY_FIELDS))}."
            )


def _collect_issue_test_names(
    issue_number: int,
    records: Mapping[str, _EvidenceRecord],
) -> list[tuple[str, str]]:
    issue = records[f"issue:{issue_number}"]
    if issue.availability != "available":
        return []
    return _fact_values(issue.evidence_id, issue.payload, "testName")


def _collect_job_test_names(
    issue_test_names: list[tuple[str, str]],
    job: _EvidenceRecord,
    logs: list[_EvidenceRecord],
) -> list[tuple[str, str]]:
    # Provenance order matters: the first evidence ID seen for a name is the one
    # cited, so job-scoped evidence wins over log-scoped, which wins over the
    # issue-level facts the caller already gated behind a deterministic anchor.
    #
    # Check-run annotations are deliberately not a source here. The collector copies
    # GitHub's annotation fields verbatim and never derives facts from them, and real
    # annotations carry no exact test name to derive one from: on microsoft/aspire
    # they arrive with an empty `title`, a `path` of ".github", empty `raw_details`,
    # and a prose `message` such as "Process completed with exit code 1." Parsing that
    # into a test name would manufacture attribution the evidence does not support.
    # Annotations stay useful as bundled evidence a human reads, not as facts.
    values = [*_fact_values(job.evidence_id, job.payload, "testName")]
    for log in logs:
        values.extend(_fact_values(log.evidence_id, log.payload, "testName"))
    values.extend(issue_test_names)

    by_name: dict[str, str] = {}
    for test_name, evidence_id in values:
        by_name.setdefault(test_name, evidence_id)
    return [(test_name, by_name[test_name]) for test_name in sorted(by_name)]


def _fact_values(evidence_id: str, payload: Mapping[str, Any], field_name: str) -> list[tuple[str, str]]:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return []
    values: list[tuple[str, str]] = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, Mapping):
            raise ValueError(f"{evidence_id} payload.facts[{index}] must be an object.")
        field = fact.get("field")
        if field != field_name:
            continue
        raw = fact.get("raw")
        normalized = fact.get("normalized")
        value = raw if isinstance(raw, str) and raw else normalized
        if not isinstance(value, str) or not value:
            raise ValueError(f"{evidence_id} payload.facts[{index}] {field_name} must be nonempty.")
        values.append((value, evidence_id))
    return values


def _diagnostic_lines(text: str) -> list[str]:
    """Keep only raw log lines that actually report a failure.

    Test frameworks print expected/actual values verbatim, so an assertion on an
    HTTP status or a compiler diagnostic string would otherwise be mistaken for
    real infrastructure or build evidence.
    """
    return [
        line
        for line in text.splitlines()
        if not _ASSERTION_LINE_RE.match(line) and _DIAGNOSTIC_LINE_RE.search(line) is not None
    ]


def _network_pattern(
    job_payload: Mapping[str, Any],
    logs: list[_EvidenceRecord],
) -> tuple[str, str] | None:
    for log in logs:
        for field_name in ("excerpt", "errorMessage"):
            value = log.payload.get(field_name)
            if not isinstance(value, str):
                continue
            for line in _diagnostic_lines(value):
                match = _HTTP_STATUS_RE.search(line)
                if match is not None and int(match.group("code")) in _RETRY_RELEVANT_HTTP_STATUSES:
                    return (f"http-{match.group('code')}", _failed_step_name(job_payload))
    return None


def _build_break_code(
    job: _EvidenceRecord,
    logs: list[_EvidenceRecord],
) -> str | None:
    for evidence_id, payload in [(job.evidence_id, job.payload), *((log.evidence_id, log.payload) for log in logs)]:
        for test in _fact_values(evidence_id, payload, "errorCode"):
            if _BUILD_BREAK_CODE_RE.fullmatch(test[0]):
                return test[0]
        for field_name in ("excerpt", "errorMessage"):
            value = payload.get(field_name)
            if not isinstance(value, str):
                continue
            for line in _diagnostic_lines(value):
                match = _BUILD_BREAK_DIAGNOSTIC_RE.search(line)
                if match is not None:
                    return match.group("code")
    return None


def _has_repo_config_evidence(issue_number: int, records: Mapping[str, _EvidenceRecord]) -> bool:
    """Report whether the issue cites CI/build configuration under a trusted kind.

    Only ``source-path`` evidence counts, and only when its path sits under a
    configuration prefix. Trusting any payload key named ``path`` would let an
    unrelated record (a log excerpt naming a workflow file, say) widen the allowed
    cause set. There is deliberately no ``repoConfig`` fact branch: signals.py only
    ever emits causeId, sourceRun, testName, job, triggeringPullRequest, failureType,
    exceptionType, and errorCode, so such a branch could never fire.
    """
    for record in records.values():
        if record.availability != "available" or not is_scoped_to_issue(record.evidence_id, record.payload, issue_number):
            continue
        if record.kind == "source-path" and record.payload.get("exists") is not False:
            path = record.payload.get("path")
            if isinstance(path, str) and path.startswith(_REPO_CONFIG_PATH_PREFIXES):
                return True
    return False


def _passed_tests(logs: list[_EvidenceRecord]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for log in logs:
        excerpt = log.payload.get("excerpt")
        if not isinstance(excerpt, str):
            continue
        for match in _PASSED_TEST_RE.finditer(excerpt):
            values.append((match.group("test").strip(), log.evidence_id))
    by_name: dict[str, str] = {}
    for test_name, evidence_id in sorted(values, key=lambda item: (item[0], item[1])):
        if not test_name:
            continue
        by_name.setdefault(test_name, evidence_id)
    return [(test_name, by_name[test_name]) for test_name in sorted(by_name)]


def _evidence_ids_for_occurrence(
    run_record: _EvidenceRecord | None,
    job: _EvidenceRecord,
    logs: list[_EvidenceRecord],
) -> list[str]:
    evidence_ids = [job.evidence_id, *(log.evidence_id for log in logs)]
    if run_record is not None:
        evidence_ids.append(run_record.evidence_id)
    return sorted(set(evidence_ids))


def _workflow_name(run_record: _EvidenceRecord | None) -> str | None:
    if run_record is None:
        return None
    for field_name in ("workflow", "workflowName", "name"):
        value = run_record.payload.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _lane_and_os(job_payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Resolve the lane and OS a job ran on, preferring structured payload fields.

    Collectors that emit ``lane``, ``os``, and ``runnerLabels`` give exact values.
    The job-name fallback exists only for evidence collected before those fields
    were introduced, and deliberately refuses to read arbitrary matrix text as an
    OS: ``Tests / Hosting (net10.0, Debug)`` is a lane, not an operating system.
    """
    structured_lane = job_payload.get("lane")
    structured_os = job_payload.get("os")
    fallback_lane, fallback_os = _lane_and_os_from_job_name(job_payload.get("name"))

    lane = structured_lane if isinstance(structured_lane, str) and structured_lane else fallback_lane
    if isinstance(structured_os, str) and structured_os:
        os_name: str | None = structured_os
    else:
        os_name = _os_from_runner_labels(job_payload.get("runnerLabels")) or fallback_os
    return (lane, os_name)


def _lane_and_os_from_job_name(name: object) -> tuple[str | None, str | None]:
    if not isinstance(name, str) or not name:
        return (None, None)
    runner_match = re.search(r"\((?P<runner>[^()]+)\)\s*$", name)
    os_name: str | None = None
    without_runner = name
    if runner_match is not None:
        candidate = runner_match.group("runner").strip()
        if _RUNNER_OS_RE.fullmatch(candidate):
            os_name = candidate
            without_runner = name[: runner_match.start()].strip()
    parts = [part.strip() for part in without_runner.split("/") if part.strip()]
    lane = parts[-1] if len(parts) > 1 else without_runner.strip()
    return (lane or None, os_name)


def _os_from_runner_labels(runner_labels: object) -> str | None:
    if not isinstance(runner_labels, list):
        return None
    for label in runner_labels:
        if isinstance(label, str) and _RUNNER_OS_RE.fullmatch(label.strip()):
            return label.strip()
    return None


def _failed_step_name(job_payload: Mapping[str, Any]) -> str:
    if "failingStep" in job_payload:
        failing_step = job_payload.get("failingStep")
        return failing_step if isinstance(failing_step, str) and failing_step else "none"
    steps = job_payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            if str(step.get("conclusion", "")).lower() in _FAILED_JOB_CONCLUSIONS:
                name = step.get("name")
                if isinstance(name, str) and name:
                    return name
    return "none"


def _observed_at(job_payload: Mapping[str, Any], run_payload: Mapping[str, Any]) -> str | None:
    for field_name, payload in (
        ("completedAt", job_payload),
        ("runStartedAt", run_payload),
        ("createdAt", run_payload),
        ("updatedAt", run_payload),
    ):
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def is_scoped_to_issue(
    evidence_id: str,
    record_or_payload: Mapping[str, Any],
    issue_number: int,
) -> bool:
    """Return whether an evidence record belongs to the given issue.

    Accepts either a full evidence record (a mapping carrying ``payload``) or a bare
    payload, because observations index payloads while lifecycle carries whole records;
    a single rule keeps their scoping identical. Public because lifecycle consumes it;
    see the module docstring for the shared surface.
    """
    if evidence_id == f"issue:{issue_number}":
        return True
    if "payload" in record_or_payload:
        payload = record_or_payload.get("payload")
        if not isinstance(payload, Mapping):
            return False
    else:
        payload = record_or_payload
    if payload.get("sourceIssueNumber") == issue_number:
        return True
    referenced_by = payload.get("referencedBy")
    return isinstance(referenced_by, list) and any(
        isinstance(reference, Mapping)
        and reference.get("sourceIssueNumber") == issue_number
        for reference in referenced_by
    )


def _encode_exact_component(value: str) -> str:
    return quote(value, safe="")


def _coverage_id(run_id: int, attempt: object, job_id: object) -> str:
    return (
        f"coverage:run:{run_id}:"
        f"attempt:{attempt if attempt is not None else 'none'}:"
        f"job:{job_id if job_id is not None else 'none'}"
    )


def _coverage_subject_base(workflow: object, lane: object, os_name: object) -> str:
    return f"{normalize_component(workflow)}:{normalize_component(lane)}:{normalize_component(os_name)}"


def _fingerprint_components(
    *,
    pattern_id: str | None = None,
    runner_os: str | None = None,
    step: str | None = None,
    error_code: str | None = None,
    job: str | None = None,
    test_name: str | None = None,
) -> dict[str, object]:
    return {
        "patternId": pattern_id,
        "runnerOS": runner_os,
        "step": step,
        "errorCode": error_code,
        "job": job,
        "testName": test_name,
    }


def _parse_workflow_evidence_id(evidence_id: str) -> dict[str, Any] | None:
    match = _WORKFLOW_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if match is None:
        return None
    return {
        "runId": int(match.group("run_id")),
        "attempt": _parse_optional_identity_component(match.group("attempt")),
        "jobId": _parse_optional_identity_component(match.group("job_id")),
        "isLog": match.group("log") is not None,
    }


def _parse_issue_evidence_id(evidence_id: str) -> dict[str, Any] | None:
    match = _ISSUE_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if match is None:
        return None
    return {"issueNumber": int(match.group("issue_number"))}


def _parse_occurrence_id(occurrence_id: str, name: str) -> dict[str, Any]:
    match = _OCCURRENCE_ID_RE.fullmatch(occurrence_id)
    if match is None:
        raise ValueError(
            f"{name} must match occurrence:<issue>:<run>:<attempt>:<job-or-none>:<ordinal> "
            "with positive numeric issue, run, and ordinal fields, and positive numeric or none attempt."
        )
    return {
        "issueNumber": int(match.group("issue_number")),
        "runId": int(match.group("run_id")),
        "attempt": _parse_optional_identity_component(match.group("attempt")),
        "jobId": _parse_optional_identity_component(match.group("job_id")),
        "ordinal": int(match.group("ordinal")),
    }


def _parse_optional_identity_component(value: str | None) -> int | None:
    if value is None or value == "none":
        return None
    return int(value)


def _require_identity_match(name: str, field_name: str, expected: object, actual: object) -> None:
    if expected != actual:
        raise ValueError(f"{name} {field_name} mismatch: evidence ID has {expected}, payload has {actual}.")


def _is_occurrence_in_window(occurrence: Mapping[str, Any], window_cutoff: datetime, collected_at: datetime) -> bool:
    return _is_timestamp_in_window(occurrence.get("observedAt"), "occurrence observedAt", window_cutoff, collected_at)


def _is_timestamp_in_window(value: object, name: str, window_cutoff: datetime, collected_at: datetime) -> bool:
    if not isinstance(value, str) or not value:
        return False
    observed_at = parse_aware_iso8601(value, name)
    return window_cutoff <= observed_at <= collected_at


def _occurrence_id_sort_key(occurrence_id: str) -> tuple[object, ...]:
    parts = occurrence_id.split(":")
    if len(parts) == 6 and parts[0] == "occurrence":
        issue_number = _parse_sort_int(parts[1])
        run_id = _parse_sort_int(parts[2])
        attempt = None if parts[3] == "none" else _parse_sort_int(parts[3])
        job_id = None if parts[4] == "none" else _parse_sort_int(parts[4])
        ordinal = _parse_sort_int(parts[5])
        if (
            issue_number is not None
            and run_id is not None
            and ordinal is not None
            and (attempt is not None or parts[3] == "none")
            and (job_id is not None or parts[4] == "none")
        ):
            return (
                0,
                issue_number,
                run_id,
                _attempt_sort(attempt),
                _none_sort(job_id),
                ordinal,
            )
    return (1, occurrence_id)


def _parse_sort_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    return dict(value)


def _require_int_list(value: object, name: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array.")
    result: list[int] = []
    for index, item in enumerate(value):
        result.append(_require_positive_int(item, f"{name}[{index}]"))
    return sorted(result)


def _require_positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _require_optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, name)


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _attempt_sort(value: object) -> tuple[int, object]:
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value)
    return (1, str(value))


def _none_sort(value: object) -> tuple[int, object]:
    if value is None:
        return (1, "")
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value)
    return (0, str(value))
