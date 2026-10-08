# CI Shepherd Pilot Architecture Artifact

## Purpose

Create a standalone interactive HTML artifact that explains the current CI
Shepherd pilot implementation to maintainers and operators.

The artifact focuses on:

- hosted and local pilot entry points;
- shared observation, decision, settlement, and publication flow;
- canonical and non-authoritative data stores;
- every externally visible mutation available to the pilot;
- trust and credential boundaries;
- fail-closed behavior, recovery, and manual Agent Merge handoff.

The legacy fixture, reconciliation, and pinned recovery paths are out of scope.

## Deliverable

The final deliverable is one self-contained HTML file under the session artifact
directory. It is not committed to the repository and has no network or runtime
dependencies.

The page opens on an end-to-end architecture map. A compact control bar lets the
reader emphasize one concern at a time:

- runtime;
- state;
- mutations;
- trust;
- failure paths.

Selecting a component reveals its owning modules, inputs, outputs, persisted
boundaries, and operational invariants. Print mode expands all details.

## Architecture Model

### Entry lanes

The artifact shows two execution lanes:

1. The hosted GitHub Actions workflow runs from a daily schedule or manual
   dispatch. Deterministic jobs prepare host-owned evidence, isolate reasoning,
   settle billing, optionally validate an inline repair, and publish guarded
   effects.
2. The local controller supports observation, one-shot runs, repeated watches,
   explicit handoff confirmation, and bounded resume operations. It uses a
   machine-wide authority lock, requires the hosted workflow to be disabled for
   effects, and invokes the same shared pilot controller.

Both lanes converge on `pilot.prepare` and `pilot.settle`.

### Shared controller flow

The primary flow is:

```text
trigger
  -> verify configuration and authority
  -> sweep adopted subjects and owned work
  -> persist observations and reconciled receipts
  -> select one due chain
  -> reserve round, credits, capacity, and settlement space
  -> run isolated typed reasoning when required
  -> settle native usage
  -> refresh all effect guards
  -> dispatch, validate, publish, remind, or wait
  -> persist receipts and render status
```

Cheap observation, human waits, billing reconciliation, and status rendering do
not require model inference. One serialized sweep admits at most one native
decision, while review-request admission is independently bounded.

### Reasoning boundary

The model receives a bounded host-built packet containing policy and projected
evidence. It has no GitHub credential, repository writer, shell, arbitrary file
access, or general mutation tool.

Its only output path is one typed `submit_decision` call. Reviewed host code
validates that proposal against the packet, fresh observations, budgets, and
authority before any effect.

## Data Stores

### Canonical authority

The canonical store is one bounded JSON ledger embedded in an authenticated
GitHub authority comment. It persists:

- chain and subject mappings;
- fair-selection cursor;
- lifetime rounds and local-attempt counters;
- operation identities and states;
- native and worker credit reservations and actual usage;
- task, session, and worker receipts;
- feedback dispositions;
- review-request receipts;
- reminder receipts;
- result publication receipts;
- manual handoff state.

No local file or Actions artifact can initialize, replace, truncate, or reset
this authority.

### Fresh external evidence

Repository, issue, pull request, head, labels, checks, statuses, workflow runs,
reviews, thread resolution, comments, tasks, sessions, artifacts, and billing
remain GitHub-owned state. The controller rereads the required subset before
each effect rather than treating previous packets as current truth.

### Transport and audit artifacts

Hosted Actions artifacts carry packets, envelopes, host evidence, native usage,
validation results, settlements, publication receipts, and failure audits.
Local run directories carry the equivalent packet/result data and a
human-readable report.

These files support transport, diagnostics, and operator review. They are not
authority or repair input and cannot authorize a retry.

## External Mutation Matrix

The artifact documents each effect with its owner, prerequisite, persisted
send boundary, final guards, success receipt, and uncertain-response behavior.

| Effect | Purpose | Owner |
| --- | --- | --- |
| Edit authority comment | Persist ledger transitions and accounting | Pilot GitHub adapter |
| Create cloud agent task | Request an initial implementation or PR repair | Pilot dispatch / handoff |
| Publish validated inline commit | Apply the narrow hosted inline repair profile | Pilot patch publisher |
| Request Copilot review | Start one guarded review for a ready current head | Pilot review policy |
| Post reminder | Notify the operator about a verified unchanged blocker | Pilot reminder policy |
| Post worker result | Publish a bounded controller-owned result report | Pilot result publisher |
| Add child adoption label | Bind a verified issue-created PR to its chain | Pilot GitHub adapter |
| Remove origin adoption label | Clean up an exact mapped origin after merge | Manual handoff policy |
| Update presentation comment | Publish current chain status without changing authority | Pilot GitHub adapter |

Merge, close, workflow approval, general Agent Merge activation, workflow
reruns, force-push, cancellation, and arbitrary comments remain human-owned or
unsupported.

## Failure and Recovery Semantics

The visualization emphasizes these invariants:

- reservations and write intent are persisted before an external effect;
- a lost or ambiguous write response is not retried blindly;
- unreadable or conflicting evidence becomes unknown and retains capacity;
- task completion is not proof that code changed, tests passed, or the PR is
  ready;
- current-head CI and current review evidence determine readiness;
- stale heads, changed authority, takeover, hands-off, disablement, dirty local
  controller source, or active hosted execution block effects;
- unknown billing is not treated as zero;
- counters and reservations survive restart, re-adoption, and head changes;
- reports and artifacts never reset the ledger.

Manual handoff becomes a sticky no-repair authority after conversion. Existing
PRs wait for owned work and review requests to become quiescent. Initial issues
may start one compact implementation worker, verify its unique PR/branch
mapping, and then follow the same handoff path. Operator confirmation asserts
that Agent Merge review, CI, and conflict actions are enabled while merging
remains disabled. Shepherd then monitors lifecycle and reminders without
resuming repairs.

## Information Design

The page contains four coordinated layers:

1. **Runtime map** — hosted and local lanes converging on the shared controller,
   isolated reasoner, guarded effectors, and external services.
2. **State map** — canonical ledger, fresh GitHub evidence, hosted artifacts,
   and local audit files, with authority direction made explicit.
3. **Mutation matrix** — allow-listed effects, credentialed owner, guard point,
   and receipt behavior.
4. **Failure and handoff map** — operation states, uncertainty, recovery, timed
   waits, worker lifecycle, and sticky manual handoff.

Color is supplemental. Shapes, labels, line styles, and badges also distinguish
reads, persisted state, decisions, mutations, and human-only actions.

## Validation

Validation is manual browser automation against the standalone file:

- open the file with JavaScript enabled;
- verify no console errors;
- exercise every concern filter and component detail control;
- verify the end-to-end path remains understandable with all filters reset;
- verify the mutation matrix identifies every effect listed above;
- verify canonical and non-authoritative stores are visually distinct;
- verify desktop and narrow viewport layouts remain readable;
- verify print mode expands the hidden details;
- verify the page performs no network requests.

The artifact must remain usable as a static file without a server after it is
generated.
