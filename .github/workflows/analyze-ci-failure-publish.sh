#!/usr/bin/env bash
set -euo pipefail

# The analysis JSON and cause files ship in the `ci-analysis-output` artifact,
# which the `download-analysis` step unpacks. They are not siblings of the agent
# output, so this path must come from that step rather than being derived.
: "${ANALYSIS_DIR:?ANALYSIS_DIR is required (download-analysis step missing?)}"
ANALYSIS_FILE="$ANALYSIS_DIR/analysis-result.json"
CAUSES_DIR="$ANALYSIS_DIR/causes"

RUN_CONTEXT_FILE="ci-failure-data/run-context.json"
TRUSTED_FAILED_JOBS_FILE="ci-failure-data/failed-jobs.json"
TRUSTED_RUN_ID=$(jq -r '.run_id' "$RUN_CONTEXT_FILE")
TRUSTED_RUN_SCOPE=$(jq -r '.run_scope' "$RUN_CONTEXT_FILE")
VERDICT=$(jq -r '.verdict' "$ANALYSIS_FILE")

: "${REPO:?REPO is required}"
MEMORY_BRANCH="memory/ci-failure-analysis"

# Read fields from the analysis JSON
RUN_ID="$TRUSTED_RUN_ID"
RUN_SCOPE="$TRUSTED_RUN_SCOPE"
RUN_URL=$(jq -r '.html_url // ""' ci-failure-data/run.json)
ANALYZED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
PR_NUMBER=$(bash .github/workflows/analyze-ci-failure-persistence.sh pr-number)

# ── 1. Set up memory branch and merge cause data ──
# Skip persisting data for code-issue verdicts — these are not
# actionable by CI automation and would just add noise.
if [ "$VERDICT" = "code-issue" ]; then
  echo "Verdict is code-issue. Skipping memory branch persistence."
