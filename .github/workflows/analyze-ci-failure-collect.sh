#!/usr/bin/env bash
set -euo pipefail

set_output()
{
  printf '%s\n' "$1" >> "$GITHUB_OUTPUT"
}

mkdir -p ci-failure-data

# Resolve the run ID
if [ "${EVENT_NAME}" = "workflow_dispatch" ]; then
  RUN_ID="${MANUAL_RUN_ID}"
else
  RUN_ID="${WORKFLOW_RUN_ID}"
fi

echo "Analyzing CI run: ${RUN_ID}"
set_output "run_id=${RUN_ID}"

# A workflow_run can wait behind another analysis, during which the source run may
# be rerun. Pin that event to its immutable attempt; manual dispatch intentionally
# analyzes the latest attempt.
if [ "${EVENT_NAME}" = "workflow_dispatch" ]; then
  RUN_METADATA_ENDPOINT="repos/${REPO}/actions/runs/${RUN_ID}"
else
  if ! [[ "${WORKFLOW_RUN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]; then
    echo "::error::The workflow_run event did not provide a valid run attempt"
    exit 1
  fi
  RUN_METADATA_ENDPOINT="repos/${REPO}/actions/runs/${RUN_ID}/attempts/${WORKFLOW_RUN_ATTEMPT}"
fi
gh api "${RUN_METADATA_ENDPOINT}" > ci-failure-data/run.json

RUN_ATTEMPT=$(jq -r '.run_attempt // 1' ci-failure-data/run.json)
RUN_STARTED_AT=$(jq -r '.run_started_at // ""' ci-failure-data/run.json)
RUN_UPDATED_AT=$(jq -r '.updated_at // ""' ci-failure-data/run.json)
RUN_EVENT=$(jq -r '.event // ""' ci-failure-data/run.json)
RUN_WORKFLOW_PATH=$(jq -r '.path // ""' ci-failure-data/run.json)
HEAD_SHA=$(jq -r '.head_sha // ""' ci-failure-data/run.json)
HEAD_BRANCH=$(jq -r '.head_branch // ""' ci-failure-data/run.json)
RUN_URL=$(jq -r '.html_url // ""' ci-failure-data/run.json)
CONCLUSION=$(jq -r '.conclusion // ""' ci-failure-data/run.json)
if [ "$RUN_WORKFLOW_PATH" != ".github/workflows/ci.yml" ]; then
  echo "::error::Run ${RUN_ID} belongs to workflow '${RUN_WORKFLOW_PATH}', not '.github/workflows/ci.yml'"
  exit 1
fi
case "${RUN_EVENT}:${HEAD_BRANCH}" in
  push:main)
    RUN_SCOPE="main"
    ;;
  pull_request:*|pull_request_target:*)
    RUN_SCOPE="pull-request"
    ;;
  *)
    echo "::notice::Unsupported run scope: event=${RUN_EVENT}, branch=${HEAD_BRANCH}. Skipping analysis."
    echo "has_work=false" >> "$GITHUB_OUTPUT"
    exit 0
    ;;
esac
set_output "run_attempt=${RUN_ATTEMPT}"
set_output "head_sha=${HEAD_SHA}"
set_output "run_url=${RUN_URL}"
set_output "run_scope=${RUN_SCOPE}"

# Skip analysis if the run succeeded (e.g. manual dispatch on a passing run)
if [ "${CONCLUSION}" = "success" ]; then
  echo "Run concluded with success. Nothing to analyze."
  set_output "has_work=false"
  exit 0
fi

