#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

from ci_shepherd.github import GitHubClient
from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_reconciliation import (
    reconcile_quarantine_pull_requests,
    verify_merged_quarantine_source,
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

    def get_file(path: str, revision: str) -> bytes:
        document = client.get(
            f"/repos/{quote(args.repository, safe='/')}/contents/"
            f"{quote(path, safe='/')}?ref={quote(revision, safe='')}"
        )
        if (
            not isinstance(document, dict)
            or document.get("encoding") != "base64"
            or not isinstance(document.get("content"), str)
        ):
            raise ValueError("GitHub returned invalid merged source content.")
        # The Contents API wraps base64 content with newlines, for example:
        #   "content": "W1F1YXJhbnRpbmVkVGVz\\ndF0K"
        # Remove only line endings so strict decoding still rejects other junk.
        content = document["content"].replace("\r", "").replace("\n", "")
        return base64.b64decode(content, validate=True)

    result = reconcile_quarantine_pull_requests(
        state_directory=args.state_dir,
        repository=args.repository,
        recorded_at=args.recorded_at,
        get_pull=lambda repository, number: client.get(
            f"/repos/{quote(repository, safe='/')}/pulls/{number}"
        ),
        verify_merged_source=lambda event, pull: (
            isinstance(event.get("mutationValidation"), dict)
            and isinstance(pull.get("merge_commit_sha"), str)
            and verify_merged_quarantine_source(
                event,
                event["mutationValidation"],
                merge_commit_sha=pull["merge_commit_sha"],
                tool_project=(
                    Path(__file__).resolve().parents[2]
                    / "tools"
                    / "QuarantineTools"
                ),
                get_file=get_file,
            )
        ),
    )
    print(stable_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