else
  if ! git clone --depth 1 --branch "$MEMORY_BRANCH" \
      "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" \
      memory-repo 2>/dev/null; then
    echo "Memory branch does not exist yet, creating orphan branch"
    git init memory-repo
    git -C memory-repo checkout --orphan "$MEMORY_BRANCH"
    git -C memory-repo remote add origin \
      "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git"
  fi
  git -C memory-repo config user.name "github-actions[bot]"
  git -C memory-repo config user.email "github-actions[bot]@users.noreply.github.com"

  # Store run summary under runs/ directory
  mkdir -p "memory-repo/runs"
  bash .github/workflows/analyze-ci-failure-persistence.sh write-run-summary \
    "$ANALYSIS_FILE" "memory-repo/runs/${RUN_ID}.json" "$ANALYZED_AT"

  # Store individual cause files under causes/ (shared across runs).
  # Each cause file accumulates occurrences over time. The agent
  # writes cause definitions (no occurrences); we build the occurrence
  # from the run summary and merge it into the stored cause file.
  if [ -d "$CAUSES_DIR" ]; then
    mkdir -p "memory-repo/causes"

    # Build the occurrence entry from the run summary JSON
    for CAUSE_FILE in "$CAUSES_DIR"/*.json; do
      [ -f "$CAUSE_FILE" ] || continue
      CAUSE_BASENAME=$(basename "$CAUSE_FILE")
      CAUSE_TYPE=$(jq -r '.type' "$CAUSE_FILE")
      printf -v CAUSE_BASENAME_DISPLAY '%q' "$CAUSE_BASENAME"
      printf -v CAUSE_TYPE_DISPLAY '%q' "$CAUSE_TYPE"
      EXISTING="memory-repo/causes/${CAUSE_BASENAME}"
      CAUSE_JOBS_PLAIN=$(bash .github/workflows/analyze-ci-failure-persistence.sh \
        cause-job-names "$CAUSE_FILE" "$TRUSTED_FAILED_JOBS_FILE" plain)

      # Add an occurrences array with this run's entry to the agent's cause file
      CAUSE_WITH_OCC=$(bash .github/workflows/analyze-ci-failure-persistence.sh add-occurrence \
        "$CAUSE_FILE" "$RUN_ID" "$RUN_URL" "$CAUSE_JOBS_PLAIN" "$ANALYZED_AT" |
        jq 'del(.job_ids, .job_names)')

      if [ -f "$EXISTING" ]; then
        CURRENT_CAUSE_TYPE=$(jq -r '.type // ""' "$EXISTING")
        CURRENT_CAUSE_ID=$(jq -r '.id // ""' "$EXISTING")
        printf -v CURRENT_CAUSE_TYPE_DISPLAY '%q' "$CURRENT_CAUSE_TYPE"
        if [ "${CURRENT_CAUSE_ID}.json" != "$CAUSE_BASENAME" ]; then
          echo "::error::Stored cause ID must match its filename: ${CAUSE_BASENAME_DISPLAY}"
          exit 1
        fi
        if [ "$CURRENT_CAUSE_TYPE" != "$CAUSE_TYPE" ]; then
          echo "::error::Stored cause ${CAUSE_BASENAME_DISPLAY} cannot change type from ${CURRENT_CAUSE_TYPE_DISPLAY} to ${CAUSE_TYPE_DISPLAY}"
          exit 1
        fi
        if [ "$CAUSE_TYPE" = "flaky-test" ]; then
          CURRENT_CAUSE_TEST_NAME=$(jq -r 'if (.test_name | type) == "string" then .test_name else "" end' "$EXISTING")
          CAUSE_TEST_NAME=$(jq -r '.test_name' "$CAUSE_FILE")
          if [ "$CURRENT_CAUSE_TEST_NAME" != "$CAUSE_TEST_NAME" ]; then
            echo "::error::Stored cause ${CAUSE_BASENAME_DISPLAY} cannot change test_name"
            exit 1
          fi
        fi
        # Stored cause fields are publisher-authoritative. A later
        # agent may add an occurrence but cannot rewrite identity
        # or diagnostic text derived from an earlier run.
        printf '%s\n' "$CAUSE_WITH_OCC" > "${EXISTING}.new"
        bash .github/workflows/analyze-ci-failure-persistence.sh merge-cause \
          "${EXISTING}.new" "$EXISTING" "${EXISTING}.tmp"
        mv "${EXISTING}.tmp" "$EXISTING"
        rm -f "${EXISTING}.new"
      else
        echo "$CAUSE_WITH_OCC" > "$EXISTING"
      fi
    done
    CAUSE_COUNT=$(find "memory-repo/causes" -name '*.json' -type f 2>/dev/null | wc -l)
    echo "Persisted cause files to causes/ (${CAUSE_COUNT} total)"
  fi

  # Push the validated cause identities before issue side effects. A
  # concurrent publisher that cloned stale memory will fail here
  # instead of creating or updating an issue for a conflicting type.
  git -C memory-repo add -A
  if git -C memory-repo diff --cached --quiet; then
    echo "No initial changes to memory branch"
  else
    git -C memory-repo commit -m "Add CI failure analysis for run ${RUN_ID}"
    git -C memory-repo push origin "HEAD:$MEMORY_BRANCH"
    echo "Memory branch updated with analysis for run ${RUN_ID}"
  fi

# ── 2. Create or update issues for each cause ──
if [ -d "$CAUSES_DIR" ]; then
  # Build occurrence info from the run summary for issue updates
  # Build the occurrence table row for this run
  OCC_DATE=$(echo "$ANALYZED_AT" | cut -dT -f1)
  if [ "$RUN_SCOPE" = "main" ]; then
    OCCURRENCE_CONTEXT="main"
  elif [ "$PR_NUMBER" = "0" ]; then
    OCCURRENCE_CONTEXT="unavailable"
  else
    OCCURRENCE_CONTEXT="#${PR_NUMBER}"
  fi
  for CAUSE_FILE in "$CAUSES_DIR"/*.json; do
    [ -f "$CAUSE_FILE" ] || continue

    CAUSE_ID=$(jq -r '.id' "$CAUSE_FILE")

    CAUSE_TYPE=$(jq -r '.type' "$CAUSE_FILE")
    CAUSE_JOBS=$(bash .github/workflows/analyze-ci-failure-persistence.sh \
      cause-job-names "$CAUSE_FILE" "$TRUSTED_FAILED_JOBS_FILE" display)
    CAUSE_JOBS_TABLE=$(bash .github/workflows/analyze-ci-failure-persistence.sh \
      cause-job-names "$CAUSE_FILE" "$TRUSTED_FAILED_JOBS_FILE" table)
    NEW_OCCURRENCE_ROW="| ${OCC_DATE} | [${RUN_ID}](${RUN_URL}) | ${CAUSE_JOBS_TABLE} | ${OCCURRENCE_CONTEXT} |"

    CAUSE_STORED="memory-repo/causes/${CAUSE_ID}.json"
    MARKER="<!-- ci-failure-cause:${CAUSE_ID} -->"
    TYPE_MARKER="<!-- ci-failure-cause-type:${CAUSE_TYPE} -->"

    # Check if the stored cause file already has a linked issue
    EXISTING_ISSUE=""
    if [ -f "$CAUSE_STORED" ]; then
      STORED_ISSUE_URL=$(jq -r '.issue_url // empty' "$CAUSE_STORED")
      if [ -n "$STORED_ISSUE_URL" ]; then
        if [[ "$STORED_ISSUE_URL" =~ ^https://github\.com/${REPO}/issues/([0-9]+)$ ]]; then
          EXISTING_ISSUE="${BASH_REMATCH[1]}"
          ISSUE_JSON=$(gh api "repos/${REPO}/issues/${EXISTING_ISSUE}" 2>/dev/null || echo "")
          if [ -n "$ISSUE_JSON" ] && jq -e \
              --arg marker "$MARKER" \
              --arg type_marker "$TYPE_MARKER" \
              --arg cause_type "$CAUSE_TYPE" '
              (.body // "" | split("\n") | map(rtrimstr("\r"))) as $lines |
              (.pull_request == null) and
              any(.labels[]?; .name == "ci-failure-cause") and
              ($lines[0] == $marker) and
              (
                ($lines[1] == $type_marker) or
                ([$lines[] | select(startswith("**Type**: "))] == ["**Type**: " + $cause_type])
              )
            ' <<< "$ISSUE_JSON" >/dev/null; then
            ISSUE_STATE=$(jq -r '.state' <<< "$ISSUE_JSON")
          else
            echo "Linked issue #${EXISTING_ISSUE} does not match cause ${CAUSE_ID}, will search by marker"
            EXISTING_ISSUE=""
          fi
        else
          echo "Stored issue URL is not a canonical ${REPO} issue URL, will search by marker"
          EXISTING_ISSUE=""
        fi
      fi
    fi

    # If no stored issue link, fall back to searching by marker
    REOPEN="false"
    if [ -z "$EXISTING_ISSUE" ]; then
      # Fetch labeled issues for marker search (lazy-loaded once)
      if [ -z "${ISSUES_CACHE_LOADED:-}" ]; then
        OPEN_ISSUES_CACHE=$(mktemp)
        CLOSED_ISSUES_CACHE=$(mktemp)
        if ! bash .github/workflows/analyze-ci-failure-persistence.sh \
            cache-cause-issues "$REPO" "$OPEN_ISSUES_CACHE" "$CLOSED_ISSUES_CACHE"; then
          echo "::error::Cause issue lookup failed. Refusing to create an issue from incomplete results."
          exit 1
        fi
        ISSUES_CACHE_LOADED="true"
      fi

      EXISTING_ISSUE=$(jq -r \
        --arg marker "$MARKER" \
        --arg type_marker "$TYPE_MARKER" \
        --arg cause_type "$CAUSE_TYPE" '
          .[] |
          (.body // "" | split("\n") | map(rtrimstr("\r"))) as $lines |
          select(
            ($lines[0] == $marker) and
            (
              ($lines[1] == $type_marker) or
              ([$lines[] | select(startswith("**Type**: "))] == ["**Type**: " + $cause_type])
            )
          ) |
          .number
        ' \
        "$OPEN_ISSUES_CACHE" | head -1 || true)

      if [ -z "$EXISTING_ISSUE" ]; then
        EXISTING_ISSUE=$(jq -r \
          --arg marker "$MARKER" \
          --arg type_marker "$TYPE_MARKER" \
          --arg cause_type "$CAUSE_TYPE" '
            .[] |
            (.body // "" | split("\n") | map(rtrimstr("\r"))) as $lines |
            select(
              ($lines[0] == $marker) and
              (
                ($lines[1] == $type_marker) or
                ([$lines[] | select(startswith("**Type**: "))] == ["**Type**: " + $cause_type])
              )
            ) |
            .number
          ' \
          "$CLOSED_ISSUES_CACHE" | head -1 || true)
        if [ -n "$EXISTING_ISSUE" ]; then
          REOPEN="true"
        fi
      fi
    else
      # Check if the stored issue is closed (may need reopening)
      if [ "$ISSUE_STATE" = "closed" ]; then
        REOPEN="true"
      fi
    fi

    if [ "$CAUSE_TYPE" = "main-repository-breakage" ]; then
      gh label create "main-ci-break" --repo "$REPO" \
        --color "b60205" \
        --description "Deterministic repository breakage on the main branch" \
        --force
    fi

    if [ -n "$EXISTING_ISSUE" ]; then
      # Store issue URL in the cause file on memory branch
      ISSUE_URL="https://github.com/${REPO}/issues/${EXISTING_ISSUE}"
      if [ -f "$CAUSE_STORED" ]; then
        jq --arg url "$ISSUE_URL" '.issue_url = $url' "$CAUSE_STORED" > "${CAUSE_STORED}.tmp" \
          && mv "${CAUSE_STORED}.tmp" "$CAUSE_STORED"
      fi

      # Keep the newest occurrence rows within GitHub's issue-body
      # budget while the memory branch retains the complete history.
      CURRENT_BODY_FILE=$(mktemp)
      gh api "repos/${REPO}/issues/${EXISTING_ISSUE}" --jq '.body // ""' > "$CURRENT_BODY_FILE"
      BODY_FILE=""
      BODY_SOURCE_FILE="$CURRENT_BODY_FILE"
      OCCURRENCE_BODY_AVAILABLE="false"
      # Anchor the pattern with '(' from the markdown link to avoid
      # partial matches (e.g., run 123 matching run 1234).
      if grep -qF "[${RUN_ID}](" "$CURRENT_BODY_FILE"; then
        echo "Occurrence for run ${RUN_ID} already recorded in issue #${EXISTING_ISSUE}. Skipping."
      else
        BODY_FILE=$(mktemp)
        TOTAL_OCCURRENCE_COUNT=$(jq '.occurrences | length' "$CAUSE_STORED")
        set +e
        bash .github/workflows/analyze-ci-failure-persistence.sh render-issue-occurrences \
          "$CURRENT_BODY_FILE" "$NEW_OCCURRENCE_ROW" "$TOTAL_OCCURRENCE_COUNT" "$BODY_FILE"
        OCCURRENCE_RENDER_STATUS=$?
        set -e
        if [ "$OCCURRENCE_RENDER_STATUS" -eq 0 ]; then
          BODY_SOURCE_FILE="$BODY_FILE"
          OCCURRENCE_BODY_AVAILABLE="true"
        elif [ "$OCCURRENCE_RENDER_STATUS" -eq 2 ]; then
          echo "::warning::Issue #${EXISTING_ISSUE} has an unsupported occurrence section. Skipping occurrence update."
          rm -f "$BODY_FILE"
          BODY_FILE=""
        else
          echo "::error::Unable to render occurrence history for issue #${EXISTING_ISSUE}."
          rm -f "$CURRENT_BODY_FILE" "$BODY_FILE"
          exit "$OCCURRENCE_RENDER_STATUS"
        fi
      fi

      if [ "$CAUSE_TYPE" = "main-repository-breakage" ]; then
        # Existing issues may contain agent-authored attribution from
        # older runs. Refresh the generated details from trusted
        # context while retaining occurrence history and operator notes.
        CANONICAL_BODY_FILE=$(mktemp)
        ISSUE_METADATA_FILE=$(mktemp)
        MIGRATED_BODY_FILE=$(mktemp)
        bash .github/workflows/analyze-ci-failure-issue.sh \
          "$CAUSE_STORED" "$RUN_CONTEXT_FILE" \
          ci-failure-data/last-successful-main-run.json \
          ci-failure-data/triggering-merge-pr.json \
          ci-failure-data/candidate-merge-history-status.json \
          "$RUN_URL" "$RUN_SCOPE" "$PR_NUMBER" "$CAUSE_JOBS" \
          "$NEW_OCCURRENCE_ROW" "$CANONICAL_BODY_FILE" "$ISSUE_METADATA_FILE"
        MIGRATED_BODY_AVAILABLE="false"
        if bash .github/workflows/analyze-ci-failure-persistence.sh \
            migrate-main-issue-body \
            "$BODY_SOURCE_FILE" "$CANONICAL_BODY_FILE" "$MIGRATED_BODY_FILE"; then
          MIGRATED_BODY_AVAILABLE="true"
        else
          echo "::warning::Unable to migrate publisher-owned details for issue #${EXISTING_ISSUE}. Updating only the fields that can be changed safely."
        fi
        ISSUE_TITLE=$(jq -r '.title' "$ISSUE_METADATA_FILE")
        ISSUE_LABELS=$(jq -r '.labels' "$ISSUE_METADATA_FILE")
        if [ "$MIGRATED_BODY_AVAILABLE" = "true" ]; then
          gh issue edit "$EXISTING_ISSUE" --repo "$REPO" \
            --title "$ISSUE_TITLE" --body-file "$MIGRATED_BODY_FILE" \
            --add-label "$ISSUE_LABELS"
        elif [ "$OCCURRENCE_BODY_AVAILABLE" = "true" ]; then
          gh issue edit "$EXISTING_ISSUE" --repo "$REPO" \
            --title "$ISSUE_TITLE" --body-file "$BODY_FILE" \
            --add-label "$ISSUE_LABELS"
        else
          gh issue edit "$EXISTING_ISSUE" --repo "$REPO" --title "$ISSUE_TITLE" \
            --add-label "$ISSUE_LABELS"
        fi
        rm -f "$CANONICAL_BODY_FILE" "$ISSUE_METADATA_FILE" "$MIGRATED_BODY_FILE"
      elif [ "$OCCURRENCE_BODY_AVAILABLE" = "true" ]; then
        gh issue edit "$EXISTING_ISSUE" --repo "$REPO" --body-file "$BODY_FILE"
      fi
      rm -f "${BODY_FILE:-}"
      rm -f "$CURRENT_BODY_FILE"

      if [ "$REOPEN" = "true" ]; then
        gh issue reopen "$EXISTING_ISSUE" --repo "$REPO"
        echo "Reopened and updated issue #${EXISTING_ISSUE} for cause: ${CAUSE_ID}"
      else
        echo "Updated issue #${EXISTING_ISSUE} for cause: ${CAUSE_ID}"
      fi
    else
      # Create a new issue for this cause
      BODY_FILE=$(mktemp)
      ISSUE_METADATA_FILE=$(mktemp)
      set +e
      bash .github/workflows/analyze-ci-failure-issue.sh \
        "$CAUSE_STORED" "$RUN_CONTEXT_FILE" \
        ci-failure-data/last-successful-main-run.json \
        ci-failure-data/triggering-merge-pr.json \
        ci-failure-data/candidate-merge-history-status.json \
        "$RUN_URL" "$RUN_SCOPE" "$PR_NUMBER" "$CAUSE_JOBS" \
        "$NEW_OCCURRENCE_ROW" "$BODY_FILE" "$ISSUE_METADATA_FILE"
      ISSUE_RENDER_STATUS=$?
      set -e
      if [ "$ISSUE_RENDER_STATUS" -eq 2 ]; then
        echo "::warning::Cause issue body exceeds the publication budget. Skipping issue creation."
        rm -f "$BODY_FILE" "$ISSUE_METADATA_FILE"
        continue
      elif [ "$ISSUE_RENDER_STATUS" -ne 0 ]; then
        rm -f "$BODY_FILE" "$ISSUE_METADATA_FILE"
        exit "$ISSUE_RENDER_STATUS"
      fi

      ISSUE_TITLE=$(jq -r '.title' "$ISSUE_METADATA_FILE")
      LABELS=$(jq -r '.labels' "$ISSUE_METADATA_FILE")
      CREATED_ISSUE_URL=$(gh issue create --repo "$REPO" \
        --title "$ISSUE_TITLE" \
        --label "$LABELS" \
        --body-file "$BODY_FILE")
      rm -f "$BODY_FILE" "$ISSUE_METADATA_FILE"
      echo "Created issue for cause: ${CAUSE_ID} — ${CREATED_ISSUE_URL}"

      # Store issue URL in the cause file on memory branch
      if [ -f "$CAUSE_STORED" ]; then
        jq --arg url "$CREATED_ISSUE_URL" '.issue_url = $url' "$CAUSE_STORED" > "${CAUSE_STORED}.tmp" \
          && mv "${CAUSE_STORED}.tmp" "$CAUSE_STORED"
      fi
    fi
  done
  rm -f "${OPEN_ISSUES_CACHE:-}" "${CLOSED_ISSUES_CACHE:-}"
fi

  # ── 3. Push issue links to the memory branch ──
  git -C memory-repo add -A
  if git -C memory-repo diff --cached --quiet; then
    echo "No issue-link changes to memory branch"
  else
    git -C memory-repo commit -m "Link CI failure issues for run ${RUN_ID}"
    git -C memory-repo push origin "HEAD:$MEMORY_BRANCH"
    echo "Memory branch updated with issue links for run ${RUN_ID}"
  fi
fi
