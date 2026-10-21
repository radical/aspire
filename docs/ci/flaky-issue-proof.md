# Issue-driven flaky-test lifecycle proof

These workflows are **fork-only experiments**, not an upstream deployment.
They run only in `radical/aspire` and do not replace the production reproduction
worker contribution.

## Enrollment

An owner-authored open issue requires both `flaky-agent-proof` and assignment
to `radical`; whichever enrollment condition is added last starts it. The issue must
contain exactly one JSON block:

```json
{
  "version": 1,
  "target": "hosting-json",
  "max_rounds": 2,
  "max_fix_attempts": 2,
  "max_executions": 12,
  "retry_probe": false,
  "resume_delay_seconds": 60,
  "cancel_probe": false
}
```

The trusted catalog in `.github/scripts/flaky-issue-proof.py` permits two frozen
targets: a passing Hosting JSON test and an induced OpenAI readiness failure.
An arbitrary issue cannot select shell fragments, paths, package feeds or
unreviewed source revisions. Changing the issue body after enrollment blocks
the investigation instead of changing its authority.

## Execution and state

```text
issue event
  -> trusted controller + per-issue Git checkpoint
  -> bounded fresh agent + immutable proposal artifact
  -> read-only test workers or scoped candidate publisher
  -> explicit completion callback + actual evidence validation
  -> optional persisted timed pause
  -> fresh agent
  -> draft PR and issue findings, or issue findings without a PR
```

Each issue has a separate controller-state branch and agent-memory branch.
Controller writes use immutable parent revisions and nonforced Git ref updates;
agent memory is a proposal transport, not the authority for execution budgets.
Deferred safe outputs can dispatch before memory persistence finishes, so the
controller waits for the entire parent run before reading its proposal artifact.
The trusted controller also watches its owned model run, so a failed model that
cannot emit a callback still ends with findings instead of a stranded issue.
Server-returned dispatch IDs identify owned workers; the newest run is not
assumed to belong to the investigation.

Limits are three baseline rounds, three candidate attempts, twenty requested
test executions and ten agent phases, or the issue's lower limits. Candidate
verification requires three Linux workers with three executions each.
No long-lived model session waits for CI. Workflow timeouts bound each phase.

The controlled target can set `retry_probe` to publish a deliberately ineffective
comment-only candidate first. Its actual verification must retain the matching
failure before a fresh agent can author a real repair. This is an explicit
control-plane test, not evidence that a naturally proposed fix failed.

The passing target can set `cancel_probe` to create a disposable sleeping worker.
A fresh agent requests cancellation of that exact owned run. It must actually
finish cancelled before the controller reports cancellation. The probe is not
counted as a test execution and cannot cancel unrelated CI.

## Terminal outcomes

The controller checks original target TRX, positive execution counts, original
output, job logs, checkout receipts, source scope and exact published identity.
Neither a green workflow nor an agent's success statement can authorize a PR.
The final agent must acknowledge the actual published source blob.

Verified induced repairs produce a draft PR against the frozen fixture branch
and a terminal issue comment. Other outcomes are `not-reproduced`, `blocked`,
`verification-failed` or `cancelled`, with an issue comment listing counts,
run links and limitations. Issues remain open and quarantine is preserved.
PR descriptions use the repository template. Generated publication previews
are printed and retained before the API write. Posted text is identified as
automated.

Duplicate enrolled/terminal events do not launch a new investigation. Terminal
comments use a stable marker. After an interrupted receipted phase, an operator
can dispatch `flaky-issue-controller.yml` with `event=resume` and the issue number.
An unreceipted dispatch is an explicit blocker, never a blind automatic retry.
This proof does not provide a continuously scheduled recovery service.

## Trust boundary and validation

Only the trusted controller receives write permissions. Candidate execution
uses read-only permissions and checkouts without persisted credentials.
Candidate code can still modify local evidence on the shared worker: receipts
are diagnostics, not tamper-proof attestations. Upstream deployment requires
its own authorization, security review and publication policy.

Behavioral checks run without network access:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 .github/scripts/test-flaky-issue-proof.py
actionlint -shellcheck= -pyflakes= \
  .github/workflows/flaky-issue-controller.yml \
  .github/workflows/flaky-issue-worker.yml
```

Compile `flaky-issue-agent.md` using the repository-pinned gh-aw compiler.
The compiled runtime uses newer Actions permission/concurrency features not
recognized by the installed actionlint, so lint the handwritten workflows and
validate the generated workflow through the compiler and hosted execution.

## Hosted evidence

Assignment and the opt-in label started these actual fork investigations.
The clean repair chain required no operator intervention after assignment:

| Issue | Observed outcome |
| --- | --- |
| [radical/aspire#147](https://github.com/radical/aspire/issues/147) | Four fresh investigators; one induced baseline failure, a persisted 60-second pause, one deliberately ineffective candidate failure, then nine passes on three parallel workers; actual draft [radical/aspire#150](https://github.com/radical/aspire/pull/150) and terminal findings |
| [radical/aspire#148](https://github.com/radical/aspire/issues/148) | Three fresh investigators; five passes across two rounds; terminal non-reproduction findings, no candidate or PR |
| [radical/aspire#146](https://github.com/radical/aspire/issues/146) | Two fresh investigators; actual owned sleeping job cancelled; findings with zero test executions |
| [radical/aspire#149](https://github.com/radical/aspire/issues/149) | Operator cancelled only its identified investigator before its callback; the controller posted blocked findings automatically, without tests, candidates or a PR |

The real repair is `5fad4dc4cb924f93c665f09b57b0e3f7ea7acf82`; the verification
run is [39642087881](https://github.com/radical/aspire/actions/runs/39642087881).
Independent downloads checked every original target TRX, output log and checkout
receipt, exact source identities, positive execution counts and the actual draft
PR. The final investigator read the published source/diff and all nine TRX
outcome attributes, but only a job-log excerpt. Its self-report was not used as
the independent execution oracle.

Four explicit terminal resumes, one for each outcome above, created no new
investigators, workers, candidates, PRs or terminal comments. Checkpoint revisions,
candidate refs and existing result bodies remained identical.

The first two issue workers stopped before execution with MSB3202 because a
relative `-projects` input was resolved from Arcade's build infrastructure.
Their automatic blocked findings remain on
[radical/aspire#144](https://github.com/radical/aspire/issues/144) and
[radical/aspire#145](https://github.com/radical/aspire/issues/145).
The matrix now supplies the absolute candidate project path.
`test_matrix_provides_absolute_arcade_project_path` fails against the actual
pre-fix implementation and passes with the correction. All 33 behavioral checks
pass.

These results prove the bounded catalog-based fork lifecycle, not arbitrary
issue selection, natural flake elimination or upstream readiness. Recovery after
an arbitrary interrupted controller or a lost worker callback remains outside
the hosted proof; an operator resume entry point is not a scheduled watchdog.
