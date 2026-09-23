---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests (over-selection) or where a
  narrow result misses a runtime-only consumer, even with no matching
  rule (under-selection),
  classifies why, checks how similar cases were handled in the trigger
  map's own commit history, and files at most one issue per run for its
  single highest-confidence case where the selection could be made safer
  or cheaper. Raw per-PR evidence is retained for a rolling 14-day window,
  while settled per-input/target verdicts persist in a memory branch.
  The filed issue is assigned to the Copilot coding agent, which
  implements and validates the fix and opens a PR for human review. This
  workflow never edits the trigger map itself.

max-daily-ai-credits: -1

on:
  schedule: weekly on monday
  workflow_dispatch:
    inputs:
      lookback_days:
        description: "How many days of PRs/CI runs to analyze (default: 14)"
        required: false
        type: number
      pr_numbers:
        description: "Optional: comma-separated PR numbers to focus on instead of the lookback window"
        required: false
        type: string

# Only run in the canonical repository. Forks don't have the required
# secrets/permissions for this report workflow.
if: github.repository == 'microsoft/aspire'

permissions:
  contents: read
  issues: read
  pull-requests: read
  actions: read
  copilot-requests: write

concurrency:
  # gh-aw's compiler always emits a static top-level group for this
  # workflow ("gh-aw-${{ github.workflow }}", queue: max) in addition to
  # whatever this field configures, and that group has no awareness of
  # `pr_numbers` — it serializes every run of this workflow, full-window
  # or PR-focused, one at a time, queued in trigger order. That is
  # deliberate here, not just an accepted side effect: two agent runs
  # executing concurrently would each read the memory ledger from the
  # same base and independently rewrite it (head replacements and
  # watchlist updates); the push that lands second can discard the first's
  # rows, even for appends (see step 13). This job-discriminator only
  # scopes the agent job's own concurrency group; it cannot change the
  # top-level group's serialization.
  job-discriminator: ${{ github.event.inputs.pr_numbers || github.run_id }}

engine: copilot
timeout-minutes: 30

network:
  allowed:
    - defaults

pre-agent-steps:
  - name: Compact test-selection memory
    env:
      MEMORY_ROOT: /tmp/gh-aw/repo-memory/default
      RETENTION_DAYS: "14"
      AUDIT_DATE_PATH: ${{ runner.temp }}/gh-aw/test-selection-audit/audit-date.txt
    run: node .github/workflows/test-selection-audit/compact_memory.cjs
  - name: Collect test-selection evidence
    env:
      GH_TOKEN: ${{ github.token }}
      REPOSITORY: ${{ github.repository }}
      LOOKBACK_DAYS: ${{ github.event.inputs.lookback_days }}
      PR_NUMBERS: ${{ github.event.inputs.pr_numbers }}
      OUTPUT_PATH: ${{ runner.temp }}/gh-aw/test-selection-audit/evidence.json
      PROCESSED_RUNS_PATH: /tmp/gh-aw/repo-memory/default/processed-runs.jsonl
      PROCESSED_BASELINE_PATH: ${{ runner.temp }}/gh-aw/test-selection-audit/processed-runs-before.jsonl
      WATCHLIST_PATH: /tmp/gh-aw/repo-memory/default/watchlist.jsonl
      WATCHLIST_BASELINE_PATH: ${{ runner.temp }}/gh-aw/test-selection-audit/watchlist-before.jsonl
      AUDIT_DATE_PATH: ${{ runner.temp }}/gh-aw/test-selection-audit/audit-date.txt
    run: python3 .github/workflows/test-selection-audit/collect_evidence.py

  # A selection is fixed for a particular CI run/attempt, not for a PR
  # head: re-runs can replace its result. `processed-runs.jsonl` retains
  # each head and its counted path contributions so a newer attempt can
  # replace, rather than add to, its earlier counts. Deterministic compaction
  # bounds this raw ledger to the configured lookback window.
  #
  # `watchlist.jsonl` retains rolling counts plus settled dispositions, so
  # known correct, filed, in-flight, and fixed cases survive raw-row expiry.
  #
  # Repo memory, not cache memory: GitHub Actions evicts unused caches after
  # 7 days, which is exactly this workflow's period, so a cache would
  # routinely be gone by the next run. Repo memory is branch-backed and
  # retained indefinitely.