PR_NUMBERS=""
if [ "${RUN_SCOPE}" = "pull-request" ]; then
  PR_LOOKUP_AMBIGUOUS=false

  consider_pr_candidates()
  {
    local candidates="$1"
    local candidate_count

    candidate_count=$(jq -r 'unique | length' <<< "${candidates}")
    if [ "${candidate_count}" -eq 1 ]; then
      PR_NUMBERS=$(jq -r 'unique | .[0]' <<< "${candidates}")
    elif [ "${candidate_count}" -gt 1 ]; then
      PR_LOOKUP_AMBIGUOUS=true
    fi
  }

  # Workflow metadata can include pull requests from forks that happen
  # to reference this commit, so only accept PRs targeting this repository.
  PR_CANDIDATES=$(jq -c --arg repo_url "https://api.github.com/repos/${REPO}" \
    '[.pull_requests[]? | select(.base.repo.url == $repo_url and (.number | type) == "number") | .number]' \
    ci-failure-data/run.json)
  consider_pr_candidates "${PR_CANDIDATES}"
  if [ -z "${PR_NUMBERS}" ] && [ "${PR_LOOKUP_AMBIGUOUS}" = "false" ] && [ -n "${HEAD_SHA}" ]; then
    if ! PR_CANDIDATE_PAGES=$(gh api --paginate --slurp \
        "repos/${REPO}/commits/${HEAD_SHA}/pulls?per_page=100" 2>/dev/null); then
      echo "::error::Failed to look up pull requests associated with commit ${HEAD_SHA}."
      exit 1
    fi
    # --slurp wraps paginated response arrays as [[page 1], [page 2]].
    PR_CANDIDATES=$(jq -c --arg repo "$REPO" \
      '[.[][] | select(.base.repo.full_name == $repo and (.number | type) == "number") | .number]' \
      <<< "$PR_CANDIDATE_PAGES")
    consider_pr_candidates "${PR_CANDIDATES}"
  fi
  if [ -z "${PR_NUMBERS}" ] && [ "${PR_LOOKUP_AMBIGUOUS}" = "false" ] && [ -n "${HEAD_SHA}" ]; then
    HEAD_OWNER=$(jq -r '.head_repository.owner.login // ""' ci-failure-data/run.json)
    if [ -n "${HEAD_OWNER}" ] && [ -n "${HEAD_BRANCH}" ]; then
      # GitHub does not return commit associations for every fork PR. Use
      # branch identity only to find candidates, then require the immutable
      # failed-run SHA to match before accepting one.
      if ! PR_CANDIDATE_DATA=$(gh api --method GET --paginate --slurp "repos/${REPO}/pulls" \
          -f state=all \
          -f per_page=100 \
          -f "head=${HEAD_OWNER}:${HEAD_BRANCH}" 2>/dev/null); then
        echo "::error::Failed to look up pull requests for ${HEAD_OWNER}:${HEAD_BRANCH}."
        exit 1
      fi
      PR_CANDIDATES=$(jq -c --arg head_sha "$HEAD_SHA" \
        '[.[][] | select((.number | type) == "number" and .head.sha == $head_sha) | .number]' \
        <<< "$PR_CANDIDATE_DATA")
      consider_pr_candidates "${PR_CANDIDATES}"
    fi
  fi

  if [ "${PR_LOOKUP_AMBIGUOUS}" = "true" ]; then
    PR_NUMBERS=""
    echo "::warning::Multiple associated PRs found. Analysis will proceed without subject PR context."
  elif [ -z "${PR_NUMBERS}" ]; then
    echo "No associated PR found. Analysis will proceed without PR context."
  fi
else
  # The PR associated with the failed head commit identifies the merge
  # that triggered this run. It is context only and is not presumed causal.
  gh api --paginate --slurp \
    "repos/${REPO}/commits/${HEAD_SHA}/pulls?per_page=100" 2>/dev/null |
    jq -c --arg repo "$REPO" \
      '[.[][] | select(.base.repo.full_name == $repo and .base.ref == "main" and .merged_at != null)] |
      unique_by(.number) |
      if length == 1 then .[0] else {} end |
      if .number then
        {number, title, state, user: {login: .user.login}, head: {ref: .head.ref},
          base: {ref: .base.ref}, html_url, merged_at}
      else
        {}
      end' \
    > ci-failure-data/triggering-merge-pr.json \
    || echo "{}" > ci-failure-data/triggering-merge-pr.json

  WORKFLOW_ID=$(jq -r '.workflow_id' ci-failure-data/run.json)
  RUN_CREATED_AT=$(jq -r '.created_at' ci-failure-data/run.json)
  FAILED_RUN_ID=$(jq -r '.id' ci-failure-data/run.json)
  if ! bash .github/workflows/analyze-ci-failure-history.sh \
      "$REPO" "$WORKFLOW_ID" "$RUN_CREATED_AT" "$FAILED_RUN_ID" \
      ci-failure-data/last-successful-main-run.json; then
    echo "::warning::Unable to find the last successful main run. Continuing without a candidate merge range."
    echo "{}" > ci-failure-data/last-successful-main-run.json
  fi

  LAST_SUCCESSFUL_SHA=$(jq -r '.head_sha // ""' ci-failure-data/last-successful-main-run.json)
  bash .github/workflows/analyze-ci-failure-candidates.sh \
    "$REPO" "$LAST_SUCCESSFUL_SHA" "$HEAD_SHA" \
    ci-failure-data/candidate-merges.json \
    ci-failure-data/candidate-merge-history-status.json
fi
set_output "pr_numbers=${PR_NUMBERS}"

