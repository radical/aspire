"""Conservative cause identity derived from frozen logs, never judgment prose."""

from dataclasses import dataclass
import json
import re

from ci_shepherd.observations import _GENERIC_FAILURE_RE, normalize_log_text

from .models import JobObservation, RunObservation


GROUPING_VERSION = "cause-group-v1"
_TEST = re.compile(
    r"^(?:Failed|failed)\s+(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}(?:\(.*\))?)"
    r"(?:\s+\[[^\]]+\])?$"
)
_COMPILER = re.compile(r"^.+\.(?:cs|fs|vb|csproj|fsproj|vbproj)(?:\(\d+(?:,\d+)*\))?: "
                       r"error (?:CS|FS|BC|MSB|NU)\d+: .+")
_HTTP = re.compile(r"^(?:HTTP(?:/[12](?:\.\d)?)? |error )([45]\d\d)\b.*https?://\S+")
_GENERIC = re.compile(
    r"\b(?:timeout|timed out|TimeoutException|TaskCanceledException|OperationCanceledException)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CauseSignature:
    kind: str
    subject: str
    diagnostic: str

    @property
    def identity(self) -> str:
        # Exact canonical text avoids hash collisions and retains resource names.
        return json.dumps(
            [GROUPING_VERSION, self.kind, self.subject, self.diagnostic],
            ensure_ascii=True, separators=(",", ":"),
        )


def group_id(run: RunObservation, signature: CauseSignature | None, leaf_key: str) -> str:
    return GROUPING_VERSION + ":" + json.dumps(
        [run.key.repository, run.key.branch, run.key.workflow_id, run.workflow_path,
         signature.identity if signature else ["singleton", leaf_key]],
        ensure_ascii=True, separators=(",", ":"),
    )


def derive_cause(
    run: RunObservation, job: JobObservation, *, failed_steps: tuple[str, ...],
) -> CauseSignature | None:
    if (
        not run.jobs_complete or job not in run.jobs
        or job.status != "completed" or job.conclusion != "failure"
        or not job.log_excerpt
    ):
        return None
    # Reuse transport timestamp normalization, but do not scrub paths, URLs,
    # numbers, case or payload whitespace: '/repo/mock  executable.sh' is not
    # '/repo/mock executable.sh', including when passed as a test argument.
    lines = [
        line.removeprefix("##[error]").strip()
        for line in normalize_log_text(job.log_excerpt).splitlines()
    ]
    # MTP/xUnit console: "Failed Namespace.Type.Method [12 ms]", followed by
    # "Error Message:" and a diagnostic block ending at "Stack Trace:".
    tests = [(index, match) for index, line in enumerate(lines)
             if (match := _TEST.fullmatch(line))]
    if tests:
        if len(tests) != 1:
            return None
        index, match = tests[0]
        remaining = lines[index + 1:]
        if not remaining or remaining[0] != "Error Message:":
            return None
        block = []
        for line in remaining[1:]:
            if not line or line.startswith(("Stack Trace:", "at ", "Test Run", "Total tests:")):
                break
            block.append(line)
        diagnostic = " ".join(block)
        primary = re.sub(r"^(?:\w+\.)*\w*(?:Exception|Error):\s*", "", diagnostic)
        if not diagnostic or _GENERIC.search(diagnostic) or _GENERIC_FAILURE_RE.fullmatch(primary):
            return None
        return CauseSignature("test", match["name"], diagnostic)
    diagnostics = []
    for line in lines:
        if _COMPILER.fullmatch(line):
            diagnostics.append(CauseSignature("tool", "dotnet-compiler", line))
        elif _HTTP.fullmatch(line) and len(failed_steps) == 1 and failed_steps[0].strip():
            diagnostics.append(CauseSignature("step", failed_steps[0], line))
    unique = set(diagnostics)
    return next(iter(unique)) if len(unique) == 1 else None
