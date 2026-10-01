#!/usr/bin/env bash

set -euo pipefail

: "${PR_BASE_SHA:?PR_BASE_SHA must be set}"
: "${HEAD_SHA:?HEAD_SHA must be set}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"

# The base SHA should always be reachable from the public origin. Treat a fetch or object
# validation failure as an infrastructure error rather than hiding it behind a full test run.
if ! git fetch --no-tags --depth=1 origin "$PR_BASE_SHA"; then
  echo "::error::Failed to fetch PR base commit $PR_BASE_SHA; cannot compute the changed-file diff." >&2
  exit 1
fi
if ! git cat-file -e "${PR_BASE_SHA}^{commit}" 2>/dev/null; then
  echo "::error::PR base commit $PR_BASE_SHA is unavailable after fetch; cannot compute the changed-file diff." >&2
  exit 1
fi

# A shallow checkout can truncate either side before their common ancestor. Deepen both
# endpoints geometrically, but keep a hard bound so pathological histories cannot fetch forever.
merge_base_found=true
depth=1
until git merge-base "$PR_BASE_SHA" "$HEAD_SHA" >/dev/null 2>&1; do
  if [ "$depth" -ge 4096 ]; then
    echo "::warning::Could not find a merge-base of base $PR_BASE_SHA and head $HEAD_SHA within $depth commits of history; running ALL tests for this PR." >&2
    merge_base_found=false
    break
  fi

  depth=$((depth * 4))
  echo "Merge-base of base..head not yet reachable; deepening history to depth $depth."
  # The merge-base probe is the real progress check. One endpoint may already be complete, so a
  # fetch that cannot deepen further must not abort before the bounded probe can decide.
  git fetch --no-tags --depth="$depth" origin "$PR_BASE_SHA" "$HEAD_SHA" || true
done

if [ "$merge_base_found" = "true" ]; then
  mode="diff"
  from_sha=$PR_BASE_SHA
  to_sha=$HEAD_SHA
  force_all_reason=
else
  mode="force-all"
  from_sha=
  to_sha=
  force_all_reason="git merge-base of base $PR_BASE_SHA and head $HEAD_SHA was unreachable within $depth commits of CI checkout history"
fi

{
  echo "mode=$mode"
  echo "from_sha=$from_sha"
  echo "to_sha=$to_sha"
  echo "force_all_reason=$force_all_reason"
} >> "$GITHUB_OUTPUT"
