# CI failure automation acceptance contracts

The C# suite in
[`tests/Infrastructure.Tests/CiFailureContracts`](../../tests/Infrastructure.Tests/CiFailureContracts)
specifies deterministic behavior for a port of the **whole** Analyze CI Failure
pipeline, not just its output validator. It exercises observable outcomes,
external calls, and durable memory. It does not inspect shell, JavaScript,
Markdown, or YAML source for implementation strings.

**These tests need a concrete adapter to the port.** `CiFailureBehaviorTests` is
deliberately abstract. The current shell workflow is not an implementation of
this contract, and a normal Infrastructure.Tests run does not execute this
abstract suite. A passing build is not a passing acceptance run. There are no
skipped tests, success-shaped placeholder adapters, or test-owned versions of
the production algorithms.

## Behavioral baseline and intentional corrections

The baseline is repository commit
`904d023e9540985120868ee51747973279fb28f0`, including the source workflow,
`analyze-ci-failure-{validation,persistence,history,candidates,issue,comment}.sh`,
the workflow maintenance documentation, and existing
`AnalyzeCiFailureWorkflowTests`.

Two open bug-fix proposals inform corrected expectations rather than preserving
known broken behavior:

- [#20332](https://github.com/microsoft/aspire/issues/20332) and
  [#20333](https://github.com/microsoft/aspire/pull/20333), reviewed at
  `7096380d79670534416eab19d266b438b6018702`: a durable cause identifies a
  mechanism, not one immutable scalar test name. A current flaky cause can
  contain up to 200 exact `{name, job_id}` observations across jobs. Each
  validated flaky pair has exactly one owning cause. Stored diagnostics and
  legacy labels remain authoritative; current observations live on immutable
  per-attempt occurrences. Issue history is reconstructed from persisted
  evidence and preserves operator text outside the managed section.
- [#20624](https://github.com/microsoft/aspire/pull/20624), reviewed at
  `a26c0493173d55af09e1f2746f092f60f009b55e`: a main-scope publication request
  does not need a PR-number property.

The suite also distinguishes a missing memory branch from an authentication or
transport failure. Only genuine absence permits initialization. A failed read
must not silently become an empty repository.

The suite does **not** adopt the separate feature/policy proposal in
[#20109](https://github.com/microsoft/aspire/pull/20109). In particular, existing
PR transient-cause persistence, per-job artifact provenance, the current verdict
vocabulary, and infrastructure-only reruns remain the baseline. Validator-port
PRs [#20675](https://github.com/microsoft/aspire/pull/20675) and
[#21231](https://github.com/microsoft/aspire/pull/21231) do not define the new
suite's implementation API.

[Issue #19578](https://github.com/microsoft/aspire/issues/19578) motivates making
every prior cause available to analysis, including causes beyond an initial
summary preview. It does not justify deterministic fuzzy matching of cause IDs.
The publisher must not merge mechanisms merely because they occur in the same
test or job. Semantic diagnosis, matching diagnostics to an existing mechanism,
and whether an LLM actually reads all supplied context need separate model
evaluation; this suite tests the evidence and publication boundaries.

## Bind the suite to the port

Add a non-abstract subclass in the port's test project:

```csharp
using System.Text.Json.Nodes;
using Infrastructure.Tests.CiFailureContracts;

public sealed class PortCiFailureTests : CiFailureBehaviorTests
{
    protected override Task<CiFailureObservation> ExecuteAsync(
        string operation, JsonObject input)
        => PortTestAdapter.ExecuteAsync(operation, input);
}
```

`PortTestAdapter` is the port-specific integration code, not supplied by these
tests. It must invoke the **real** port, in process or through its command-line
entry points. It may translate field names, file layouts, CLI arguments, enums,
and transport payloads. It must not implement validation, matching, projection,
merging, classification, or publication decisions itself.

Copy or link all `CiFailureContracts/*.cs` files and the `Snapshots` directory
into an xUnit v3 test project with nullable references and implicit usings
enabled. The suite needs the BCL, xUnit, and Verify.XunitV3 (the repository uses
32.0.0), not Aspire production assemblies, bash, jq, Node, Python, network
access, credentials, a live GitHub repository, or a memory branch. Keep its
namespace when copying. The containing Infrastructure.Tests project currently
targets net11.0; the suite itself does not require repository-specific SDK APIs.

The adapter should provide isolated, recording GitHub and memory dependencies
for each invocation. It must use the real file/JSON parsers where relevant.
Use a fresh temporary workspace if invoking a process; do not change the global
current directory or environment. Capture both process streams concurrently,
bound execution, and terminate the process tree on timeout using shared test
process infrastructure. Fakes must be thread-safe if the port makes concurrent
calls. Any unexpected live network or git operation should fail the test.

For example, after adding the concrete subclass:

```bash
dotnet test --project tests/Infrastructure.Tests/Infrastructure.Tests.csproj \
  --no-launch-profile -- \
  --filter-class "*.PortCiFailureTests" \
  --filter-not-trait "quarantined=true" \
  --filter-not-trait "outerloop=true"
```

Check that the concrete class discovers and executes the inherited cases.
Zero discovered tests is not success.

## Adapter inputs

Each test calls `ExecuteAsync(operation, input)` with a fresh `JsonObject`.
Input JSON is a **test interchange format**, not a proposed production schema
or command-line protocol. Fields irrelevant to an operation can be ignored.
Missing fields must not be filled from another fixture field when a test is
checking an absent required document.

| Input | Meaning |
|---|---|
| `repository`, `now` | Trusted repository and injected clock. |
| `trigger` | Dispatch or automatic event; automatic events carry their immutable run attempt and conclusion. |
| `run`, `context`, `jobs` | Source metadata, trusted run context, and jobs. For `collect`, context must be derived rather than copied from the fixture's `context`. |
| `analysis`, `causes` | Agent proposal and filename-to-cause map. These are untrusted. |
| `test_evidence`, `test_failures` | Trusted structured-evidence availability and extracted failed-test records. |
| `prior_causes` | Collector-time cause snapshot, keyed by filename. |
| `memory` | Current durable branch files, keyed by paths such as `causes/feed-timeout.json` and `runs/123.json`. Publication must read this fresh snapshot, not substitute `prior_causes`. |
| `memory_branch_exists` | Defaults to true; false means a confirmed absent branch, not a failed read. |
| `publish_request`, `rerun_request`, `enable_rerun` | Safe-output requests and rerun feature gate. |
| `pull_request`, `current_run` | Responses for live subject-state and latest-attempt checks, distinct from collected evidence. |
| `pull_request_sequence` | Optional ordered replacement for repeated PR reads. Each response is consumed once; it models closing/locking between read and mutation. |
| `issues`, `comments` | GitHub state. Issues use REST label objects; comments include author login. Defaults are empty. |
| `issue_pages` | Optional paginated replacement for issue listings; direct linked lookups still use `issues`. |
| `next_issue_number`, `next_comment_id` | Deterministic fake allocation, defaults 200 and 300. |
| `commit_pr_pages`, `fork_pr_pages` | Arrays of API pages for subject-PR fallback. Missing collections behave as empty results. |
| `job_pages` | Optional ordered replacement for the jobs listing, with one array per API page. |
| `history_pages` | Ordered workflow-run API responses for successive history requests, including pagination/window subdivision. Each response is consumed once. |
| `last_successful_sha`, `failed_sha`, `comparison_pages`, `candidate_pr_pages` | Main comparison inputs; PR pages are keyed by commit SHA. |
| `last_successful_run`, `candidate_history`, `candidate_merges`, `triggering_merge` | Trusted main context for publication. Missing history is unavailable, not complete. |
| `artifacts`, `logs` | Artifact-list and per-job log responses. |
| `artifact_archives` | Immutable artifact ID (decimal string) to base64 ZIP bytes returned by successful downloads during `collect`. Missing bytes are an adapter error, not an empty successful download. |
| `files`, `format`, `job` | Extracted result filenames and text, `trx`/`mocha` parser selection, and producing job. |
| `archive_base64`, `expected_bytes` | Actual ZIP bytes and declared compressed size, not a fake archive listing. |
| `budgets` | Optional small limits: `artifact_count`, `artifact_bytes`, `total_bytes` for selection; `archive_bytes`, `entry_count`, `uncompressed_bytes` for extraction. During collection, `uncompressed_bytes` is one shared remaining-byte budget across all archives, not a fresh allowance per artifact. Omitted budgets use production defaults. |
| `text`, `max_length` | Sanitization/rendering input and Unicode-scalar limit. |
| `raw_documents` | Raw-text replacements applied before parsing. Keys name `analysis`, `context`, `run`, `jobs`, `test_evidence`, or `causes/<filename>`. Invalid JSON must reach the real parser. |
| `faults` | External operations that fail with the supplied diagnostic. Inject at the dependency boundary, not by replacing production decisions. |

Fault keys use the recorded call names below. A `:<id>` suffix restricts a fault
to an artifact, job, or commit SHA. `memory-commit:1` and `memory-commit:2` fail
the first or second attempted push. An unsuffixed fault affects every call of
that kind. A failed memory commit does not advance durable `Memory`.
The baseline contract fails a rejected push immediately; it does not retry the
push automatically. Recovery occurs through a new, idempotent publication
invocation. Adding concurrent push retries requires a deliberate contract update
and transport-level convergence coverage.

The fake API must apply successful mutations to its own state. Created issue
records must include their number, canonical URL, state, body, and labels, so
tests can seed them into a new invocation after partial publication. Accept
recorded mutation labels as strings or REST label objects when seeding fake
state; this is representation translation, not matching logic. Likewise,
normalize a recorded creation's canonical `url` into REST `html_url` when it is
subsequently supplied in `issues`; never confuse it with an API resource URL.

## Operations and observations

| Operation | Production boundary | Required `Output` |
|---|---|---|
| `collect` | Trigger admission, metadata/subject/job/log/artifact/prior-cause collection | `has_work`, derived `context`, `jobs`, `logs`, `prior_causes`, `test_evidence`, `test_failures` when applicable |
| `validate` | Parsing, sanitization, exact evidence and cause/verdict validation | Sanitized, trusted-diagnostic-bound `analysis` on success |
| `last-success` | Bounded paginated main history search | `run`, an object (empty if exhausted) |
| `candidate-merges` | Comparison completeness and exact merged-PR associations | `state` (`available`, `incomplete`, `unavailable`), `candidates` |
| `select-artifacts` | Job/attempt provenance and download budgets | `artifacts` with immutable ID, job, name, size, and format |
| `extract-artifact` | Real bounded ZIP extraction | `files`, a relative flattened filename-to-text map; empty on failure |
| `read-test-results` | Real TRX/Mocha parsing and provenance binding | `test_failures`; empty on rejected/withheld evidence |
| `publish` | Validation, fresh memory merge, issue reconciliation/projection, PR comment | No additional output fields required; inspect durable `Memory` and recorded `Calls` |
| `rerun` | Independent evidence validation and live mutation gates | No additional output fields required |
| `sanitize-text` | Redaction, control stripping, newline normalization, bounding | `text` without a transport-added trailing newline |
| `render-text` | Inert indented rendering | `text`, including the renderer's trailing newline |

Return `CiFailureObservation`:

- `Status`: `Success` for completed work (including optional work withheld with
  warnings), `Skipped` for an intentional no-op gate, `Rejected` for invalid
  evidence or a required external operation that failed.
- `Output`: actual returned documents/artifacts, normalized only for transport.
- `Memory`: files in the **durable committed** fake branch after the invocation,
  including successful progress before failure. Do not expose uncommitted local
  files here.
- `Calls`: actual attempted external operations in execution order.
- `Diagnostics`: emitted warnings/errors/informational skip reasons, not
  test-generated explanations. Do not demand exact shell-specific wording.

`CiFailureCall.Succeeded` distinguishes an attempted failed operation from an
applied mutation. Its `Data` is a snapshot, not a reference to later-mutated
state. `Mutations` contains only successful writes; `AttemptedMutations` also
contains failed writes. Invalid input and precondition gates must not attempt
writes at all. Transport-failure cases instead assert the expected failed
attempt and any previously committed progress. Normalize call names as follows:

| Kind | Data used by assertions |
|---|---|
| `get-run` | `run_id`, `attempt` (null for latest metadata) |
| `get-current-run` | `run_id` |
| `list-jobs`, `get-job-log` | `run_id`/`attempt` or job `id` |
| `list-commit-prs` | `sha` |
| `list-fork-prs` | `head`, `state` |
| `list-successful-runs` | `workflow_id`, `branch`, `event`, `status`, UTC `start`/`end`, `page` |
| `compare` | Trusted base and failed SHAs; supports the `compare` fault key |
| `list-open-issues`, `list-closed-issues`, `get-issue` | Query or issue `number` |
| `get-pr`, `list-comments` | `pr_number` |
| `list-artifacts`, `download-artifact` | Source run or immutable artifact `id`; listing failures support the `list-artifacts` fault key |
| `memory-read`, `memory-commit` | A successful commit includes a full `files` snapshot |
| `create-issue` | Allocated `number`, canonical `url`, `state`, `title`, full `body`, `labels` as strings |
| `update-issue` | `number` and fields actually changed (`title`, full `body`, labels) |
| `reopen-issue`, `ensure-label` | `number` or label name |
| `create-comment`, `update-comment` | `pr_number`, comment `id`, full `body` |
| `rerun` | `run_id`; failed-jobs-only rerun endpoint |

Ordinary transport pagination may produce additional read calls. Do not record
test assertions or adapter bookkeeping as production calls. Do not hide extra
mutations, collapse multiple issue creations, manufacture a successful commit,
or reconstruct a supposedly published body in the adapter.

Comment snapshots were generated with the baseline's actual comment renderer,
using trusted diagnostic inputs. They intentionally preserve its inert Markdown,
verdict-specific remediation, first-five-stack-frame limit, and redaction.
The grouped issue display retains the correction's twenty-test display limit;
the durable occurrence still retains every authenticated observation. The
indented text renderer's trailing newline is intentional compatibility behavior.
Grouped observation arrays preserve the supplied order for legacy projection
compatibility; no global alphabetical sorting requirement is imposed.

## Coverage boundaries

The partial classes divide the acceptance cases into validation, collection,
history, artifacts, test results, memory/grouped causes, legacy migration,
publication, reruns, and text handling. They include positive cases alongside
negative cases and exact budget boundaries. Recovery tests feed an earlier
invocation's durable memory and created GitHub state into a new invocation.
They never recover state from a test-owned reimplementation of the publisher.

The following remain outside a deterministic adapter suite:

- LLM diagnosis quality and semantic matching of new failures to mechanisms.
- GitHub workflow scheduling, gh-aw safe-output schema admission, actual token
  permissions, GitHub artifact storage, git server semantics, and Actions'
  serialization/queue configuration. Main-without-PR publication is specified
  here, but its gh-aw schema still needs workflow-level validation.
- Live multi-process races. Commit failure and replay recovery are covered;
  distributed convergence needs a transport/concurrency integration test.
- Universal equivalence with every malformed jq/Python input and every ZIP
  encoding. The contract focuses on supported shapes, provenance, bounds,
  malformed evidence, unsafe paths/file types, and atomic extraction.

Keep the existing executable helper tests. They remain the evidence for the
current implementation until a real port adapter executes this suite. Add
port-specific entry-point/transport tests and workflow-level validation rather
than treating adapter conformance alone as proof that CI is correctly wired.
