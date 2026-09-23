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
  or cheaper. Per-PR results and per-input/target verdicts persist across runs in
  a memory branch, so escalation counts accumulate into cross-run
  evidence without double-counting PR heads or their reruns.
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
  - name: Prepare test-selection collector
    run: |
      mkdir -p /tmp/gh-aw/test-selection-audit
      cat > /tmp/gh-aw/test-selection-audit/collector.py <<'PY'
      import concurrent.futures
      import datetime
      import http.client
      import io
      import json
      import os
      import pathlib
      import re
      import time
      import urllib.error
      import urllib.parse
      import urllib.request
      import zipfile

      API_ROOT = "https://api.github.com"
      ARTIFACT_NAME = "select-tests-selection-Linux"
      ARTIFACT_MEMBER = "select-tests-selection.json"
      MAX_COMPRESSED_BYTES = 10 * 1024 * 1024
      MAX_EXPANDED_BYTES = 1024 * 1024
      MAX_PRS = 1000
      PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/@+#=\-]+$")
      TARGET_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
      SELECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]+$")
      SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
      TRANSIENT_NETWORK_ERRORS = (
          urllib.error.URLError,
          http.client.IncompleteRead,
          http.client.RemoteDisconnected,
          ConnectionResetError,
          TimeoutError,
      )
      TRANSIENT_API_ERRORS = TRANSIENT_NETWORK_ERRORS + (json.JSONDecodeError,)
      COLLECTOR_RECORD_ERRORS = (ValueError,) + TRANSIENT_NETWORK_ERRORS

      token = os.environ["GH_TOKEN"]
      repository = os.environ["REPOSITORY"]
      output_path = pathlib.Path(os.environ["OUTPUT_PATH"])
      baseline_path = pathlib.Path(os.environ["PROCESSED_BASELINE_PATH"])
      authorization_prefix = bytes((66, 101, 97, 114, 101, 114, 32)).decode("ascii")

      def parse_timestamp(value):
          if not value:
              return None
          return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))

      def request(path, parameters=None):
          url = f"{API_ROOT}{path}"
          if parameters:
              url += "?" + urllib.parse.urlencode(parameters)
          for attempt in range(3):
              request_value = urllib.request.Request(
                  url,
                  headers={
                      "Accept": "application/vnd.github+json",
                      "Authorization": authorization_prefix + token,
                      "X-GitHub-Api-Version": "2022-11-28",
                      "User-Agent": "aspire-audit",
                  },
              )
              try:
                  with urllib.request.urlopen(request_value, timeout=30) as response:
                      return json.loads(response.read())
              except urllib.error.HTTPError as error:
                  if error.code != 429 and error.code < 500:
                      raise
                  if attempt == 2:
                      raise
              except TRANSIENT_API_ERRORS:
                  if attempt == 2:
                      raise
              time.sleep(2 ** attempt)
          raise RuntimeError("unreachable")

      def paginate(path, parameters=None, key=None, max_pages=20):
          result = []
          parameters = dict(parameters or {})
          parameters["per_page"] = 100
          for page in range(1, max_pages + 1):
              parameters["page"] = page
              payload = request(path, parameters)
              values = payload[key] if key else payload
              if not isinstance(values, list):
                  raise ValueError(f"{path} did not return a list")
              result.extend(values)
              if len(values) < 100:
                  return result, False
          return result, True

      def require_safe_string(value, pattern, context, maximum=400):
          if not isinstance(value, str) or not value or len(value) > maximum or not pattern.fullmatch(value):
              raise ValueError(f"{context} is invalid")
          return value

      def normalize_string_list(value, pattern, context, maximum_items=5000):
          if not isinstance(value, list) or len(value) > maximum_items:
              raise ValueError(f"{context} is not a bounded array")
          result = []
          seen = set()
          for index, item in enumerate(value):
              item = require_safe_string(item, pattern, f"{context}[{index}]")
              if item in seen:
                  raise ValueError(f"{context} contains duplicate {item}")
              seen.add(item)
              result.append(item)
          return sorted(result)

      def normalize_named_items(value, context):
          if not isinstance(value, list) or len(value) > 5000:
              raise ValueError(f"{context} is not a bounded array")
          result = []
          seen = set()
          for index, item in enumerate(value):
              if not isinstance(item, dict):
                  raise ValueError(f"{context}[{index}] is not an object")
              name = require_safe_string(
                  item.get("name"), SELECTION_NAME_PATTERN, f"{context}[{index}].name", 200
              )
              if name in seen:
                  raise ValueError(f"{context} contains duplicate {name}")
              seen.add(name)
              result.append(name)
          return sorted(result)

      def normalize_selection(payload, include_reason):
          if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
              raise ValueError("selection artifact has an unsupported schema")
          inputs = payload.get("inputs")
          if not isinstance(inputs, dict) or not isinstance(inputs.get("changeSource"), str):
              raise ValueError("selection artifact inputs are invalid")
          change_source = inputs["changeSource"]
          diff_match = re.fullmatch(r"git diff ([0-9a-f]{40})\.\.([0-9a-f]{40})", change_source)
          if diff_match:
              source_base_sha, source_head_sha = diff_match.groups()
          elif change_source == "(none -- force-all or unset)":
              source_base_sha = None
              source_head_sha = None
          else:
              raise ValueError("selection artifact changeSource is unsupported")
          selects_all = payload.get("selectsAll")
          if not isinstance(selects_all, bool):
              raise ValueError("selection artifact selectsAll is not boolean")
          reason = None
          if include_reason and payload.get("escalationReason") is not None:
              if not isinstance(payload["escalationReason"], str):
                  raise ValueError("selection artifact escalationReason is not a string")
              reason = payload["escalationReason"]
              if len(reason) > 500 or any(ord(character) < 32 for character in reason):
                  raise ValueError("selection artifact escalationReason is invalid")
          return {
              "selectsAll": selects_all,
              "sourceBaseSha": source_base_sha,
              "sourceHeadSha": source_head_sha,
              "escalationReason": reason,
              "changedFiles": normalize_string_list(payload.get("changedFiles"), PATH_PATTERN, "changedFiles"),
              "excludedFiles": normalize_string_list(payload.get("excludedFiles"), PATH_PATTERN, "excludedFiles"),
              "unattributedFiles": normalize_string_list(payload.get("unattributedFiles"), PATH_PATTERN, "unattributedFiles"),
              "testProjects": normalize_named_items(payload.get("testProjects"), "testProjects"),
              "jobs": normalize_named_items(payload.get("jobs"), "jobs"),
          }

      class ArtifactRedirectHandler(urllib.request.HTTPRedirectHandler):
          def redirect_request(self, request_value, file_pointer, code, message, headers, new_url):
              redirected = super().redirect_request(
                  request_value, file_pointer, code, message, headers, new_url
              )
              if redirected and urllib.parse.urlsplit(new_url).netloc != "api.github.com":
                  redirected.remove_header("Authorization")
              return redirected

      def download_selection(artifact_id, include_reason):
          path = f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"
          compressed = None
          for attempt in range(3):
              request_value = urllib.request.Request(
                  f"{API_ROOT}{path}",
                  headers={
                      "Accept": "application/vnd.github+json",
                      "Authorization": authorization_prefix + token,
                      "X-GitHub-Api-Version": "2022-11-28",
                      "User-Agent": "aspire-audit",
                  },
              )
              try:
                  current = bytearray()
                  opener = urllib.request.build_opener(ArtifactRedirectHandler())
                  with opener.open(request_value, timeout=30) as response:
                      while True:
                          chunk = response.read(65536)
                          if not chunk:
                              break
                          current.extend(chunk)
                          if len(current) > MAX_COMPRESSED_BYTES:
                              raise ValueError("selection artifact exceeds the compressed-byte limit")
                  compressed = current
                  break
              except urllib.error.HTTPError as error:
                  if error.code != 429 and error.code < 500:
                      raise
                  if attempt == 2:
                      raise
              except TRANSIENT_NETWORK_ERRORS:
                  if attempt == 2:
                      raise
              time.sleep(2 ** attempt)
          if compressed is None:
              raise RuntimeError("unreachable")
          with zipfile.ZipFile(io.BytesIO(compressed)) as archive:
              members = [entry for entry in archive.infolist() if entry.filename == ARTIFACT_MEMBER]
              if len(members) != 1:
                  raise ValueError(f"selection artifact contains {len(members)} exact members")
              member = members[0]
              if member.is_dir() or member.file_size > MAX_EXPANDED_BYTES:
                  raise ValueError("selection artifact member exceeds the expanded-byte limit")
              with archive.open(member) as stream:
                  expanded = stream.read(MAX_EXPANDED_BYTES + 1)
              if len(expanded) > MAX_EXPANDED_BYTES:
                  raise ValueError("selection artifact member exceeded the expanded-byte limit while reading")
          try:
              payload = json.loads(expanded)
          except (UnicodeDecodeError, json.JSONDecodeError) as error:
              raise ValueError("selection artifact member is not valid UTF-8 JSON") from error
          return normalize_selection(payload, include_reason)

      PY
  - name: Collect test-selection evidence
    env:
      GH_TOKEN: ${{ github.token }}
      REPOSITORY: ${{ github.repository }}
      LOOKBACK_DAYS: ${{ github.event.inputs.lookback_days }}
      PR_NUMBERS: ${{ github.event.inputs.pr_numbers }}
      OUTPUT_PATH: /tmp/gh-aw/test-selection-audit/evidence.json
      PROCESSED_RUNS_PATH: /tmp/gh-aw/repo-memory/default/processed-runs.jsonl
      PROCESSED_BASELINE_PATH: /tmp/gh-aw/test-selection-audit/processed-runs-before.jsonl
    run: |
      cat >> /tmp/gh-aw/test-selection-audit/collector.py <<'PY'
      def list_pull_requests():
          explicit_text = os.environ.get("PR_NUMBERS", "").strip()
          if explicit_text:
              numbers = []
              for item in explicit_text.split(","):
                  item = item.strip()
                  if not re.fullmatch(r"[1-9][0-9]*", item):
                      raise ValueError(f"invalid PR number {item!r}")
                  number = int(item)
                  if number not in numbers:
                      numbers.append(number)
              pull_requests = []
              for number in numbers:
                  try:
                      pull_requests.append(request(f"/repos/{repository}/pulls/{number}"))
                  except Exception as error:
                      pull_requests.append({"number": number, "_collectorError": type(error).__name__})
              return pull_requests, False

          lookback_text = os.environ.get("LOOKBACK_DAYS", "").strip() or "14"
          if not re.fullmatch(r"[1-9][0-9]*", lookback_text):
              raise ValueError("lookback_days must be a positive integer")
          lookback_days = int(lookback_text)
          if lookback_days > 90:
              raise ValueError("lookback_days must not exceed 90")
          cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)
          pull_requests = []
          truncated = False
          for page in range(1, 11):
              values = request(
                  f"/repos/{repository}/pulls",
                  {
                      "state": "all",
                      "sort": "updated",
                      "direction": "desc",
                      "per_page": 100,
                      "page": page,
                  },
              )
              if not isinstance(values, list):
                  raise ValueError("pull request listing did not return an array")
              for pull_request in values:
                  if pull_request.get("state") == "open":
                      scope_timestamp = parse_timestamp(pull_request.get("updated_at"))
                  else:
                      scope_timestamp = max(
                          timestamp
                          for timestamp in (
                              parse_timestamp(pull_request.get("created_at")),
                              parse_timestamp(pull_request.get("closed_at")),
                              parse_timestamp(pull_request.get("merged_at")),
                          )
                          if timestamp is not None
                      )
                  if scope_timestamp >= cutoff:
                      pull_requests.append(pull_request)
              if len(values) < 100:
                  break
              if parse_timestamp(values[-1].get("updated_at")) < cutoff:
                  break
          else:
              truncated = True
          if len(pull_requests) > MAX_PRS:
              pull_requests = pull_requests[:MAX_PRS]
              truncated = True
          return pull_requests, truncated

      def load_processed_index():
          processed_path = pathlib.Path(os.environ["PROCESSED_RUNS_PATH"])
          baseline_path.parent.mkdir(parents=True, exist_ok=True)
          if not processed_path.exists():
              baseline_path.write_text("", encoding="utf-8")
              baseline_path.chmod(0o444)
              return {}
          raw = processed_path.read_bytes()
          if len(raw) > 2 * 1024 * 1024:
              raise ValueError("processed-runs.jsonl exceeds the configured memory limit")
          baseline_path.write_bytes(raw)
          baseline_path.chmod(0o444)
          index = {}
          for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
              if not line:
                  continue
              row = json.loads(line)
              if (
                  not isinstance(row, dict)
                  or not isinstance(row.get("pr"), int)
                  or not isinstance(row.get("sha"), str)
                  or not SHA_PATTERN.fullmatch(row["sha"])
                  or not isinstance(row.get("run"), int)
                  or not isinstance(row.get("attempt"), int)
              ):
                  raise ValueError(f"processed-runs.jsonl:{line_number} has an invalid identity")
              key = (row["pr"], row["sha"])
              if key in index:
                  raise ValueError(f"processed-runs.jsonl:{line_number} duplicates {row['pr']}:{row['sha']}")
              index[key] = (row["run"], row["attempt"])
          return index

      def list_changed_files(number):
          values, truncated = paginate(
              f"/repos/{repository}/pulls/{number}/files",
              max_pages=30,
          )
          paths = []
          for index, value in enumerate(values):
              paths.append(require_safe_string(value.get("filename"), PATH_PATTERN, f"PR {number} file {index}"))
          return sorted(set(paths)), truncated

      def find_selection_record(pull_request):
          number = pull_request["number"]
          if "_collectorError" in pull_request:
              return {
                  "pr": number,
                  "headSha": None,
                  "isFork": None,
                  "selection": {"status": "collector-error", "error": pull_request["_collectorError"]},
              }
          head_sha = require_safe_string(pull_request["head"]["sha"], SHA_PATTERN, f"PR {number} head SHA", 40)
          head_repo = (pull_request.get("head", {}).get("repo") or {}).get("full_name")
          head_ref = pull_request.get("head", {}).get("ref")
          is_fork = head_repo != repository
          record = {
              "pr": number,
              "headSha": head_sha,
              "isFork": is_fork,
              "selection": {"status": "unresolved"},
          }
          runs, runs_truncated = paginate(
              f"/repos/{repository}/actions/runs",
              {"event": "pull_request", "head_sha": head_sha},
              key="workflow_runs",
              max_pages=5,
          )
          relevant_runs = [
              run for run in runs
              if run.get("path") == ".github/workflows/ci.yml"
              and run.get("head_sha") == head_sha
              and (run.get("head_repository") or {}).get("full_name") == head_repo
              and run.get("head_branch") == head_ref
          ]
          relevant_runs.sort(key=lambda run: (run.get("created_at") or "", run.get("run_attempt") or 0), reverse=True)
          if not relevant_runs:
              record["selection"] = {"status": "no-ci-run", "runsTruncated": runs_truncated}
              return record

          run = relevant_runs[0]
          selection = {
              "status": "unresolved",
              "run": run["id"],
              "attempt": run.get("run_attempt"),
              "runsTruncated": runs_truncated,
          }
          record["selection"] = selection
          if run.get("status") != "completed":
              selection["status"] = "pending"
              return record
          if run.get("conclusion") == "action_required":
              selection["status"] = "action-required"
              return record
          if processed_index.get((number, head_sha)) == (run["id"], run.get("run_attempt")):
              selection["status"] = "recorded"
              return record

          associated, associations_truncated = paginate(
              f"/repos/{repository}/commits/{head_sha}/pulls",
              max_pages=3,
          )
          matching_prs = [
              value for value in associated
              if value.get("head", {}).get("sha") == head_sha
              and value.get("head", {}).get("ref") == head_ref
              and (value.get("head", {}).get("repo") or {}).get("full_name") == head_repo
          ]
          if associations_truncated or len(matching_prs) != 1 or matching_prs[0].get("number") != number:
              selection["status"] = "pr-attribution-ambiguous"
              return record

          changed_files, files_truncated = list_changed_files(number)
          record["changedFilesFromGitHub"] = changed_files
          record["filesTruncated"] = files_truncated
          jobs, jobs_truncated = paginate(
              f"/repos/{repository}/actions/runs/{run['id']}/jobs",
              {"filter": "latest"},
              key="jobs",
              max_pages=10,
          )
          selection_jobs = [
              job for job in jobs
              if job.get("name", "").endswith("Tests / Setup for tests")
              and job.get("run_attempt") == run.get("run_attempt")
          ]
          if len(selection_jobs) != 1:
              selection["status"] = "selection-job-ambiguous" if selection_jobs else "no-selection-job"
              selection["jobsTruncated"] = jobs_truncated
              return record
          job = selection_jobs[0]
          selection["jobsTruncated"] = jobs_truncated
          if job.get("status") != "completed":
              selection["status"] = "pending"
              return record
          steps = [step for step in job.get("steps", []) if step.get("name") == "Select relevant tests"]
          if len(steps) != 1 or steps[0].get("conclusion") != "success":
              selection["status"] = "selection-step-failed"
              return record
          select_started = parse_timestamp(steps[0].get("started_at"))
          job_completed = parse_timestamp(job.get("completed_at"))

          artifacts, artifacts_truncated = paginate(
              f"/repos/{repository}/actions/runs/{run['id']}/artifacts",
              key="artifacts",
              max_pages=20,
          )
          candidates = []
          for artifact in artifacts:
              workflow_run = artifact.get("workflow_run") or {}
              created_at = parse_timestamp(artifact.get("created_at"))
              if (
                  artifact.get("name") == ARTIFACT_NAME
                  and workflow_run.get("id") == run["id"]
                  and workflow_run.get("head_sha") == head_sha
                  and created_at is not None
                  and select_started is not None
                  and job_completed is not None
                  and select_started <= created_at <= job_completed
              ):
                  candidates.append(artifact)
          selection["artifactsTruncated"] = artifacts_truncated
          if len(candidates) != 1:
              selection["status"] = "artifact-ambiguous" if candidates else "artifact-missing"
              return record
          artifact = candidates[0]
          if artifact.get("expired"):
              selection["status"] = "artifact-expired"
              return record
          try:
              normalized = download_selection(artifact["id"], include_reason=not is_fork)
          except Exception as error:
              selection["status"] = "artifact-invalid"
              selection["error"] = type(error).__name__
              return record

          source_head_matches = normalized["sourceHeadSha"] == head_sha
          selection["creditable"] = not is_fork and source_head_matches
          if is_fork:
              selection["status"] = "untrusted-fork-artifact"
              return record
          elif not source_head_matches:
              selection["status"] = "artifact-head-mismatch"
          else:
              selection["status"] = "resolved"
          selection["result"] = normalized
          return record

      def collect_selection_record(pull_request):
          try:
              return find_selection_record(pull_request)
          except COLLECTOR_RECORD_ERRORS as error:
              head = pull_request.get("head") or {}
              head_repo = (head.get("repo") or {}).get("full_name")
              return {
                  "pr": pull_request.get("number"),
                  "headSha": head.get("sha") if SHA_PATTERN.fullmatch(head.get("sha") or "") else None,
                  "isFork": head_repo != repository,
                  "selection": {
                      "status": "collector-error",
                      "error": type(error).__name__,
                  },
              }

      pull_requests, enumeration_truncated = list_pull_requests()
      processed_index = load_processed_index()
      with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
          records = list(executor.map(collect_selection_record, pull_requests))
      output = {
          "schemaVersion": 1,
          "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "repository": repository,
          "enumerationTruncated": enumeration_truncated,
          "records": records,
      }
      output_path.parent.mkdir(parents=True, exist_ok=True)
      output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
      output_path.chmod(0o444)
      print(f"Wrote {len(records)} records to {output_path}")
      PY
      python3 /tmp/gh-aw/test-selection-audit/collector.py

  # A selection is fixed for a particular CI run/attempt, not for a PR
  # head: re-runs can replace its result. `processed-runs.jsonl` retains
  # each head and its counted path contributions so a newer attempt can
  # replace, rather than add to, its earlier counts. Do not prune identities:
  # even an old head may be revisited by a focused dispatch or PR update.
  #
  # `watchlist.jsonl` is the durable half. A rule that escalates to ALL a
  # few times in one window is weak evidence, but the same rule accumulating
  # escalations week after week is worth acting on -- and that is only
  # visible if the counts survive across runs.
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
    # Defaults (100KB file / 10KB patch) are too small: the durable index
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
        const datePattern = /^\d{4}-\d{2}-\d{2}$/;
        const titlePattern = /^\[test-selection-audit\] [A-Za-z0-9 .-]{1,77}$/;

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
        const evidencePath = "/tmp/gh-aw/test-selection-audit/evidence.json";
        const baselinePath = "/tmp/gh-aw/test-selection-audit/processed-runs-before.jsonl";
        const hasProvenance = fs.existsSync(evidencePath) && fs.existsSync(baselinePath);
        const canonical = value => {
          if (Array.isArray(value)) return value.map(canonical);
          if (!isObject(value)) return value;
          return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
        };
        const baselineRows = new Map();
        const trustedSelections = new Map();
        if (hasProvenance) {
          const baselineText = fs.readFileSync(baselinePath, "utf8");
          for (const line of baselineText.split("\n")) {
            if (!line) continue;
            const row = JSON.parse(line);
            baselineRows.set(`${row.pr}:${row.sha}`, JSON.stringify(canonical(row)));
          }
          const evidence = JSON.parse(fs.readFileSync(evidencePath, "utf8"));
          for (const record of evidence.records) {
            const selection = record.selection;
            if (selection.status === "resolved" && selection.creditable === true) {
              trustedSelections.set(`${record.pr}:${record.headSha}`, selection);
            }
          }
        }
        const processedKeys = new Set();
        const overCounts = new Map();
        const missCounts = new Map();
        const overPrs = new Map();
        const missPrs = new Map();
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
          requireString(row.seen, datePattern, `${context}.seen`, 10);
          const identity = `${row.pr}:${row.sha}`;
          if (processedKeys.has(identity)) fail(`duplicate processed identity ${identity}`);
          processedKeys.add(identity);
          if (hasProvenance) {
            const selection = trustedSelections.get(identity);
            if (selection) {
              if (row.run !== selection.run ||
                  row.attempt !== selection.attempt ||
                  row.all !== selection.result.selectsAll) {
                fail(`${context} does not match trusted selection evidence`);
              }
            } else if (baselineRows.get(identity) !== JSON.stringify(canonical(row))) {
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
        }
        if (hasProvenance) {
          for (const identity of baselineRows.keys()) {
            if (!processedKeys.has(identity)) fail(`missing baseline processed row ${identity}`);
          }
          for (const identity of trustedSelections.keys()) {
            if (!processedKeys.has(identity)) fail(`missing processed row for trusted selection ${identity}`);
          }
        }

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
          requireString(row.first_seen, datePattern, `${context}.first_seen`, 10);
          requireString(row.last_seen, datePattern, `${context}.last_seen`, 10);
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
  deliberately wider than the weekly cadence: a single week's merges are
  mostly routine and tend to surface only correct-by-design escalations, so
  a one-week window produces empty runs. Overlapping windows are safe
  because processed PR heads and their counted contributions are retained
  and findings are deduplicated by title. If
  `${{ github.event.inputs.pr_numbers }}` is set, analyze only those PRs
  (ignore the lookback window for both selecting PRs and finding their
  completed CI runs; still use it as context when useful).
- Primary evidence is the deterministic collector output at
  `/tmp/gh-aw/test-selection-audit/evidence.json`. Read it before making
  GitHub calls. It already enumerates the requested PR scope, finds the
  latest CI run and selection-job attempt for each current head, paginates
  artifacts, bounds both compressed and expanded bytes, validates the
  JSON schema, and normalizes selection data. Do not repeat those
  mechanical steps or substitute PR comments for this file.
- A record is creditable only when `selection.creditable` is `true` and
  `selection.status` is `resolved`. Other statuses are explicit data
  gaps, except `recorded`: that status means the latest run/attempt
  exactly matches the existing processed row, so reuse that row without
  changing its counters or re-running the analysis. In particular, a
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

   - `processed-runs.jsonl` — a **durable index**, one row per resolved
     `pr`+full `sha`, recording the last selection evidence and the exact
     over-selection paths and under-selection edges credited to that head:
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

     Retain these rows even after their `seen` date ages out. This file
     is read in full every run and has a 2 MiB limit: keep rows compact,
     but **never prune or silently omit** an identity to make it fit.
     If a write would exceed the configured size or patch limit, report
     the capacity failure prominently, do not write incomplete ledgers or
     claim exact cumulative counts, and file no issue until the memory
     capacity is explicitly addressed.

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
       confidence bar. Keep accumulating evidence for it.
     - `correct-by-design` — settled; stop re-deriving it.
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
     - `fixed` — the referenced fix has merged. Retain the row and its
       historical counters so a later CI attempt can reverse a credited
       head's prior contribution. Put the merged fix's PR number in `ref`;
       re-evaluate if the rule changes again.

     Use `correct-by-design` for anything the prompt tells you to reject as
     intended behavior rather than as weak evidence — a file on the
     build-input list in step 5, or a self-referential selector change in
     step 6. Those are settled, not still being watched.

     Only create a row for a path actually observed escalating (or
     a distinct missing edge, for `under-selection`) in this audit's
     window or explicit `pr_numbers` scope. Do not seed
     a row for a path you merely noticed sharing a fix with an observed
     one. Retain a previously credited row even if its count falls to
     zero after a rerun; the prior finding and its verdict are still
     part of the audit history.

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
   a few times in one window is weak evidence and will not clear the
   confidence bar — but the same rule accumulating escalations week after
   week is exactly the signal worth acting on, and it is only visible if
   the counts survive across runs.
2. **Use the collected evidence.** Read every record in
   `/tmp/gh-aw/test-selection-audit/evidence.json`; do not enumerate PRs,
   runs, jobs, comments, or artifacts again. The collector includes all PR
   states in the requested scope and records the current head SHA, latest
   CI run/attempt, normalized changed and excluded paths, selected tests
   and jobs, and explicit gap status.

   The collector checks prior processed rows only after resolving the
   latest run and attempt. Reuse a `recorded` row unchanged. Re-evaluate
   a newer creditable attempt, but preserve the prior row and contributions when
   the newer record is pending, approval-blocked, missing, invalid,
   truncated, or untrusted. An unchanged creditable attempt contributes
   no new count.

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

   Stage each resolved head's distinct `over_paths` as proposed
   contributions, and report the cumulative figure from `watchlist.jsonl`
   alongside this window's. Do not increment the watchlist here:
   step 13 applies the difference from that head's prior contributions
   **after** step 7 determines `miss_edges`. An unchanged rerun contributes
   nothing new; an ALL-to-narrow rerun must remove its previous ALL
   contribution. A merge-base fail-safe ALL result has no triggering path
   and must not be attributed to a rule just to make the totals grow.
4. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.

   An existing ALL-route with a rationale that names a *general* effect
   (e.g. "checkout normalization can affect fixture-sensitive tests")
   rather than a specific, already-covered test is not proof the ALL scope
   is the minimum safe one — it usually means nobody has written the guard
   yet. That guard must be exhaustive, not just pin today's known values: a
   test that only asserts the file's *current* directives/values stay
   unchanged does not catch a new, unguarded directive being added, and a
   new addition is exactly the class of edit a narrower route needs to
   catch. For example, a guard pinning `.gitattributes`'s existing rules
   stays green when a PR adds a brand-new `*.cs` rule, because it only
   ever inspected the rules it already knew about — yet that new rule
   changes every checkout of a `.cs` file, and a narrower route would
   silently miss it. The guard must instead assert the file's *complete*
   set of directives against an explicit allow-list and fail on anything
   outside it — including a new directive of the same byte-affecting kind
   (e.g. another `text`/`eol`/filter attribute) — not just re-check the
   values already known. If you cannot write a guard with that exhaustive,
   reject-anything-new shape, keep the path routed to `ALL` instead of
   proposing the narrower route.
5. **Do not question broad build-input files.** These files legitimately
   affect nearly the entire .NET project graph — treat their `ALL`
   escalation as correct-by-design and do not flag it as a finding, even if
   it looks broad:

   `Directory.Packages.props`, `Directory.Build.props`,
   `Directory.Build.targets`, `NuGet.config`, `eng/Versions.props`,
   `eng/Version.Details.xml`, `src/Directory.Build.props`, and
   `global.json`.

   **That list is exhaustive.** It is not a category to reason by analogy
   from. A file is exempt because it appears above, not because it sits at
   the repository root, has a `.props`/`.json` extension, or looks
   infrastructural. In particular, `.gitattributes`, `.editorconfig`,
   `.config/dotnet-tools.json`, `Aspire.slnx`, and CI YAML are **not** on
   this list and must be judged on their actual effect like anything else.
   A previous run wrongly waved `.gitattributes` through as a "broad
   build input", and separately put `Aspire.slnx` on this very exemption
   list by analogy ("it's a solution file, sounds build-wide") — but
   Layer 1 already roots its project graph at `Aspire.slnx` at the PR
   head, so an added project is already in the universe the graph walks
   and carries no signal the graph does not already have; only the
   removal direction still needs the run-all fallback, since a deleted
   project's files can no longer be attributed to anything. Both are
   exactly the kind of over-selection this audit exists to catch — do
   not let a file's *name* substitute for reading what actually consumes
   it.
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
13. **Write back what you learned.** Before finishing, update the two
    ledgers in `/tmp/gh-aw/repo-memory/default/`. They are committed
    automatically after the run; you only need to write the files.

    - For every head with a **new, verified** selection result and the
      necessary changed-file and under-selection evidence this run,
      finish both over- and under-selection checks before updating either
      ledger. Compare its staged distinct `over_paths`/`miss_edges`
      against that exact `pr`+full `sha` row's arrays **as they existed
      at the start of this run** (empty sets for a genuinely new head).
      For each previously or newly credited path/edge, apply `+1` only
      if newly credited and `-1` only if previously credited but now
      absent; unchanged sets have delta zero. Use `(path, over-selection)`
      for `all_runs` and `(path, under-selection, target)` for
      `miss_runs`. Apply all deltas to the matching watchlist rows, then replace the
      processed row with this run ID, attempt, `over_paths`,
      `miss_edges`, and `seen` date (or append it for a new head).
      Count a head once per path or missing edge, never once per CI
      attempt. Check that no counter becomes negative and every prior
      credit has a matching row; if either check fails,
      report the inconsistency and leave **both** ledgers unchanged
      rather than guessing a correction.

      If a newer attempt is pending, blocked, has no selection job, or
      lacks creditable deterministic selection or changed-file evidence, do not
      replace the head's prior row, adjust its counters, or treat an old
      result as current evidence. Do not write rows for unresolved heads.
      Preserve older rows **indefinitely**: PR `updated` time can
      bring an old head back into a scheduled window, and `pr_numbers`
      can revisit one at any age. Pruning by `seen` would turn it into
      a fresh head and add its existing contribution a second time.
    - Update `watchlist.jsonl` using those per-head deltas, plus any
      carried-forward rows. An unchanged rerun or repeated static gap
      has no delta: it must not refresh `last_seen` or increment a
      counter. Refresh `last_seen` only on a newly credited observation;
      on retraction, keep it as the historical last-observation date,
      not an assertion that the retracted evidence is still credited.
      Keep `example_prs` drawn from currently credited heads rather
      than retaining a PR whose only contribution was retracted.
      Preserve existing `verdict` and `ref` when replacing evidence
      only after verifying that the underlying rule, path, missing
      target, and (for under-selection) consumer and PR eligibility
      evidence still support them. Keep `watch`
      and `correct-by-design` rows even when their counts fall to zero.
      Once a fix merges, mark the row `fixed` instead of deleting it:
      old heads still reference its counts and may need corrections.

    Repo-memory validation rejects unknown files or fields, unsafe strings,
    duplicate identities, counters that do not exactly match the processed
    rows, and examples that are not currently credited. Keep the ledgers
    in the exact schemas above; arbitrary prose is not persistent memory.

    No JSONL-aware merge protects either file: a rejected push retries
    with `git pull --no-rebase -X ours`, and **even two appends at EOF
    can conflict**, silently dropping one run's rows. The workflow-level
    concurrency group serializes the agent and memory-push jobs; do not
    rely on the file format to make concurrent runs safe. When updating
    a row, preserve every unrelated row. Verify both ledgers fit the
    configured file and patch limits **before** emitting `create-issue`;
    on failure, report the capacity problem and make no partial update.
    Keep rows one-line and minimal; both files are read in full on every
    future run.

## The issue you file

Write the issue as a task specification for the Copilot coding agent that
will be assigned to it — it should be able to start work from the issue
alone, without re-doing your analysis.

Title it so it identifies the offending input and effect, for example
`Narrow build.yml selection to affected CI jobs` for over-selection, or
`Select extension e2e for CLI archive changes` for under-selection.
Use a stable, plain ASCII title of at most 100 characters **including**
the `[test-selection-audit] ` prefix, with only letters, digits, spaces,
periods, and hyphens. Do not put Markdown, mentions, or a second copy
of the prefix in the title passed to `create-issue`. Store the same
prefixed final title in the pending row's `note`; mention full file
paths, target names, and other details in the body. If two missing
targets share a path, distinguish them in the title and body so
deduplication does not collapse different findings.
Titles are deduplicated against open and recently-closed issues, so a stable,
specific title prevents re-filing the same finding on a later run.

The body must contain:

- **Symptom**: the concrete finding — which PR(s)/run(s), what changed,
  and either that the selector ran ALL tests as a result (over-selection)
  or which real consumer's tests the narrow selection missed
  (under-selection). For an ALL result, quote the actual escalation
  reason/log line verbatim in a fenced code block. A narrow result has
  no escalation reason: instead quote its actual selected tests/jobs
  from the deterministic evidence and identify the omitted
  target. Spell out the literal input path and `test:`/`job:` target in
  the body so a pending issue can be reconciled with the right watchlist
  edge. Never fabricate a reason for a narrow result.
- **Evidence**: real example PR(s) that hit this rule, each with the
  file(s) it touched and the before/after **selected test-project and
  job sets**, with counts for each. A missing job can change no test
  count; do not present a zero test delta as no impact. You cannot
  run `tools/SelectTests` yourself in this sandbox, so derive the
  proposed sets from the existing result and map targets (including
  derived targets), label the after-set explicitly as an estimate,
  and do not claim a precise count when it cannot be established;
  the assigned agent establishes exact sets and counts under Required
  validation below. **One clear, unambiguous example is enough** — do not
  pad the issue with additional PRs just to hit a count. Reach for more
  than one only when a single example leaves genuine room for doubt (for
  example, it could plausibly be a one-off rather than a repeating
  pattern); in that case, pull the extra examples and the cumulative count
  from `watchlist.jsonl`'s history for this rule rather than searching for
  new ones. A rule that has been climbing for weeks is stronger evidence
  than one seen once — say which case this is. Link the specific
  trigger-map rule or code path responsible, with file and line.
- **Root cause**: why the current rule is broader (or narrower/missing)
  than necessary, in one or two plain-language sentences. This is the
  sentence a reviewer should be able to quote back to explain the change
  in one breath — write it so it stands on its own, without the reader
  needing the rest of the issue.
- **Suggested fix**: the specific, scoped change to
  `eng/github-ci/test-trigger-map.yml` (or the selector) — not a rewrite of
  the selection design. Say which mechanism it uses (`prefilter`, `ignore`,
  `path_rules`, `affected_project_rules`, `derived_targets`, or `groups`)
  and cite a prior commit that used the same shape, so the assigned agent
  follows established convention rather than inventing one. Name the test to
  add or update under `tests/Infrastructure.Tests/TestTriggerMap/` to pin the
  new behavior, and note whether `docs/ci/test-trigger-map.md` needs a
  matching update.
- **Prior art**: the commits you consulted for this rule and what they
  establish. If history shows a related change that was reverted, say so
  and explain why this proposal is different.
- **Required validation** (the assigned agent must do this before opening a
  PR, and must not claim success without it):
  - Run `tools/SelectTests` against the changed-file lists from **every**
    example PR named in Evidence, not just one, and report selected test
    projects **and jobs** before and after the change for each.
  - Run the guard tests:
    `dotnet test --project tests/Infrastructure.Tests/Infrastructure.Tests.csproj --no-launch-profile -- --filter-namespace "*.TestTriggerMap" --filter-not-trait "quarantined=true" --filter-not-trait "outerloop=true"`
  - Open the result as a **draft** PR that links back to this issue.
- **PR description requirement**: tell the assigned agent explicitly that
  the PR it opens must carry this issue's Root cause sentence and the
  Evidence examples forward into its own description, not just a diff
  summary — state it in the issue body as an instruction the assigned
  agent will follow, e.g. "Your PR description must restate why the old
  rule was wrong and name the real PRs this would have helped, with their
  before/after test-project and job sets, so a reviewer can judge the
  change without re-deriving your analysis." A reviewer approving a
  trigger-map change should not have to re-open this issue to find out why.
- **Unvalidated-analysis caveat**: state that this issue came from an
  automated audit and the suggested fix has not been validated by running
  the selector or tests. If validation contradicts the analysis here, the
  assigned agent should say so on the issue and not force the change
  through.
- Footer: `<sub>Automated by the weekly CI test-selection audit workflow.</sub>`

## Run summary (always report, regardless of whether an issue was filed)

In your final response, report:

- How many PRs/runs were analyzed and over what window (or which PR numbers,
  if explicitly given), and how many heads reused a recorded result
  after comparing the deterministic run metadata. Report separately any reruns that replaced
  prior contributions and any missing/stale-attempt evidence.
- How many PRs were skipped because they could not have a selection result
  yet (CI pending, `action_required`, or no selection job), so a quiet
  window is distinguishable from an unanalyzable one.
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts — both for this window and cumulatively across runs.
- The current watchlist: each path and, for under-selection, each missing
  target being tracked; its cumulative `all_runs` / `miss_runs` count,
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
