#!/usr/bin/env bash
set -euo pipefail

CHANGELOG=extension/CHANGELOG.md
if [ ! -f "${CHANGELOG}" ]; then
  echo "No ${CHANGELOG} file in this checkout; skipping authoritative range preload."
  exit 0
fi

MARKERS=()
while IFS= read -r marker; do
  MARKERS+=("${marker}")
done < <(grep '<!-- aspire-ext-changelog from=' "${CHANGELOG}" || true)
if [ "${#MARKERS[@]}" -eq 0 ]; then
  echo "No pending aspire-ext-changelog marker present; skipping authoritative range preload."
  exit 0
fi

if [ "${#MARKERS[@]}" -ne 1 ]; then
  echo "::error::Expected exactly one pending aspire-ext-changelog marker, found ${#MARKERS[@]}."
  exit 1
fi

MARKER_LINE="${MARKERS[0]}"
# Bash parses literal '<' tokens in an inline [[ ... =~ ... ]] regex as syntax,
# so keep the HTML comment marker pattern in a variable before matching it.
MARKER_REGEX='^<!-- aspire-ext-changelog from=([0-9a-f]{40}) to=([0-9a-f]{40}) base=[^>]* -->$'
if [[ ! "${MARKER_LINE}" =~ ${MARKER_REGEX} ]]; then
  echo "::error::Could not parse authoritative marker: ${MARKER_LINE}"
  exit 1
fi

FROM_SHA="${BASH_REMATCH[1]}"
TO_SHA="${BASH_REMATCH[2]}"
CANDIDATES_FILE="${RUNNER_TEMP}/gh-aw/extension-changelog-candidates.tsv"
mkdir -p "${RUNNER_TEMP}/gh-aw"
rm -f "${CANDIDATES_FILE}"
CURRENT_BRANCH="$(git branch --show-current)"
if [ -z "${CURRENT_BRANCH}" ]; then
  echo "::error::Could not determine the checked-out PR branch for authoritative range preload."
  exit 1
fi

materialize_candidate_set() {
  if ! git cat-file -e "${FROM_SHA}^{commit}" 2>/dev/null \
    || ! git cat-file -e "${TO_SHA}^{commit}" 2>/dev/null \
    || ! git merge-base --is-ancestor "${FROM_SHA}" "${TO_SHA}" 2>/dev/null; then
    return 1
  fi

  if ! git log --format='%H%x09%s' --no-merges \
    "${FROM_SHA}..${TO_SHA}" -- extension/ > "${CANDIDATES_FILE}"; then
    rm -f "${CANDIDATES_FILE}"
    return 1
  fi

  local candidate_count
  candidate_count="$(wc -l < "${CANDIDATES_FILE}")"
  echo "Materialized ${candidate_count} authoritative extension changelog candidates in ${CANDIDATES_FILE}."
}

if materialize_candidate_set; then
  echo "Authoritative marker range ${FROM_SHA}..${TO_SHA} is already locally enumerable."
  exit 0
fi

DEEPEN_BY=128
ATTEMPT=0
while [ "$(git rev-parse --is-shallow-repository)" = "true" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if [ "${ATTEMPT}" -le 6 ]; then
    echo "Deepening ${CURRENT_BRANCH} by ${DEEPEN_BY} commits to preload ${FROM_SHA}..${TO_SHA}."
    git fetch --no-tags --deepen="${DEEPEN_BY}" origin "${CURRENT_BRANCH}"
    DEEPEN_BY=$((DEEPEN_BY * 2))
  else
    echo "Unshallowing ${CURRENT_BRANCH} to preload ${FROM_SHA}..${TO_SHA}."
    git fetch --no-tags --unshallow origin "${CURRENT_BRANCH}"
  fi

  if materialize_candidate_set; then
    echo "Preloaded authoritative marker range ${FROM_SHA}..${TO_SHA} for local changelog enumeration."
    exit 0
  fi
done

if materialize_candidate_set; then
  echo "Preloaded authoritative marker range ${FROM_SHA}..${TO_SHA} for local changelog enumeration."
  exit 0
fi

echo "::error::Failed to preload authoritative marker range ${FROM_SHA}..${TO_SHA} for local changelog enumeration."
exit 1
