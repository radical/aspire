#!/usr/bin/env bash
# Check whether a failed main CI run is still the current, final automatic attempt.
# The analyzer calls this before collecting evidence, publishing analysis, and
# updating each cause issue so queued work cannot publish against a newer run.
# Exit 0 when current, 2 when stale or not final, or fail if verification fails.
set -euo pipefail

RUN_CONTEXT_FILE="${1:?run context file is required}"
REPO="${2:?repository is required}"

RUN_ID=$(jq -er '.run_id | select(type == "number" and . > 0)' "$RUN_CONTEXT_FILE")
RUN_ATTEMPT=$(jq -er '.run_attempt | select(type == "number" and . > 0)' "$RUN_CONTEXT_FILE")
HEAD_SHA=$(jq -er '.head_sha | select(type == "string" and length > 0)' "$RUN_CONTEXT_FILE")

if [ "$RUN_ATTEMPT" -ne 4 ]; then
  echo "::notice::Main CI run ${RUN_ID} attempt ${RUN_ATTEMPT} is not the final automatic attempt."
  exit 2
fi

LIVE_RUN=$(gh api "repos/${REPO}/actions/runs/${RUN_ID}")
if ! jq -e '
  (.id | type == "number" and . > 0) and
  (.run_attempt | type == "number" and . > 0) and
  (.run_number | type == "number" and . > 0) and
  (.workflow_id | type == "number" and . > 0) and
  (.head_sha | type == "string" and length > 0)
' <<< "$LIVE_RUN" >/dev/null; then
  echo "::error::The live main CI run is missing required identity fields." >&2
  exit 1
fi

if ! jq -e \
  --argjson id "$RUN_ID" \
  --argjson attempt "$RUN_ATTEMPT" \
  --arg sha "$HEAD_SHA" '
    .id == $id and .run_attempt == $attempt and
    .head_sha == $sha and .event == "push" and .head_branch == "main" and
    .path == ".github/workflows/ci.yml" and
    .status == "completed" and .conclusion == "failure"
  ' <<< "$LIVE_RUN" >/dev/null; then
  echo "::notice::Main CI run ${RUN_ID} is no longer the completed failed attempt ${RUN_ATTEMPT}."
  exit 2
fi

MAIN_REF=$(gh api "repos/${REPO}/git/ref/heads/main")
if ! jq -e '.object.sha | type == "string" and length > 0' <<< "$MAIN_REF" >/dev/null; then
  echo "::error::Could not verify the current main SHA." >&2
  exit 1
fi
if ! jq -e --arg sha "$HEAD_SHA" '.object.sha == $sha' <<< "$MAIN_REF" >/dev/null; then
  echo "::notice::Main has advanced beyond CI run ${RUN_ID}. Skipping stale analysis."
  exit 2
fi

WORKFLOW_ID=$(jq -r '.workflow_id' <<< "$LIVE_RUN")
RUN_NUMBER=$(jq -r '.run_number' <<< "$LIVE_RUN")
MAIN_RUNS=$(gh api --method GET "repos/${REPO}/actions/workflows/${WORKFLOW_ID}/runs" \
  -f branch=main -f event=push -f per_page=100)
if ! jq -e '
  (.workflow_runs | type == "array") and
  all(.workflow_runs[]; (.id | type == "number" and . > 0) and (.run_number | type == "number" and . > 0))
' <<< "$MAIN_RUNS" >/dev/null; then
  echo "::error::Could not verify the newer main CI runs." >&2
  exit 1
fi
if jq -e --argjson id "$RUN_ID" --argjson number "$RUN_NUMBER" \
  'any(.workflow_runs[]; .id != $id and .run_number > $number)' \
  <<< "$MAIN_RUNS" >/dev/null; then
  echo "::notice::A newer main CI run supersedes run ${RUN_ID}. Skipping stale analysis."
  exit 2
fi
