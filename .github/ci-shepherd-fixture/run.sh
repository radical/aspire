#!/usr/bin/env bash

set -euo pipefail

fixture_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mode="$(<"$fixture_directory/mode.txt")"

case "$mode" in
  fail-build)
    python3 "$fixture_directory/resolve_config.py"
    ;;
  slow-fail-build)
    sleep 20
    python3 "$fixture_directory/resolve_config.py"
    ;;
  success)
    exit 0
    ;;
  slow-success)
    sleep 20
    exit 0
    ;;
  fail-test)
    python3 - <<'PY'
import unittest


class FixtureFailureTests(unittest.TestCase):
    def test_controlled_failure(self) -> None:
        self.assertEqual("expected", "actual")


if __name__ == "__main__":
    unittest.main()
PY
    ;;
  *)
    echo "Unknown fixture mode: $mode" >&2
    exit 2
    ;;
esac
