#!/usr/bin/env python3
"""Print the canonical diagnostic GET contract or preflight a URL without fetching it."""
from __future__ import annotations

import argparse

from ci_shepherd.investigation_scope import diagnostic_get_contract, validate_diagnostic_get
from ci_shepherd.models import stable_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--url", help="Check the exact URL before making a GET; no network operation is performed.")
    args = parser.parse_args()
    request = {"repository": args.repository, "issueNumber": args.issue_number}
    try:
        output = diagnostic_get_contract(request) if args.url is None else {
            "method": "GET", "url": validate_diagnostic_get(request, args.url),
            "executed": False,
        }
    except ValueError as error:
        parser.error(str(error))
    print(stable_json(output), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