tools:
  bash: ["cat", "ls", "grep", "head", "tail", "wc"]
  github:
    # Only GitHub MCP reads: repository source (selector implementation,
    # trigger map, docs), PR history, and issues (to reconcile a
    # `pending-filed` watchlist row against the real issue `create-issue`
    # produced, per step 1). CI selection evidence is collected by the
    # deterministic pre-agent step above, not by the agent. The `issues`
    # toolset also exposes `create_issue`, but the GitHub MCP server always
    # runs with `GITHUB_READ_ONLY: "1"` regardless of toolset -- write tools
    # are non-functional here. All writes go through safe-outputs instead.
    # The default "approved" integrity filter would hide fork PRs from
    # first-time/external contributors -- exactly the fork PRs this audit
    # is meant to inspect, so it is disabled here. GitHub mutations are
    # limited to one safe-output issue whose resulting PR still requires
    # human review before merging.
    toolsets: [repos, pull_requests, issues]
    min-integrity: none
  repo-memory:
    branch-name: memory/test-selection-audit
    description: "Resolved PR selections and the rule watchlist for the CI test-selection audit"
    # Both ledgers are JSONL so individual observations can be counted and
    # updated. gh-aw's push retry uses `git pull --no-rebase -X ours` (step
    # 13), not a JSONL-aware merge; even two appends can conflict and lose
    # rows. The workflow-level concurrency group protects these ledgers.
    file-glob: ["processed-runs.jsonl", "watchlist.jsonl"]
    allowed-extensions: [".jsonl"]
    # Defaults (100KB file / 10KB patch) are too small: the rolling ledger
    # retains one row per resolved PR head and a busy window covers hundreds.
    max-file-size: 2097152
    max-patch-size: 262144
    max-file-count: 10
    validation:
      timeout-minutes: 1
      script: |
        const allowedFiles = new Set(["processed-runs.jsonl", "watchlist.jsonl"]);
        const pathPattern = /^[A-Za-z0-9._/@+#=\-]+$/;
        const globPattern = /^[A-Za-z0-9._/@+#=*?\[\]\-]+$/;
        const targetPattern = /^(test|job):[A-Za-z0-9._-]+$/;
        const shaPattern = /^[0-9a-f]{40}$/;
        const titlePattern = /^\[test-selection-audit\] [A-Za-z0-9 .-]{1,77}$/;
        let auditDate = new Date().toISOString().slice(0, 10);

        const fail = message => {
          throw new Error(`Invalid test-selection audit memory: ${message}`);
        };
        const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
        const requireKeys = (value, required, allowed, context) => {
          if (!isObject(value)) fail(`${context} must be an object`);
          for (const key of required) {
            if (!(key in value)) fail(`${context} is missing ${key}`);
          }
          for (const key of Object.keys(value)) {
            if (!allowed.has(key)) fail(`${context} has unexpected field ${key}`);
          }
        };
        const requireInteger = (value, context, minimum = 0) => {
          if (!Number.isSafeInteger(value) || value < minimum) fail(`${context} must be an integer >= ${minimum}`);
        };
        const requireString = (value, pattern, context, maxLength = 400) => {
          if (typeof value !== "string" || value.length === 0 || value.length > maxLength || !pattern.test(value)) {
            fail(`${context} is invalid`);
          }
        };
        const requireUtcDate = (value, context) => {
          requireString(value, /^\d{4}-\d{2}-\d{2}$/, context, 10);
          const parsed = new Date(value + "T00:00:00Z");
          if (Number.isNaN(+parsed) || parsed.toISOString().slice(0, 10) !== value || value > auditDate) {
            fail(`${context} must be a UTC date <= ${auditDate}`);
          }
        };
        const requireUniqueStrings = (values, pattern, context) => {
          if (!Array.isArray(values)) fail(`${context} must be an array`);
          const seen = new Set();
          for (const [index, value] of values.entries()) {
            requireString(value, pattern, `${context}[${index}]`);
            if (seen.has(value)) fail(`${context} contains duplicate ${value}`);
            seen.add(value);
          }
          return seen;
        };
        const readJsonLines = fileName => {
          const fullPath = path.join(memoryRoot, fileName);
          if (!fs.existsSync(fullPath)) return [];
          const text = fs.readFileSync(fullPath, "utf8");
          if (text.length === 0) return [];
          if (!text.endsWith("\n")) fail(`${fileName} must end with a newline`);
          return text.trimEnd().split("\n").map((line, index) => {
            if (line.length > 16384) fail(`${fileName}:${index + 1} exceeds 16 KiB`);
            try {
              return JSON.parse(line);
            } catch {
              fail(`${fileName}:${index + 1} is not valid JSON`);
            }
          });
        };

        for (const entry of fs.readdirSync(memoryRoot, { withFileTypes: true })) {
          if (entry.name === ".git") continue;
          if (!entry.isFile() || !allowedFiles.has(entry.name)) {
            fail(`unexpected memory entry ${entry.name}`);
          }
        }

        const processedAllowed = new Set([
          "pr", "sha", "run", "attempt", "all", "over_paths", "miss_edges", "seen"
        ]);
        const edgeAllowed = new Set(["path", "target"]);
        const processed = readJsonLines("processed-runs.jsonl");
        const provenanceRoot = path.join(
          process.env.RUNNER_TEMP || fail("RUNNER_TEMP unavailable"),
          "gh-aw/test-selection-audit");
        const [evidenceFile, processedBefore, watchBefore] =
          ["evidence.json", "processed-runs-before.jsonl", "watchlist-before.jsonl"]
            .map(fileName => path.join(provenanceRoot, fileName));
        const provenance = [evidenceFile, processedBefore, watchBefore].map(fs.existsSync);
        if (new Set(provenance).size > 1) fail("provenance files are incomplete");
        const hasProvenance = provenance[0];
        const canonical = value => {
          if (Array.isArray(value)) return value.map(canonical);
          if (!isObject(value)) return value;
          return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
        };
        const parseBaseline = filePath => {
          const rows = new Map();
          for (const line of fs.readFileSync(filePath, "utf8").split("\n")) {
            if (!line) continue;
            const row = JSON.parse(line);
            rows.set(`${row.pr}:${row.sha}`, row);
          }
          return rows;
        };
        let baselineRows = new Map();
        const trustedSelections = new Map();
        const recordedSelections = new Map();
        if (hasProvenance) {
          const evidence = JSON.parse(fs.readFileSync(evidenceFile, "utf8"));
          requireUtcDate(evidence.auditDate, "evidence.auditDate");
          auditDate = evidence.auditDate;
          baselineRows = parseBaseline(processedBefore);
          for (const record of evidence.records) {
            const selection = record.selection;
            if (selection.status === "resolved" && selection.creditable === true) {
              trustedSelections.set(`${record.pr}:${record.headSha}`, selection);
            } else if (selection.status === "recorded") {
              recordedSelections.set(`${record.pr}:${record.headSha}`, selection);
            }
          }
        }
        const processedKeys = new Set();
        const affectedWatchKeys = new Set();
        const overCounts = new Map();
        const missCounts = new Map();
        const overPrs = new Map();
        const missPrs = new Map();
        const addAffectedWatchKeys = row => {
          for (const pathValue of row?.over_paths || []) {
            affectedWatchKeys.add(`over\u0000${pathValue}`);
          }
          for (const edge of row?.miss_edges || []) {
            affectedWatchKeys.add(`miss\u0000${edge.path}\u0000${edge.target}`);
          }
        };
        for (const [index, row] of processed.entries()) {
          const context = `processed-runs.jsonl:${index + 1}`;
          requireKeys(
            row,
            ["pr", "sha", "run", "attempt", "all", "over_paths", "miss_edges", "seen"],
            processedAllowed,
            context);
          requireInteger(row.pr, `${context}.pr`, 1);
          requireString(row.sha, shaPattern, `${context}.sha`, 40);
          requireInteger(row.run, `${context}.run`, 1);
          requireInteger(row.attempt, `${context}.attempt`, 1);
          if (typeof row.all !== "boolean") fail(`${context}.all must be boolean`);
          requireUtcDate(row.seen, `${context}.seen`);
          const identity = `${row.pr}:${row.sha}`;
          if (processedKeys.has(identity)) fail(`duplicate processed identity ${identity}`);
          processedKeys.add(identity);
          const baselineRow = baselineRows.get(identity);
          const selection = trustedSelections.get(identity);
          const recordedSelection = recordedSelections.get(identity);
          if (hasProvenance) {
            if (selection) {
              if (row.run !== selection.run ||
                  row.attempt !== selection.attempt ||
                  row.all !== selection.result.selectsAll) {
                fail(`${context} does not match trusted selection evidence`);
              }
              if (row.seen !== auditDate) {
                fail(`${context}.seen must match the protected audit date`);
              }
            } else if (recordedSelection) {
              if (!baselineRow ||
                  baselineRow.run !== recordedSelection.run ||
                  baselineRow.attempt !== recordedSelection.attempt ||
                  JSON.stringify(canonical(baselineRow)) !== JSON.stringify(canonical(row))) {
                fail(`${context} does not match its recorded baseline`);
              }
            } else if (!baselineRow ||
                JSON.stringify(canonical(baselineRow)) !== JSON.stringify(canonical(row))) {
              fail(`${context} is not an unchanged baseline or trusted selection`);
            }
          }

          if (!row.all && row.over_paths.length > 0) {
            fail(`${context}.over_paths must be empty for a narrow selection`);
          }
          for (const pathValue of requireUniqueStrings(row.over_paths, pathPattern, `${context}.over_paths`)) {
            overCounts.set(pathValue, (overCounts.get(pathValue) || 0) + 1);
            if (!overPrs.has(pathValue)) overPrs.set(pathValue, new Set());
            overPrs.get(pathValue).add(row.pr);
          }

          if (!Array.isArray(row.miss_edges)) fail(`${context}.miss_edges must be an array`);
          if (row.all && row.miss_edges.length > 0) {
            fail(`${context}.miss_edges must be empty for an ALL selection`);
          }
          const edgeKeys = new Set();
          for (const [edgeIndex, edge] of row.miss_edges.entries()) {
            const edgeContext = `${context}.miss_edges[${edgeIndex}]`;
            requireKeys(edge, ["path", "target"], edgeAllowed, edgeContext);
            requireString(edge.path, pathPattern, `${edgeContext}.path`);
            requireString(edge.target, targetPattern, `${edgeContext}.target`);
            const edgeKey = `${edge.path}\u0000${edge.target}`;
            if (edgeKeys.has(edgeKey)) fail(`${context} contains duplicate missing edge`);
            edgeKeys.add(edgeKey);
            missCounts.set(edgeKey, (missCounts.get(edgeKey) || 0) + 1);
            if (!missPrs.has(edgeKey)) missPrs.set(edgeKey, new Set());
            missPrs.get(edgeKey).add(row.pr);
          }

          if (selection) {
            const result = selection.result;
            const inputPaths = new Set([
              ...result.changedFiles,
              ...result.excludedFiles,
              ...result.unattributedFiles
            ]);
            const selectedTargets = new Set([
              ...result.testProjects.map(name => `test:${name}`),
              ...result.jobs
            ]);
            if (!result.sourceHasDiff && row.over_paths.length > 0) {
              fail(`${context}.over_paths requires selection-time diff attribution`);
            }
            for (const pathValue of row.over_paths) {
              if (!inputPaths.has(pathValue)) {
                fail(`${context}.over_paths contains a path absent from trusted evidence`);
              }
            }
            for (const edge of row.miss_edges) {
              if (!inputPaths.has(edge.path)) {
                fail(`${context}.miss_edges contains a path absent from trusted evidence`);
              }
              if (selectedTargets.has(edge.target)) {
                fail(`${context}.miss_edges contains a target selected by trusted evidence`);
              }
            }
            addAffectedWatchKeys(baselineRow);
            addAffectedWatchKeys(row);
          }
        }
        if (hasProvenance) {
          for (const identity of baselineRows.keys()) {
            if (!processedKeys.has(identity)) fail(`missing baseline processed row ${identity}`);
          }
          for (const identity of trustedSelections.keys()) {
            if (!processedKeys.has(identity)) fail(`missing processed row for trusted selection ${identity}`);
          }
        }

        const watchIdentity = row => row.kind === "over-selection"
          ? `over\u0000${row.path}`
          : `miss\u0000${row.path}\u0000${row.target}`;
        const baselineWatchRows = new Map();
        if (hasProvenance) {
          for (const line of fs.readFileSync(watchBefore, "utf8").split("\n")) {
            if (!line) continue;
            const row = JSON.parse(line);
            baselineWatchRows.set(watchIdentity(row), row);
          }
        }
        const withoutLifecycle = row => {
          const value = { ...row };
          delete value.verdict;
          delete value.ref;
          delete value.note;
          return JSON.stringify(canonical(value));
        };
        const lifecycleTransitions = new Map([
          ["pending-filed", new Set(["filed", "watch"])],
          ["filed", new Set(["in-flight", "fixed", "watch"])],
          ["in-flight", new Set(["fixed", "watch"])],
          ["fixed", new Set(["watch"])],
          ["correct-by-design", new Set(["watch"])]
        ]);

        const watchAllowed = new Set([
          "path", "rule", "rule_ref", "path_ref", "consumer_refs", "target",
          "kind", "verdict", "all_runs", "miss_runs", "first_seen", "last_seen",
          "example_prs", "note", "ref"
        ]);
        const verdicts = new Set(["watch", "correct-by-design", "pending-filed", "filed", "in-flight", "fixed"]);
        const watch = readJsonLines("watchlist.jsonl");
        const watchKeys = new Set();
        for (const [index, row] of watch.entries()) {
          const context = `watchlist.jsonl:${index + 1}`;
          requireKeys(
            row,
            ["path", "rule", "rule_ref", "path_ref", "kind", "verdict",
             "first_seen", "last_seen", "example_prs", "ref"],
            watchAllowed,
            context);
          requireString(row.path, pathPattern, `${context}.path`);
          if (row.rule !== null) requireString(row.rule, globPattern, `${context}.rule`);
          requireString(row.rule_ref, /^[A-Za-z0-9._/@+#=\-]+@[0-9a-f]{7,40}$/, `${context}.rule_ref`);
          requireString(row.path_ref, /^[A-Za-z0-9._/@+#=\-]+@[0-9a-f]{7,40}$/, `${context}.path_ref`);
          requireUtcDate(row.first_seen, `${context}.first_seen`);
          requireUtcDate(row.last_seen, `${context}.last_seen`);
          if (row.first_seen > row.last_seen) fail(`${context}.first_seen is after last_seen`);
          if (!verdicts.has(row.verdict)) fail(`${context}.verdict is invalid`);
          if (row.ref !== null) requireInteger(row.ref, `${context}.ref`, 1);
          if (!Array.isArray(row.example_prs) || row.example_prs.length > 3) {
            fail(`${context}.example_prs must contain at most three PR numbers`);
          }
          const examples = new Set();
          for (const [exampleIndex, pr] of row.example_prs.entries()) {
            requireInteger(pr, `${context}.example_prs[${exampleIndex}]`, 1);
            if (examples.has(pr)) fail(`${context}.example_prs contains duplicates`);
            examples.add(pr);
          }

          let key;
          let expectedCount;
          let contributingPrs;
          if (row.kind === "over-selection") {
            if ("target" in row || "consumer_refs" in row || "miss_runs" in row) {
              fail(`${context} mixes under-selection fields into an over-selection row`);
            }
            requireInteger(row.all_runs, `${context}.all_runs`);
            key = `over\u0000${row.path}`;
            expectedCount = overCounts.get(row.path) || 0;
            contributingPrs = overPrs.get(row.path) || new Set();
          } else if (row.kind === "under-selection") {
            if ("all_runs" in row) fail(`${context} mixes all_runs into an under-selection row`);
            requireString(row.target, targetPattern, `${context}.target`);
            requireInteger(row.miss_runs, `${context}.miss_runs`);
            if (!Array.isArray(row.consumer_refs) || row.consumer_refs.length === 0) {
              fail(`${context}.consumer_refs must identify the proven runtime edge`);
            }
            requireUniqueStrings(
              row.consumer_refs,
              /^[A-Za-z0-9._/@+#=\-]+@[0-9a-f]{7,40}$/,
              `${context}.consumer_refs`);
            const edgeKey = `${row.path}\u0000${row.target}`;
            key = `miss\u0000${edgeKey}`;
            expectedCount = missCounts.get(edgeKey) || 0;
            contributingPrs = missPrs.get(edgeKey) || new Set();
          } else {
            fail(`${context}.kind is invalid`);
          }
          if (watchKeys.has(key)) fail(`duplicate watchlist identity ${key}`);
          watchKeys.add(key);

          const actualCount = row.kind === "over-selection" ? row.all_runs : row.miss_runs;
          if (actualCount !== expectedCount) {
            fail(`${context} counter ${actualCount} does not match ${expectedCount} processed heads`);
          }
          if (hasProvenance) {
            const baseline = baselineWatchRows.get(key);
            const current = JSON.stringify(canonical(row));
            const baselineSerialized = baseline && JSON.stringify(canonical(baseline));
            if (baselineSerialized !== current && !affectedWatchKeys.has(key)) {
              const transitionAllowed = baseline &&
                withoutLifecycle(baseline) === withoutLifecycle(row) &&
                lifecycleTransitions.get(baseline.verdict)?.has(row.verdict);
              if (!transitionAllowed) {
                fail(`${context} changes a watch row without trusted current evidence`);
              }
            }
            if (!baseline && actualCount === 0) {
              fail(`${context} adds a zero-count watch row`);
            }
          }
          for (const pr of examples) {
            if (!contributingPrs.has(pr)) fail(`${context}.example_prs contains uncredited PR ${pr}`);
          }

          if (row.verdict === "pending-filed") {
            requireString(row.note, titlePattern, `${context}.note`, 100);
            if (row.ref !== null) fail(`${context}.ref must be null while pending-filed`);
          } else if ("note" in row) {
            fail(`${context}.note is allowed only for pending-filed rows`);
          }
          if (["filed", "in-flight", "fixed"].includes(row.verdict) && row.ref === null) {
            fail(`${context}.ref is required for ${row.verdict}`);
          }
          if (["watch", "correct-by-design"].includes(row.verdict) && row.ref !== null) {
            fail(`${context}.ref must be null for ${row.verdict}`);
          }
        }

        if (hasProvenance) {
          for (const key of baselineWatchRows.keys()) {
            if (!watchKeys.has(key)) fail(`missing baseline watch row ${key}`);
          }
        }
        for (const pathValue of overCounts.keys()) {
          if (!watchKeys.has(`over\u0000${pathValue}`)) fail(`missing watchlist row for ${pathValue}`);
        }
        for (const edgeKey of missCounts.keys()) {
          if (!watchKeys.has(`miss\u0000${edgeKey}`)) fail(`missing watchlist row for missing edge`);
        }

safe-outputs:
  create-issue:
    title-prefix: "[test-selection-audit] "
    labels: [area-testing, area-pipelines]
    # Assigning `copilot` starts a Copilot coding agent session on the filed
    # issue, which implements and validates the fix and opens a PR for human
    # review. This requires the `GH_AW_AGENT_TOKEN` fine-grained PAT secret;
    # without it the issue is still filed but assignment fails.
    assignees: [copilot]
    # One finding per run. Each filed issue starts a coding agent session and
    # ends in a PR a human must review, so the workflow surfaces only its
    # single highest-confidence finding rather than a batch of candidates.
    max: 1
    # A weekly schedule would otherwise re-file the same finding (and start a
    # duplicate agent session) every run. Titles name the offending rule, so
    # normalized exact matches against open and recently-closed issues are
    # dropped. Fuzzy matches could suppress an unrelated finding whose
    # issue title would not reconcile with the pending row.
    deduplicate-by-title: true

---

# Weekly CI test-selection audit

Audit Aspire's dynamic test selection for pull requests and find the
**single highest-confidence** case where the selector ran **ALL tests**
unnecessarily or a narrow selection missed a real test consumer. If you
find one, file an issue describing the fix.

The issue you file is automatically assigned to the Copilot coding agent,
which will implement and validate the fix and open a pull request for human
review. So the issue is not a report — it is a **task specification** for
another agent, and filing one commits real review effort. Do not make any
code changes yourself.

## Scope and data sources

- Lookback: the last `${{ github.event.inputs.lookback_days }}` days of pull
  requests and CI runs, or **14 days** if that input is empty. The window is
  deliberately wider than the weekly cadence so a late CI completion, a
  rerun, or one missed audit receives another chance to be observed.
  Before collection, deterministic compaction removes raw processed rows
  observed more than 14 days ago and recomputes active watch counts, so
  the default overlap does not double-count or grow raw memory indefinitely.
  The collector also rejects automatic-scope selection artifacts older than
  the requested lookback, so an expired identity cannot make stale evidence
  look newly observed. Settled dispositions remain durable. If
  `${{ github.event.inputs.pr_numbers }}` is set, analyze only those PRs
  (ignore the lookback window for both selecting PRs and finding their
  completed CI runs; still use it as context when useful). This explicit mode
  is a targeted re-audit and intentionally bypasses the selection-age check.
- Primary evidence is the deterministic collector output at
  `$RUNNER_TEMP/gh-aw/test-selection-audit/evidence.json`. This path is
  mounted read-only into the agent. Summarize its statuses and ALL/narrow
  split with `yq` before opening individual records; do not load the whole
  file into one tool response. It already enumerates the requested PR
  scope, resolves the latest CI run and selection-job attempt, paginates
  artifacts, bounds bytes, validates the schema, and normalizes selection
  data. Use it instead of repeating collection or substituting PR comments.
- A record is creditable only when `selection.creditable` is `true` and
  `selection.status` is `resolved`. Other statuses are explicit data
  gaps, including `selection-outside-lookback`, except `recorded`: that
  status means the latest run/attempt exactly matches the existing processed
  row, so reuse that row without changing its counters or re-running the
  analysis. In particular, a
  fork's artifact is produced by PR-authored workflow, action, and
  selector code: its signed download URL proves transport, not truth.
  The collector validates the artifact only to classify it as
  `untrusted-fork-artifact`, then withholds its result. It cannot update
  counters, support an issue, or authorize persistent memory. Report the
  data gap separately; the current collector does not implement an
  independent corroboration path that can make it creditable.

## Audit procedure

1. **Load what previous runs already know.** Persistent memory for this
   workflow is mounted at `/tmp/gh-aw/repo-memory/default/`. Read these two
   files if they exist (on the very first run they will not — that is
   normal, treat both as empty and carry on):

   Before anything else, **reconcile any `pending-filed` row** in
   `watchlist.jsonl` (see below): search issues (`search_issues`, any
   state, no date bound) using the title stored in that row's `note`.
   Compare the returned issues' actual titles after lowercasing and
   collapsing whitespace, and verify their bodies describe the same
   path and, for an under-selection, the same missing target. The
   `create-issue` handler sanitizes titles, adds a prefix, and
   deduplicates normalized titles against open and recently closed
   issues; search results alone are not proof of identity. If several
   issues match, use the one for this finding and the most recent filing,
   not an unrelated old issue. Record its number in `ref` and mark the
   row `filed`, remove its `note`, and keep the issue number in `ref`
   (including when deduplication reused a matching issue).
   If the search is incomplete, fails, or yields ambiguous candidates,
   leave the row pending and report the gap. Only revert an unreconciled
   row to `watch` after a *subsequent* run completes a reliable search
   with no matching issue; when reverting it, remove `note` and keep
   `ref` null. Do not infer failure from an unavailable search or a
   differently formatted title.

   - `processed-runs.jsonl` — a **rolling contribution ledger**, one row
     per resolved `pr`+full `sha`, recording the last selection evidence
     and the exact over-selection paths and under-selection edges credited
     to that head:
     `{"pr":20131,"sha":"<full head SHA>","run":35802294466,"attempt":2,"all":false,"over_paths":[],"miss_edges":[{"path":"<literal input path>","target":"job:extension-e2e"}],"seen":"2026-09-22"}`.
     Use distinct literal paths and distinct `(path, target)` edges,
     not rule globs or an `example_prs` list. An unaffected selection
     has empty arrays. A single head contributes at most **one** to each
     path's over-selection counter and each missing edge's under-selection
     counter, even if it has several CI attempts. The `run` and
     `attempt` identify the selection job whose output you used, not
     the audit workflow's run.

     **`pr` and the full head `sha` are required on every new row.** Key
     on both: a PR gains commits, and the same commit can be reselected
     on another CI run or attempt (including a transient merge-base
     fail-safe becoming a narrow selection on rerun). Use the full head
     SHA, run ID, and attempt from the deterministic evidence. If any
     identity field is unavailable, leave the head unresolved rather
     than write an identity that could make a later run skip it.
     Set `seen` to the protected evidence file's `auditDate`; do not use
     the wall clock or preserve an older date when replacing a row from a
     newer creditable attempt.

     A deterministic pre-agent step retains only rows whose `seen` date is
     inside the current lookback window (14 days by default). Do not
     manually prune or omit additional identities: the retained rows are
     the exact source of truth for rolling counts and rerun replacement.
     If a write would exceed the configured size or patch limit, report
     the capacity failure prominently, do not write incomplete ledgers,
     and file no issue until the memory capacity is explicitly addressed.

   - `watchlist.jsonl` — the rules worth continuing to watch. Key
     over-selection rows on the literal `path` plus `kind`. Key
     under-selection rows on the literal `path`, `kind`, and missing
     `target` (a `test:<project>` or `job:<job>`). A broad rule can match
     files with different effects, and one file can miss two independent
     consumers: neither a verdict nor a fix for one edge settles the
     other. Record the matching trigger-map rule separately:
     `{"path":".github/workflows/build.yml","rule":".github/workflows/**","rule_ref":"eng/github-ci/test-trigger-map.yml@a1b2c3d","path_ref":".github/workflows/build.yml@e4f5a6b","kind":"over-selection","verdict":"watch","all_runs":12,"first_seen":"2026-09-08","last_seen":"2026-09-22","example_prs":[20131,20046],"ref":null}`.
     For an under-selection row add `"target":"job:extension-e2e"`,
     `"consumer_refs":["<consumer source path>@a1b2c3d","<eligibility source path>@e4f5a6b"]`,
     and `miss_runs` instead of `all_runs`. The refs identify the source
     proving the runtime edge **and** the test or job eligibility (for
     example a scheduling trait or job gate); use the actual files
     consulted, not these example names.

     `kind` is `over-selection` (the path escalates to ALL) or
     `under-selection` (the effective selection misses a real runtime-only
     consumer, per step 7). It picks which counter the row tracks:
     `all_runs` for `over-selection` rows counts escalations to ALL;
     `miss_runs` for `under-selection` rows counts **distinct affected
     PRs/commits** currently credited to that exact missing edge —
     never increment it just because step 7's static source analysis
     still finds the same gap it found last week. Never mix the two
     counters on one row.

     `rule_ref` is the trigger-map file and the short commit SHA it was
     last read at when this verdict was set. `path_ref` is the *triggering
     path itself* and the short commit SHA it was last read at — track
     both, since a `correct-by-design` verdict for a workflow/action often
     turns on what that file currently runs (step 6's self-referential
     judgment, or the single-job-gate case in step 6), not just on the
     trigger-map rule that selected it; if the workflow later changes what
     it gates while the trigger-map rule stays untouched, `rule_ref` alone
     would look unchanged and the stale verdict would suppress the path
     indefinitely. For under-selection, `consumer_refs` also need to
     match current source, including the test's execution lane. If a referenced
     consumer or its eligibility changed, the edge may no longer exist
     even though the trigger map and triggering input did not move.

     `verdict` is one of:

     - `watch` — a plausible candidate that has not yet cleared the
       confidence bar. Keep accumulating evidence within the rolling
       lookback window; deterministic compaction removes it when no
       credited observation remains.
     - `correct-by-design` — settled; stop re-deriving it. Compaction
       retains this disposition even after its rolling count reaches zero.
     - `pending-filed` — this run asked `create-issue` to file it, but
       `create-issue` runs in a separate job after this one finishes, so
       the agent never learns the resulting issue number or whether
       filing even succeeded (it can be silently dropped by
       `deduplicate-by-title`, or filing itself can fail; a missing
       assignment PAT does not prove no issue was filed).
       Put the intended final issue title (including `create-issue`'s
       `[test-selection-audit] ` prefix) in `note`, and keep it short and
       plain as specified below so sanitization cannot change it.
       Do not write `filed` directly — there is no confirmed issue
       number to put in `ref` yet.
     - `filed` — a prior `pending-filed` row was confirmed against a real
       issue (see step 1). Put the issue number in `ref`.
     - `in-flight` — someone else is already fixing it (see step 9). Put
       the PR number in `ref`. Do not record this as `filed`: the two
       decay differently, since an in-flight PR can be closed unmerged and
       the rule then returns to `watch`, whereas a filed issue stays ours.
     - `fixed` — the referenced fix has merged. Retain the disposition and
       source references even after its rolling count reaches zero. Put the
       merged fix's PR number in `ref`; re-evaluate if the rule changes
       again.

     Use `correct-by-design` for anything the prompt tells you to reject as
     intended behavior rather than as weak evidence — a file on the
     build-input list in step 5, or a self-referential selector change in
     step 6. Those are settled, not still being watched.

     Only create a row for a path actually observed escalating (or
     a distinct missing edge, for `under-selection`) in this audit's
     window or explicit `pr_numbers` scope. Do not seed
     a row for a path you merely noticed sharing a fix with an observed
     one. A rerun can retract the last active contribution and remove a
     zero-count `watch` row; settled dispositions remain durable.

     Carry these forward rather than re-deriving them. For a row recorded
     `correct-by-design`, skip re-reading the trigger map and the
     triggering path's history only after confirming `rule_ref` and
     `path_ref` still match their current commits. For **any** settled
     under-selection verdict (`correct-by-design`, `filed`, `in-flight`,
     or `fixed`), also confirm every `consumer_refs` commit still
     matches and the referenced test/job still runs in the relevant PR
     lane. Missing refs or any change makes that verdict stale: re-derive
     the edge and its status from current source before suppressing it.
     An unchanged CI rerun alone adds no new counter contribution.

   The watchlist is the point of this memory. A rule that escalates to ALL
   once may be weak evidence, while repeated escalations across PR heads
   in the rolling window are a stronger signal. Settled dispositions
   survive after their raw observations expire so known cases are not
   repeatedly re-investigated.
2. **Use the collected evidence.** Filter
   `$RUNNER_TEMP/gh-aw/test-selection-audit/evidence.json` by status and
   ALL/narrow result, then inspect every record within each relevant group.
   Do not enumerate PRs, runs, jobs, comments, or artifacts again. The
   collector records the current head SHA, latest CI run/attempt, normalized
   changed and excluded paths, selected tests/jobs, and explicit gap status.

   `recorded` means the exact latest attempt is already represented. A
   newer pending, blocked, missing, invalid, truncated, untrusted, or
   `selection-outside-lookback` record cannot replace that prior contribution.

   Work in two analytical passes: classify creditable `ALL` results first,
   then inspect creditable narrow results for runtime-only consumers in
   step 7. A narrow selection is not proof that every consumer was covered.
   Report every non-creditable status as a data gap; never turn a gap into
   a finding or a processed row. If `enumerationTruncated` or a record's
   pagination flags are true, file no issue because the audit scope is
   incomplete.
3. **Classify.** Group `ALL` selections by the triggering file/path/rule.
   Quantify frequency (how many PRs/runs hit each trigger) and keep 2-3
   concrete example PRs per trigger.

   First, exclude any selection whose `escalationReason` is the
   `run-full-ci` label kill switch (`"kill switch: the run-full-ci label
   forces the full matrix"`, or a caller-supplied override of that same
   switch — see `tools/SelectTests/TestSelector.cs`). That is a human
   deliberately forcing the full matrix, not a trigger-map defect;
   treat it as `correct-by-design` and never count it toward a rule's
   escalation total. This is distinct from the merge-base fail-safe
   fallback, which uses its own reason text and is real evidence — do not
   over-broaden this exclusion to match on "kill switch" or `ForceAll`
   generically.

   Stage each resolved head's distinct `over_paths`; wait until step 7 also
   determines `miss_edges` before rebuilding the watchlist in step 13. An
   ALL-to-narrow rerun replaces, rather than adds to, that head's prior
   contribution. A force-all result without a selection-time diff, including
   a merge-base fail-safe, has no triggering path and cannot be attributed to
   a rule just to make totals grow.
4. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.

   A general rationale such as "checkout normalization can affect tests"
   is not proof that ALL is minimal. Narrowing a byte-affecting metadata
   file requires a guard over its **complete** allowed directive set that
   fails on any new directive, not a test that pins only today's known
   values. If an exhaustive reject-anything-new guard is not practical,
   keep the path routed to ALL.
5. **Do not question broad build-input files.** These files legitimately
   affect nearly the entire .NET project graph — treat their `ALL`
   escalation as correct-by-design and do not flag it as a finding, even if
   it looks broad:

   `Directory.Packages.props`, `Directory.Build.props`,
   `Directory.Build.targets`, `NuGet.config`, `eng/Versions.props`,
   `eng/Version.Details.xml`, `src/Directory.Build.props`, and
   `global.json`.

   The list is exhaustive, not a category for analogy. `.gitattributes`,
   `.editorconfig`, `.config/dotnet-tools.json`, `Aspire.slnx`, and CI YAML
   still require consumer analysis. Layer 1 already evaluates additions to
   `Aspire.slnx` at the PR head; project removal is the asymmetric case
   that can still require the run-all fallback.
6. **CI YAML and composite actions are in scope.** Changes under
   `.github/workflows/**` and `.github/actions/**` are a frequent `ALL`
   trigger, and unlike build inputs they are *not* automatically
   correct-by-design. A workflow or action that gates exactly one job, or
   whose change cannot affect any test outcome at all (release gating,
   labeling, issue automation, docs publishing), is a legitimate finding —
   do not wave it through just because the rule that matched it carries a
   comment. Judge the specific file's real effect, not the rule's blurb.

   Two things to get right before proposing anything here:

   - **`.github/actions/**` -> ALL is pinned by a guard test.** The map
     routes every local composite action to ALL, and
     `TestTriggerMapTests.EveryLocalActionUsedByAWorkflowIsRoutedToAll`
     asserts that every action referenced by any workflow stays routed that
     way. So a narrowing here is not a one-line map edit: your suggested fix
     must say explicitly how that test's contract changes (for example, a
     documented exception list the test honors) and must treat updating the
     test as part of the work. If you cannot describe that coherently, the
     candidate fails the confidence bar.
   - **A self-referential ALL is correct.** If the PR modified the selector
     itself — `tools/SelectTests`, `eng/github-ci/test-trigger-map.yml`, or
     the select-tests action/workflow — then running ALL is the intended
     safety behavior, not an over-selection bug. Reject those.
7. **Also look for under-selection, not just ALL.** A rule that already
   names specific `targets` can still be wrong in the opposite direction:
   its target list can be narrower than the file's real consumers, so a
   change silently runs too few tests instead of falling back to ALL. This
   is the more dangerous failure mode, because nothing in the selection
   comment looks anomalous — the selector reports a confident, narrow
   selection, and a missed test does not show up as a `.gitattributes`-style
   over-broad rule would. Treat a candidate here with **at least** the same
   rigor as an over-selection one, and weigh it higher when both are
   equally well-evidenced: a missed test risks a real regression escaping,
   where an extra CI run only costs compute.

   This check is not driven by which PRs selected ALL this window — an
   under-selecting result never shows up that way. Start with the
   selection-time changed **and prefiltered** paths of narrow-result
   PRs, including ones with no `path_rules` entry. For an explicit
   `pr_numbers` dispatch, use those PRs even outside the window.
   Independently search the repository's
   runtime-only consumer sites (package loads, generated AppHosts,
   copied fixtures, polyglot codegen contracts, extension RPC and CI
   job inputs) for inputs among those paths. Bound the search to the
   paths changed in this audit scope and prioritize these cross-cutting
   surfaces:

   - `src/Aspire.Hosting/**` — core orchestration APIs every hosting
     integration and the CLI's generated-AppHost path build on.
   - `src/Aspire.TypeSystem/**` and `src/Aspire.Hosting.CodeGeneration.*/**`
     — changes ripple into every polyglot language exporter (Go, Java,
     Python, Rust, TypeScript) and the generated SDK contract.
   - `src/Aspire.Dashboard/**` — Blazor components plus their JS interop.
   - `extension/**` — the VS Code extension (bootstrap, RPC bridge, e2e).
   - the CLI (`src/Aspire.Cli/**`, acquisition scripts, native archive
     packaging).

   For each matching input, enumerate its real consumers from source —
   search for package references, generated-code call sites, file
   copies, or RPC/protocol message types it defines — rather than
   trusting the trigger map's `reason` comment or using its rules as
   the list of inputs. Also inspect `path_rules`,
   `affected_project_rules`, and `derived_targets` on these surfaces,
   but remember the latter two are keyed on projects/selected tests,
   not file paths: they cannot by themselves reveal an entirely absent
   runtime-only edge. Most compiled C#
   dependencies here are Layer 1's job (the project graph is exhaustive
   for MSBuild project references) and do not need this check; focus on
   exactly the blind spots Layer 2 exists to cover — a runtime-only
   dependency such as a package loaded by `aspire add`, a generated
   AppHost, a fixture copied into an E2E workspace, or a contract read by
   a codegen target that Layer 1's static graph cannot see. Check the
   **effective** selected tests and jobs for each changed input,
   including Layer 1, conventions, `path_rules`,
   `affected_project_rules`, `derived_targets`, `ignore`, and
   prefilter. A prefiltered input never reaches either layer; check
   `excludedFiles` as well as `changedFiles` before dismissing it.
   An input with no explicit path rule can be covered by
   another mechanism; an unmatched input may force `ALL` instead,
   which is an over-selection, **not** a missed target. Only a narrow
   result omitting a real eligible consumer is an under-selection.
   Name the specific `test:<project>` or `job:<job>` missing and cite
   the source reference (file:line) proving that runtime dependency
   and the test/job's PR execution lane.

   For each example head, verify **at the time of that selection** that
   the changed input, consumer edge, PR-eligible target, map omission,
   and effective selected set all coexisted. The deterministic evidence
   establishes the selection-time head, changed inputs, and selected
   targets; use historical repository source at that head/base for the
   consumer, eligibility, and trigger-map claims. Do not treat current
   main, today's PR diff, or current call sites as historical proof. If
   any part cannot be reconstructed, report an unverified candidate
   without crediting a miss or filing it. Confirm separately that the
   gap still exists on current main before proposing a fix.

   When re-evaluating a head, check every exact edge previously in its
   `miss_edges`. Stage the distinct `(literal path, missing target)`
   edges only when that attempt's result omitted a proven consumer.
   Compare these with its prior credits in step 13; do not use
   `example_prs` to deduplicate counts, because it does not track SHAs.
   A static map
   omission without a matching new PR is still worth reporting in the
   run summary, but supplies neither a `miss_runs` increment nor the
   concrete example required to file an issue.
8. **Verify against source, not memory.** For every candidate, read the
   actual selector implementation, `eng/github-ci/test-trigger-map.yml`,
   `docs/ci/test-trigger-map.md`, and the real changed-file list from the
   example PRs before concluding the selection is wrong. Do not speculate
   about what a file "probably" affects.
9. **Check whether a fix is already in flight.** Before going further with a
   candidate, check whether someone is already fixing it:

   - `search_pull_requests` only searches issue-style metadata (title,
     body, labels) — it cannot see a PR's changed files, so an open PR
     that edits the trigger map without naming it or the rule in its
     title/body would be missed. List open PRs
     (`list_pull_requests`, `state: open`) and check each one's changed
     files (`get_pull_request_files`) for `eng/github-ci/test-trigger-map.yml`;
     use `search_pull_requests` in addition, for PRs that name the rule by
     text but might not (yet) touch the file.
   - Check `watchlist.jsonl` for an `in-flight`, `filed`, or `fixed`
     verdict against this exact `(path, kind)` or, for under-selection,
     `(path, kind, target)` by an earlier run.
     Confirm whether a referenced PR is still open, closed unmerged,
     or merged, or a filed issue still tracks the fix. An open issue,
     open PR, or merged fix that still applies blocks a duplicate.
     Reopen `watch` if an in-flight PR closed unmerged or a prior fix
     no longer applies to the current rule; remove any `note` and clear
     `ref` when doing so.

   If a fix is already tracked or merged, reject the duplicate and say so
   in the run summary. Filing anyway would start a second coding agent on work
   that is already done and put a duplicate PR in front of a reviewer.
10. **Check how this was handled before.** Maintainers have already made
   many of these decisions, and the trigger map records them. For each
   surviving candidate — not up front, and not for the whole file — use
   `list_commits` with `path: eng/github-ci/test-trigger-map.yml` and
   `get_commit` to read the commits that last touched the rule or section
   you are about to change, plus their PR discussion. Keep `perPage` small
   (5-10) — commit messages in this repository are long, and a wide page
   costs far more context than it returns. Use this for three things:

   - **Pick an existing fix shape.** The map has distinct mechanisms —
     `prefilter`, `ignore`, `path_rules`, `affected_project_rules`,
     `derived_targets`, and `groups` — and they are not interchangeable.
     In particular, when a file genuinely cannot affect any test outcome,
     the established fix is an `ignore:` entry with a comment saying why
     (e.g. "Layer 1 covers", "no GH-CI consumer"), **not** a narrowed
     `path_rules` target. Match the surrounding comment convention,
     including its habit of documenting deliberate *non*-entries.
   - **Respect recorded failures.** If history shows a rule was already
     narrowed and later widened back (or an `ignore` entry was removed),
     that is direct evidence the narrowing was wrong. Do not propose it
     again — report it in the run summary as previously-tried instead.
   - **Look for missed siblings.** If a past commit routed one consumer of
     a shared input but left sibling consumers on the fallback, that gap
     is itself a strong candidate.
11. **Apply the confidence bar.** Candidate findings include: a path rule
   broader than its actual consumers, a missing path rule that would let a
   runtime-only consumer (e.g. a test fixture, generated AppHost, or package
   copied into an E2E workspace) silently rely on the ALL fallback, an
   orphaned/renamed input that only ever hits the fallback, or a rule that
   looks unnecessary entirely (e.g. a file whose change cannot affect any
   test outcome — treat `.gitattributes`-style metadata files as an example
   of "runs everything for no functional reason" only if you have verified
   nothing in the trigger map or CI depends on it).

   File a candidate **only if all of these hold**:
   - You identified the exact rule or code path responsible, by reading it.
   - You enumerated the file's real consumers from repository source, not
     from what the name suggests.
   - You can name the specific scoped fix using a map mechanism: narrow
     an over-selection, or add the missing target/remove an incorrect
     prefilter or ignore for an under-selection.
   - You can name a test that would fail if the fix regressed: an
     exhaustive new-directive guard as step 4 requires for narrowing
     a byte-affecting file, or an assertion that the missing target is
     selected in the correct PR lane for an under-selection.
   - If an existing guard test currently pins the behavior you want to
     change, you can state how that test's contract should change.
   - History does not show this same narrowing already being tried and
     reverted.
   - You would be comfortable defending the change in review.

   If any of those is missing, it is not high-confidence. Report it in the
   run summary instead.
12. **Pick one, or none.** If several candidates clear the bar, file only the
   strongest — prioritize a proven missed consumer over comparable CI
   savings, then weigh the clarity of the evidence and the number of
   affected PRs or avoidable `ALL` runs. If none clear it, file nothing.
   A run that files no issue is a
   normal, successful run; filing a weak finding is worse than filing
   nothing, because it starts a coding agent session and consumes human
   review time.
13. **Write back what you learned.** Update the two ledgers in
    `/tmp/gh-aw/repo-memory/default/`; gh-aw commits them automatically.

    - For each **new, verified** selection result, finish both over- and
      under-selection analysis before replacing or appending its exact
      `pr`+full `sha` row in `processed-runs.jsonl`. Store the evidence run,
      attempt, distinct `over_paths`, distinct `miss_edges`, and protected
      `auditDate`. Reuse `recorded` rows byte-for-byte. If a newer attempt is
      pending, blocked, missing, truncated, invalid, untrusted, or outside the
      automatic lookback, preserve the prior row and do not treat it as current
      evidence.
    - After the processed rows are final, make each watch counter equal the
      number of retained processed identities that credit that exact path or
      `(path, target)` edge. One head contributes at most once. Rebuild
      `example_prs` from currently credited heads, cap it at three distinct
      PR numbers, and derive active `first_seen`/`last_seen` dates from those
      rows. Remove zero-count `watch` rows; retain settled dispositions at
      zero so known decisions are not rediscovered.
    - Preserve a row's `verdict` and `ref` only after confirming its rule,
      triggering path, target, and consumer/eligibility refs still support
      it. Reopen `watch` when an in-flight PR closes unmerged or a previous
      fix no longer applies. Mark a merged fix `fixed` instead of deleting
      its disposition.

    Repo-memory validation independently recomputes identities, counters,
    examples, provenance, dates, and allowed lifecycle transitions. Keep
    JSONL rows one-line and schema-only. Before emitting `create-issue`,
    verify both files fit the configured file and patch limits. On any
    inconsistency or capacity failure, leave both ledgers unchanged, report
    the problem, and file no issue.

## The issue you file

If one candidate clears the confidence bar, read
`.github/workflows/test-selection-audit/issue_instructions.md` and follow
its complete task-specification and validation contract. The final title,
including the `[test-selection-audit] ` prefix stored in `note`, must be
plain ASCII, at most 100 characters, and contain only letters, digits,
spaces, periods, and hyphens.

## Run summary (always report, regardless of whether an issue was filed)

In your final response, report:

- How many PRs/runs were analyzed and over what window (or which PR numbers,
  if explicitly given), and how many heads reused a recorded result
  after comparing the deterministic run metadata. Report separately any reruns that replaced
  prior contributions and any missing/stale-attempt evidence.
- How many PRs were skipped because they could not have a selection result
  yet (CI pending, `action_required`, or no selection job), and how many
  selection artifacts were outside the automatic lookback, so a quiet window
  is distinguishable from an unanalyzable one.
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts for the retained lookback window.
- The current watchlist: each path and, for under-selection, each missing
  target being tracked; its rolling `all_runs` / `miss_runs` count,
  and how that count moved this run. Do not sum edge counts and call them
  distinct PR heads: a head may miss more than one target. A rising
  count is the audit's main product even when nothing is filed.
- Candidates you considered but rejected as correct-by-design or as failing
  the confidence bar, and which specific criterion each one failed. Call out
  separately any candidate rejected because history shows the same change
  was already tried and reverted, and any rejected because a fix is already
  in flight.
- The issue filed this run, if any, or a one-line note that no finding
  cleared the bar this week.
