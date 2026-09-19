#!/usr/bin/env bash

set -euo pipefail

fixture_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mode="$(<"$fixture_directory/mode.txt")"

case "$mode" in
  fail-build)
    python3 "$fixture_directory/resolve_config.py"
    ;;
  success)
    exit 0
    ;;
  *)
    echo "Unknown fixture mode: $mode" >&2
    exit 2
    ;;
esac
