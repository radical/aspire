#!/usr/bin/env bash
set -euo pipefail

# The analysis JSON ships in the `ci-analysis-output` artifact, which the
# `download-analysis` step unpacks. It is not a sibling of the agent output,
# so this path must come from that step rather than being derived.
: "${ANALYSIS_DIR:?ANALYSIS_DIR is required (download-analysis step missing?)}"
ANALYSIS_FILE="$ANALYSIS_DIR/analysis-result.json"
RUN_CONTEXT_FILE="ci-failure-data/run-context.json"
TRUSTED_FAILED_JOBS_FILE="ci-failure-data/failed-jobs.json"
RUN_SCOPE=$(jq -r '.run_scope' "$RUN_CONTEXT_FILE")
RUN_URL=$(jq -r '.html_url // ""' ci-failure-data/run.json)
PR_NUMBERS=$(jq -r '.pr_numbers' "$RUN_CONTEXT_FILE")
: "${REPO:?REPO is required}"

# ── 4. Post PR comment using the analysis JSON ──
if [ "$RUN_SCOPE" = "main" ]; then
  echo "Main run analysis is reported through cause issues, not PR comments."
  exit 0
fi

SUBJECT_PR="$PR_NUMBERS"
if [[ ! "$SUBJECT_PR" =~ ^[0-9]+$ ]]; then
  echo "No unambiguous subject PR found. Skipping comment."
  exit 0
fi

require_actionable_pr()
{
  if ! PR_ACTIONABLE=$(bash .github/workflows/analyze-ci-failure-persistence.sh \
      pr-actionable "$REPO" "$SUBJECT_PR"); then
    echo "::warning::PR state is unknown. Skipping comment."
    return 1
  fi
  if [ "$PR_ACTIONABLE" != "true" ]; then
    echo "PR #${SUBJECT_PR} is closed or locked. Skipping comment."
    return 1
  fi
}

# Avoid fetching and rendering comments when the subject PR is
# already unavailable. This is rechecked before the mutation below.
if ! require_actionable_pr; then
  exit 0
fi

# Update an existing analysis comment if one exists (by marker),
# otherwise create a new one. This prevents stacking duplicate
# comments on PRs with repeated CI failures.
if ! EXISTING_COMMENT_ID=$(bash .github/workflows/analyze-ci-failure-persistence.sh \
    find-analysis-comment "$REPO" "$SUBJECT_PR"); then
  echo "::warning::Existing comment state is unknown. Skipping comment."
  exit 0
fi

# Build comment body from the analysis JSON and write to a file
# to avoid shell expansion issues and ARG_MAX limits.
COMMENT_FILE=""
COMMENT_REQUEST_FILE=""
trap 'rm -f "$COMMENT_FILE" "$COMMENT_REQUEST_FILE"' EXIT
COMMENT_FILE=$(mktemp)
bash .github/workflows/analyze-ci-failure-comment.sh \
  "$ANALYSIS_FILE" "$TRUSTED_FAILED_JOBS_FILE" "$RUN_URL" > "$COMMENT_FILE"

if [ -n "$EXISTING_COMMENT_ID" ]; then
  COMMENT_REQUEST_FILE=$(mktemp)
  jq -n --rawfile body "$COMMENT_FILE" '{body: $body}' > "$COMMENT_REQUEST_FILE"
  if ! require_actionable_pr; then
    exit 0
  fi
  gh api --method PATCH "repos/${REPO}/issues/comments/${EXISTING_COMMENT_ID}" \
    --input "$COMMENT_REQUEST_FILE" > /dev/null
  echo "Updated existing analysis comment (ID: ${EXISTING_COMMENT_ID}) on PR #${SUBJECT_PR}"
else
  if ! require_actionable_pr; then
    exit 0
  fi
  gh pr comment "$SUBJECT_PR" --repo "$REPO" --body-file "$COMMENT_FILE"
  echo "Posted new analysis comment on PR #${SUBJECT_PR}"
fi