jq -n \
  --argjson run_id "${RUN_ID}" \
  --argjson run_attempt "${RUN_ATTEMPT}" \
  --arg event "${RUN_EVENT}" \
  --arg head_branch "${HEAD_BRANCH}" \
  --arg head_sha "${HEAD_SHA}" \
  --arg run_scope "${RUN_SCOPE}" \
  --arg pr_numbers "${PR_NUMBERS}" \
  '{
    run_id: $run_id,
    run_attempt: $run_attempt,
    event: $event,
    head_branch: $head_branch,
    head_sha: $head_sha,
    run_scope: $run_scope,
    pr_numbers: $pr_numbers
  }' > ci-failure-data/run-context.json

# Fetch all jobs for this run attempt.
# Use --jq '.jobs[]' to emit individual job objects (handles pagination
# correctly) then jq -s collects them into a single JSON array.
gh api --paginate "repos/${REPO}/actions/runs/${RUN_ID}/attempts/${RUN_ATTEMPT}/jobs" \
  --jq '.jobs[]' | jq -s '.' > ci-failure-data/all-jobs.json

# Extract failed jobs, excluding "gate" jobs that just check dependency status.
# Gate jobs (e.g. "Final Results", "Final Test Results") only echo "dependent jobs
# failed" and provide zero diagnostic value — they just inflate the logs.
jq '[.[] | select(.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out")
     | select(
         (.steps // [] | map(select(.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out")) | length) > 0
         and (
           (.steps // [] | map(select(.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out")) | .[0].name)
           | test("^(Fail if|Check ).*(depend|failed)"; "i") | not
         )
       )]' \
  ci-failure-data/all-jobs.json > ci-failure-data/failed-jobs.json

FAILED_COUNT=$(jq 'length' ci-failure-data/failed-jobs.json)
echo "Failed jobs: ${FAILED_COUNT}"

if [ "${FAILED_COUNT}" -eq 0 ]; then
  echo "No failed jobs found. Skipping analysis."
  set_output "has_work=false"
  exit 0
fi

set_output "has_work=true"

# Fetch logs for each failed job and extract only error-relevant lines.
# Raw logs are huge (64KB+). Instead of blindly taking the last N lines,
# we grep for error indicators with context to produce a focused extract.
# Newer gh versions reject terminal escapes even when stdout is redirected.
# Allow them in the captured file, then remove credentials and unsafe controls
# before extracting the bounded diagnostic included in the analysis summary.
GH_LOG_FLAGS=()
if gh api --help | grep -q -- '--allow-escape-sequences'; then
  GH_LOG_FLAGS+=(--allow-escape-sequences)
fi
jq -r '.[].id' ci-failure-data/failed-jobs.json | while read -r JOB_ID; do
  JOB_NAME=$(jq -r ".[] | select(.id == ${JOB_ID}) | .name" ci-failure-data/failed-jobs.json)
  echo "Fetching logs for job: ${JOB_NAME} (${JOB_ID})"
  LOG_EXIT_CODE=0
  gh api "${GH_LOG_FLAGS[@]}" "repos/${REPO}/actions/jobs/${JOB_ID}/logs" \
    > "ci-failure-data/job-${JOB_ID}-raw.log" \
    2> "ci-failure-data/job-${JOB_ID}-fetch-error.log" || LOG_EXIT_CODE=$?
  if [ "${LOG_EXIT_CODE}" -ne 0 ]; then
    {
      printf '\nFailed to fetch complete logs for job %s: gh exited with code %s.\n' \
        "${JOB_ID}" "${LOG_EXIT_CODE}"
      cat "ci-failure-data/job-${JOB_ID}-fetch-error.log"
    } >> "ci-failure-data/job-${JOB_ID}-raw.log"
  fi
  rm -f "ci-failure-data/job-${JOB_ID}-fetch-error.log"
  bash .github/workflows/analyze-ci-failure-persistence.sh \
    sanitize-untrusted-text \
    "ci-failure-data/job-${JOB_ID}-raw.log" \
    "ci-failure-data/job-${JOB_ID}-normalized.log"
  mv "ci-failure-data/job-${JOB_ID}-normalized.log" \
    "ci-failure-data/job-${JOB_ID}-raw.log"

  # Extract error-relevant lines with 3 lines of context before and 5 after.
  # Patterns: compiler errors, build failures, test failures, runtime errors,
  # infrastructure errors, and GitHub Actions error annotations.
  grep -n -i -B3 -A5 \
    -e 'error [A-Z]\{2,\}[0-9]' \
    -e '##\[error\]' \
    -e '\bFAILED\b' \
    -e '\bfailed!\b' \
    -e 'Build FAILED' \
    -e 'ECONNRESET\|ECONNREFUSED\|ENOTFOUND' \
    -e 'Connection reset by peer' \
    -e 'Could not resolve host' \
    -e 'Operation timed out' \
    -e 'The SSL connection could not be established' \
    -e 'The requested URL returned error' \
    -e '403 Forbidden' \
    -e 'exit code [1-9]' \
    -e 'Process completed with exit code' \
    "ci-failure-data/job-${JOB_ID}-raw.log" 2>/dev/null \
    | head -150 > "ci-failure-data/job-${JOB_ID}.log" || true

  # If grep found nothing, fall back to last 200 lines (job may have unusual errors)
  if [ ! -s "ci-failure-data/job-${JOB_ID}.log" ]; then
    tail -200 "ci-failure-data/job-${JOB_ID}-raw.log" > "ci-failure-data/job-${JOB_ID}.log"
  fi
  rm -f "ci-failure-data/job-${JOB_ID}-raw.log"
done

# Fetch annotations for each failed job
jq -r '.[].id' ci-failure-data/failed-jobs.json | while read -r JOB_ID; do
  CHECK_RUN_ID=$(jq -r ".[] | select(.id == ${JOB_ID}) | .check_run_url" ci-failure-data/failed-jobs.json \
    | grep -oP '\d+$' || echo "")
  if [ -n "${CHECK_RUN_ID}" ]; then
    gh api --paginate "repos/${REPO}/check-runs/${CHECK_RUN_ID}/annotations" \
      --jq '.[]' | jq -s '.' \
      > "ci-failure-data/annotations-${JOB_ID}.json" 2>/dev/null || \
      echo "[]" > "ci-failure-data/annotations-${JOB_ID}.json"
  else
    echo "[]" > "ci-failure-data/annotations-${JOB_ID}.json"
  fi
done

# Fetch the PR diff to compare against failures
SUBJECT_PR="${PR_NUMBERS}"
if [[ "${SUBJECT_PR}" =~ ^[0-9]+$ ]]; then
  gh api "repos/${REPO}/pulls/${SUBJECT_PR}/files" --paginate \
    --jq '.[]' | jq -s '[.[] | {filename, status, additions, deletions, changes}]' \
    > ci-failure-data/pr-files.json 2>/dev/null || echo "[]" > ci-failure-data/pr-files.json

  # Fetch PR metadata (state, title, author) so the agent doesn't need
  # to make MCP pull_request_read calls at runtime.
  gh api "repos/${REPO}/pulls/${SUBJECT_PR}" \
    --jq '{number, title, state, locked, user: .user.login, head_branch: .head.ref, base_branch: .base.ref, html_url}' \
    > ci-failure-data/pr-metadata.json 2>/dev/null || echo "{}" > ci-failure-data/pr-metadata.json
fi

# Load the known transient failure patterns for reference
if [ -f "eng/test-retry-patterns.json" ]; then
  cp eng/test-retry-patterns.json ci-failure-data/retry-patterns.json
fi

# Fetch prior cause files from the memory branch so the agent can
# identify recurring failures and append occurrences rather than
# creating duplicate cause entries.
MEMORY_BRANCH="memory/ci-failure-analysis"
if git clone --depth 1 --branch "$MEMORY_BRANCH" \
    "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" \
    memory-checkout 2>/dev/null; then
  if [ -d "memory-checkout/causes" ]; then
    mkdir -p ci-failure-data/prior-causes
    cp memory-checkout/causes/*.json ci-failure-data/prior-causes/ 2>/dev/null || true
    PRIOR_COUNT=$(find ci-failure-data/prior-causes -name '*.json' -type f 2>/dev/null | wc -l)
    echo "Loaded ${PRIOR_COUNT} prior cause file(s) from memory branch"
  else
    echo "No prior causes directory on memory branch"
  fi
  rm -rf memory-checkout
else
  echo "Memory branch not found (first run or not yet created)"
fi

# Artifact listings are run-scoped and can contain same-named artifacts from
# multiple attempts. The attempt metadata bounds the upload window. Select each
# failed test job's immutable artifact by its workflow-defined API name, then
# download by ID so result paths or contents cannot reassign evidence across artifacts.
ARTIFACTS_FILE="ci-failure-data/artifacts.json"
TEST_EVIDENCE_STATE=unavailable
rm -f ci-failure-data/test-failures.json
if gh api --paginate "repos/${REPO}/actions/runs/${RUN_ID}/artifacts" \
    --jq '.artifacts[]' | jq -s '.' > "${ARTIFACTS_FILE}"; then
  SELECTED_ARTIFACTS_FILE="ci-failure-data/selected-test-result-artifacts.json"
  if bash .github/workflows/analyze-ci-failure-persistence.sh \
      select-test-result-artifacts "${ARTIFACTS_FILE}" \
      "${RUN_STARTED_AT}" "${RUN_UPDATED_AT}" ci-failure-data/failed-jobs.json \
      20 1073741824 104857600 "${RUN_ATTEMPT}" \
      > "${SELECTED_ARTIFACTS_FILE}"; then
    if [ "$(jq 'length' "${SELECTED_ARTIFACTS_FILE}")" -eq 0 ]; then
      TEST_EVIDENCE_STATE=not-applicable
    else
      mkdir -p \
        ci-failure-data/test-result-zips \
        ci-failure-data/test-results \
        ci-failure-data/test-failures
      printf '[]\n' > ci-failure-data/test-failures/empty.json
      ARTIFACT_DOWNLOAD_FAILED=false
      REMAINING_UNCOMPRESSED_BYTES=1073741824
      while IFS= read -r ARTIFACT; do
        ARTIFACT_ID=$(jq -r '.id' <<< "${ARTIFACT}")
        ARTIFACT_NAME=$(jq -r '.name' <<< "${ARTIFACT}")
        ARTIFACT_SIZE=$(jq -r '.size_in_bytes' <<< "${ARTIFACT}")
        JOB_NAME=$(jq -r '.job' <<< "${ARTIFACT}")
        RESULT_FORMAT=$(jq -r '.format' <<< "${ARTIFACT}")
        ARTIFACT_ZIP="ci-failure-data/test-result-zips/${ARTIFACT_ID}.zip"
        ARTIFACT_OUTPUT="ci-failure-data/test-results/${ARTIFACT_ID}"
        echo "Downloading test results artifact: ${ARTIFACT_NAME} (${ARTIFACT_ID})..."
        if ! gh api "repos/${REPO}/actions/artifacts/${ARTIFACT_ID}/zip" \
              > "${ARTIFACT_ZIP}" 2>/dev/null ||
            ! bash .github/workflows/analyze-ci-failure-persistence.sh \
              extract-test-results-artifact "${ARTIFACT_ZIP}" "${ARTIFACT_OUTPUT}" \
              10000 "${REMAINING_UNCOMPRESSED_BYTES}" 104857600 \
              "${ARTIFACT_SIZE}" "${RESULT_FORMAT}"; then
          if [ "${RESULT_FORMAT}" = "mocha" ]; then
            echo "Warning: Optional extension test diagnostics unavailable for ${JOB_NAME}; continuing without structured failed-test records"
            rm -f "${ARTIFACT_ZIP}"
            rm -rf "${ARTIFACT_OUTPUT}"
            continue
          fi
          ARTIFACT_DOWNLOAD_FAILED=true
          break
        fi
        if ! bash .github/workflows/analyze-ci-failure-persistence.sh \
            collect-test-failures "${ARTIFACT_OUTPUT}" "${JOB_NAME}" \
            ci-failure-data/failed-jobs.json \
            "ci-failure-data/test-failures/${ARTIFACT_ID}.json" \
            "${RESULT_FORMAT}"; then
          ARTIFACT_DOWNLOAD_FAILED=true
          break
        fi

        EXTRACTED_BYTES=$(find "${ARTIFACT_OUTPUT}" -type f -printf '%s\n' \
          | awk '{ total += $1 } END { print total + 0 }')
        REMAINING_UNCOMPRESSED_BYTES=$((REMAINING_UNCOMPRESSED_BYTES - EXTRACTED_BYTES))
      done < <(jq -c '.[]' "${SELECTED_ARTIFACTS_FILE}")

      if [ "${ARTIFACT_DOWNLOAD_FAILED}" = "false" ]; then
        jq -s 'add // []' ci-failure-data/test-failures/*.json \
          > ci-failure-data/test-failures.json
        TEST_EVIDENCE_STATE=complete
        echo "Extracted $(jq 'length' ci-failure-data/test-failures.json) test failure(s) from structured test results"
      else
        echo "Warning: Failed to download or safely extract per-job test results"
      fi
      rm -rf \
        ci-failure-data/test-result-zips \
        ci-failure-data/test-results \
        ci-failure-data/test-failures
    fi
  else
    echo "Warning: Failed to select bounded per-job test result artifacts"
  fi
else
  echo "Warning: Failed to list test results artifacts"
fi
printf '{"state":"%s"}\n' "${TEST_EVIDENCE_STATE}" \
  > ci-failure-data/test-evidence.json

echo "Data collection complete."
