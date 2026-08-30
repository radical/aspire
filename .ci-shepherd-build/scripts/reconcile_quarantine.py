#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

from ci_shepherd.github import GitHubClient
from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_reconciliation import (
    reconcile_quarantine_pull_requests,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GET-verify pending quarantine pull requests and reconcile their state."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    client = GitHubClient(
        runner=subprocess.run,
        popen_factory=subprocess.Popen,
        sleep=time.sleep,
        now=lambda: datetime.now(UTC),
        audit_path=args.audit,
    )
    result = reconcile_quarantine_pull_requests(
        state_directory=args.state_dir,
        repository=args.repository,
        recorded_at=args.recorded_at,
        get_pull=lambda repository, number: client.get(
            f"/repos/{quote(repository, safe='/')}/pulls/{number}"
        ),
    )
    print(stable_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
