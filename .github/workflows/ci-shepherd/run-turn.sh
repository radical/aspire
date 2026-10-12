#!/usr/bin/env bash
# One refreshed turn; cloud completion is observed by the next invocation.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$root"
exec python3 -B .github/workflows/ci-shepherd/local.py run "$@"
